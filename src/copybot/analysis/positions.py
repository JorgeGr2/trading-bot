"""Contabilidad de las posiciones de una wallet a partir de sus eventos (FIFO).

Reglas:

* Una compra pagada en USDC o USDT abre un lote con coste conocido en USD. Si se paga
  con otra moneda (SOL), el lote queda con coste desconocido: no usamos precios
  históricos de SOL que no podamos verificar.
* Una transferencia recibida abre un lote «gratis» (coste desconocido): puede ser un
  reparto automático, un envío desde otra wallet del mismo trader o tokens del creador.
* Una venta consume lotes en orden FIFO. Solo realiza PnL la parte vendida de lotes con
  coste conocido y cobrada en USD. Lo vendido sin lote (comprado antes del periodo
  importado, o por una vía que no vemos) cuenta como **venta sin coste conocido**.
* Una transferencia enviada consume lotes sin realizar PnL.
* Una operación (*round trip*) empieza cuando la posición en un token pasa de cero a
  positiva y termina cuando queda como mucho ``close_tolerance`` de la cantidad máxima
  alcanzada. Ese resto se da por perdido (criterio prudente).

Todo se calcula con ``Decimal``: el resultado es exacto y reproducible.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

USD_QUOTES = frozenset({"USDC", "USDT"})
ZERO = Decimal(0)


@dataclass(frozen=True)
class Trade:
    """Un evento de la wallet, ya normalizado para el análisis."""

    signature: str
    slot: int
    tx_index: int
    time: datetime
    kind: str  # buy | sell | transfer_in | transfer_out | ambiguous
    mint: str | None
    amount: Decimal  # tokens, siempre positivo
    usd: Decimal | None  # importe pagado o cobrado en USD (positivo), si fue en USDC/USDT
    confidence: str = "high"

    @property
    def sort_key(self) -> tuple[int, int, str]:
        return (self.slot, self.tx_index, self.signature)


@dataclass
class _Lot:
    amount: Decimal
    cost: Decimal | None
    opened_at: datetime
    free: bool


@dataclass(frozen=True)
class Realization:
    """La parte de una venta que realiza PnL (coste y cobro conocidos)."""

    time: datetime
    mint: str
    amount: Decimal
    cost: Decimal
    proceeds: Decimal
    hold_seconds: Decimal  # media ponderada por cantidad

    @property
    def pnl(self) -> Decimal:
        return self.proceeds - self.cost


@dataclass
class RoundTrip:
    mint: str
    opened_at: datetime
    opened_slot: int
    opening_signature: str
    closed_at: datetime | None = None
    first_sell_at: datetime | None = None
    buys: int = 0
    sells: int = 0
    invested: Decimal = ZERO  # coste conocido de las compras
    cost: Decimal = ZERO  # coste de lo vendido con coste y cobro conocidos
    proceeds: Decimal = ZERO
    written_off: Decimal = ZERO  # coste del resto abandonado al cerrar
    max_amount: Decimal = ZERO
    unpriced_sold: Decimal = ZERO  # tokens vendidos sin coste o sin cobro conocidos
    free_tokens: bool = False  # incluye tokens recibidos por transferencia
    ended_by_transfer: bool = False
    same_slot_exit: bool = False  # vende en el mismo bloque en que compró
    # Solo para operaciones abiertas en ``as_of``: lo que queda y su coste conocido.
    remaining_amount: Decimal = ZERO
    remaining_cost: Decimal = ZERO
    last_activity_at: datetime | None = None  # última compra, venta o transferencia

    @property
    def closed(self) -> bool:
        return self.closed_at is not None

    @property
    def priced_cost(self) -> Decimal:
        return self.cost + self.written_off

    @property
    def pnl(self) -> Decimal:
        return self.proceeds - self.cost - self.written_off

    @property
    def measurable(self) -> bool:
        """Cerrada por ventas y con coste conocido: cuenta para las estadísticas."""
        return self.closed and not self.ended_by_transfer and self.priced_cost > 0

    @property
    def roi(self) -> Decimal | None:
        return self.pnl / self.priced_cost if self.priced_cost > 0 else None

    @property
    def abandoned_roi(self) -> Decimal | None:
        """Rentabilidad de una operación abierta si lo que queda vale 0 (criterio prudente)."""
        basis = self.cost + self.remaining_cost
        if self.closed or basis <= 0:
            return None
        return (self.proceeds - basis) / basis

    @property
    def hold_seconds(self) -> Decimal | None:
        if self.closed_at is None:
            return None
        return Decimal((self.closed_at - self.opened_at).total_seconds())

    @property
    def seconds_to_first_sell(self) -> Decimal | None:
        if self.first_sell_at is None:
            return None
        return Decimal((self.first_sell_at - self.opened_at).total_seconds())


@dataclass(frozen=True)
class ValueAt:
    time: datetime
    usd: Decimal | None


@dataclass
class Ledger:
    round_trips: list[RoundTrip] = field(default_factory=list)
    realizations: list[Realization] = field(default_factory=list)
    # Coste conocido de todo lo abierto justo después de cada evento.
    open_cost_curve: list[ValueAt] = field(default_factory=list)
    # Ventas (o partes) sin lote: el token se obtuvo antes del periodo o sin que lo veamos.
    unmatched_sales: list[ValueAt] = field(default_factory=list)
    # Ventas de tokens recibidos por transferencia (no comprados).
    free_token_sales: list[ValueAt] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)

    def open_trips(self) -> list[RoundTrip]:
        return [t for t in self.round_trips if not t.closed]


def build_ledger(
    trades: list[Trade], as_of: datetime, close_tolerance: Decimal = Decimal("0.01")
) -> Ledger:
    """Recorre los eventos hasta ``as_of`` (incluido) en orden y devuelve la contabilidad.

    Los eventos posteriores a ``as_of`` se ignoran: así es imposible usar información
    del futuro aunque se pasen por error.
    """
    ledger = Ledger()
    lots: dict[str, deque[_Lot]] = {}
    trips: dict[str, RoundTrip] = {}
    open_cost = ZERO

    for tr in sorted((t for t in trades if t.time <= as_of), key=lambda t: t.sort_key):
        ledger.counts[f"{tr.kind}/{tr.confidence}"] += 1
        if tr.kind == "ambiguous" or tr.mint is None:
            continue
        if tr.kind in ("buy", "sell") and tr.usd is None:
            ledger.counts["non_usd_quote"] += 1
        queue = lots.setdefault(tr.mint, deque())

        if tr.kind in ("buy", "transfer_in"):
            trip = trips.get(tr.mint)
            if trip is None:
                trip = RoundTrip(tr.mint, tr.time, tr.slot, tr.signature)
                trips[tr.mint] = trip
                ledger.round_trips.append(trip)
            trip.last_activity_at = tr.time
            free = tr.kind == "transfer_in"
            cost = tr.usd if tr.kind == "buy" else None
            queue.append(_Lot(tr.amount, cost, tr.time, free))
            if free:
                trip.free_tokens = True
            else:
                trip.buys += 1
                if cost is not None:
                    trip.invested += cost
                    open_cost += cost
            trip.max_amount = max(trip.max_amount, sum((lot.amount for lot in queue), ZERO))

        elif tr.kind in ("sell", "transfer_out"):
            is_sale = tr.kind == "sell"
            unit_price = tr.usd / tr.amount if is_sale and tr.usd is not None else None
            remaining = tr.amount
            r_amount = r_cost = r_proceeds = r_hold = ZERO
            unpriced = ZERO
            free_proceeds = ZERO
            while remaining > 0 and queue:
                lot = queue[0]
                take = min(lot.amount, remaining)
                fraction = take / lot.amount
                part_cost = lot.cost * fraction if lot.cost is not None else None
                if part_cost is not None:
                    open_cost -= part_cost
                if is_sale:
                    if part_cost is not None and unit_price is not None:
                        r_amount += take
                        r_cost += part_cost
                        r_proceeds += unit_price * take
                        r_hold += take * Decimal((tr.time - lot.opened_at).total_seconds())
                    else:
                        unpriced += take
                        if lot.free and unit_price is not None:
                            free_proceeds += unit_price * take
                lot.amount -= take
                if lot.cost is not None and part_cost is not None:
                    lot.cost -= part_cost
                remaining -= take
                if lot.amount == 0:
                    queue.popleft()
            if remaining > 0 and is_sale:
                ledger.unmatched_sales.append(
                    ValueAt(tr.time, unit_price * remaining if unit_price is not None else None)
                )
                unpriced += remaining
            if free_proceeds:
                ledger.free_token_sales.append(ValueAt(tr.time, free_proceeds))
            if r_amount:
                ledger.realizations.append(
                    Realization(tr.time, tr.mint, r_amount, r_cost, r_proceeds, r_hold / r_amount)
                )

            trip = trips.get(tr.mint)
            if trip is not None:
                trip.last_activity_at = tr.time
                if is_sale:
                    trip.sells += 1
                    if trip.first_sell_at is None:
                        trip.first_sell_at = tr.time
                        trip.same_slot_exit = tr.slot == trip.opened_slot
                    trip.cost += r_cost
                    trip.proceeds += r_proceeds
                    trip.unpriced_sold += unpriced
                else:
                    trip.ended_by_transfer = True
                left = sum((lot.amount for lot in queue), ZERO)
                if left <= close_tolerance * trip.max_amount:
                    abandoned = sum((lot.cost for lot in queue if lot.cost is not None), ZERO)
                    if abandoned:
                        # El resto se da por perdido: es una pérdida realizada al cerrar.
                        trip.written_off += abandoned
                        open_cost -= abandoned
                        ledger.realizations.append(
                            Realization(tr.time, tr.mint, left, abandoned, ZERO, ZERO)
                        )
                    queue.clear()
                    trip.closed_at = tr.time
                    del trips[tr.mint]
            elif is_sale:
                ledger.counts["sell_without_position"] += 1

        ledger.open_cost_curve.append(ValueAt(tr.time, open_cost))

    for mint, trip in trips.items():
        trip.remaining_amount = sum((lot.amount for lot in lots[mint]), ZERO)
        trip.remaining_cost = sum((lot.cost for lot in lots[mint] if lot.cost is not None), ZERO)
    return ledger

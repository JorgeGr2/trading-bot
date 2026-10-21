"""Backtest del copy-trading: qué habría pasado copiando a unos traders en un periodo,
comprando y vendiendo con el retraso medido en vivo y con todos los costes.

Para cada compra del trader (en orden cronológico):

* **Precio retrasado real**: el de las operaciones de otros usuarios entre 4 y 8 s
  después (``DelayedPrices``), más nuestro propio impacto (supuesto, por lado), el
  recorte por MEV, la comisión de red y prioritaria, la renta de la cuenta de token y la
  comisión de transferencia del token.
* **Dos libros**, como en vivo: la cartera (con los límites del perfil de riesgo) y el
  libro de señales (cada compra, sin límite de capital).

Salidas: la misma fracción que vende el trader (con sus saldos), stop-loss cuando un
precio observado lo dispara, venta forzada a las 48 h y valoración de lo que siga
abierto al final del periodo. Si a la hora de vender no hay ninguna operación en el
token (nadie compra), la posición se da por perdida.

**Copia teórica**: las mismas operaciones a los precios exactos del trader y sin costes.
La diferencia con la realista es lo que cuesta llegar tarde.

Todo es determinista: con los mismos datos (en caché) y parámetros, el mismo resultado.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from copybot.config import RiskLimits
from copybot.sim.risk import BookState, entry_rejections, halt_reasons

ZERO = Decimal(0)
ONE = Decimal(1)
LAMPORTS_PER_SOL = Decimal(1_000_000_000)
PORTFOLIO = "portfolio"
SIGNALS = "signals"


@dataclass(frozen=True)
class CopyParams:
    size_usd: Decimal = Decimal(25)
    delay: tuple[int, int] = (4, 8)
    # Nuestro propio impacto en el precio, por lado (supuesto: en vivo, comprar y vender
    # a la vez costó un 1,3 % en Kimchi y un 6,4 % en Elon Coin).
    impact_pct: Decimal = Decimal("1.5")
    mev_bps: int = 50
    base_fee_lamports: int = 5_000
    priority_fee_lamports: int = 100_000
    rent_lamports: int = 1_488_440
    stop_loss_pct: Decimal = Decimal(50)
    max_hold_hours: int = 48
    exit_dust_fraction: Decimal = Decimal("0.02")
    # Multiplica impacto y MEV (sensibilidad: 0,5 = la mitad de costes, 2 = el doble).
    cost_multiplier: Decimal = ONE
    # Precio de SOL si no se puede estimar de las operaciones (solo para las comisiones).
    fallback_sol_usd: Decimal = Decimal(150)


@dataclass(frozen=True)
class TraderEvent:
    """Una compraventa (o transferencia) del trader, ya leída de la base de datos."""

    wallet: str
    mint: str
    kind: str  # buy | sell | transfer_in | transfer_out
    amount: Decimal  # tokens, positivo
    usd: Decimal | None  # importe en USD si pagó/cobró en USDC/USDT
    sol: Decimal | None  # importe en SOL si pagó/cobró en SOL
    at: int  # segundos Unix (hora del bloque)
    order: int  # para desempatar en el mismo segundo


@dataclass(frozen=True)
class Quote:
    price_usd: Decimal | None  # None: nadie operó el token en la ventana
    sol_usd: Decimal | None


class PriceSource(Protocol):
    async def quote(self, mint: str, at: int, window: tuple[int, int]) -> Quote | None:
        """Precio retrasado; ``None`` si no está disponible (ni en caché ni consultable)."""
        ...


@dataclass
class Exit:
    at: int
    reason: str  # copia | stop_loss | max_hold | final | perdida
    tokens: Decimal
    price_usd: Decimal | None
    proceeds_usd: Decimal  # neto de costes (con la renta devuelta al cerrar)


@dataclass
class CopyPosition:
    book: str
    wallet: str
    mint: str
    opened_at: int
    tokens: Decimal
    invested_usd: Decimal  # todo lo pagado (comisiones y renta incluidas)
    trader_entry_price: Decimal | None
    entry_price: Decimal  # precio retrasado pagado (antes de nuestros costes)
    rent_usd: Decimal
    last_price: Decimal  # último precio observado (para valorar)
    theo_tokens: Decimal | None  # copia teórica
    theo_invested: Decimal
    theo_proceeds: Decimal = ZERO
    proceeds_usd: Decimal = ZERO
    exits: list[Exit] = field(default_factory=list)
    closed_at: int | None = None
    status: str = "open"  # open | closed | written_off

    @property
    def pnl_usd(self) -> Decimal:
        return self.proceeds_usd - self.invested_usd

    @property
    def return_pct(self) -> Decimal:
        return self.pnl_usd / self.invested_usd * 100

    @property
    def theo_pnl_usd(self) -> Decimal | None:
        if self.theo_tokens is None:
            return None
        return self.theo_proceeds - self.theo_invested

    @property
    def entry_slippage_pct(self) -> Decimal | None:
        """Cuánto más caro (o barato) que el trader se entra, antes de nuestros costes."""
        if not self.trader_entry_price:
            return None
        return (self.entry_price / self.trader_entry_price - 1) * 100

    def value(self) -> Decimal:
        return self.tokens * self.last_price


@dataclass
class CopyResult:
    params: CopyParams
    start: int
    end: int
    positions: list[CopyPosition]
    skipped: dict[str, int]  # motivo → compras no copiadas
    equity_curve: list[tuple[int, Decimal]]  # cartera: (hora, patrimonio)
    halts: list[tuple[int, str]]
    initial_usd: Decimal

    def book(self, name: str) -> list[CopyPosition]:
        return [p for p in self.positions if p.book == name]


class CopyBacktest:
    def __init__(
        self,
        prices: PriceSource,
        params: CopyParams,
        limits: RiskLimits,
        *,
        transfer_fee_bps: dict[str, int] | None = None,
        trader_wallets: Iterable[str] = (),
    ) -> None:
        self._prices = prices
        self.p = params
        self.limits = limits
        self._fee_bps = transfer_fee_bps or {}
        self._wallets = set(trader_wallets)

    # --- Costes ---------------------------------------------------------------------------

    def _impact(self) -> Decimal:
        return self.p.impact_pct / 100 * self.p.cost_multiplier

    def _mev(self) -> Decimal:
        return Decimal(self.p.mev_bps) / 10_000 * self.p.cost_multiplier

    def _lamports(self, lamports: int, sol_usd: Decimal) -> Decimal:
        return Decimal(lamports) / LAMPORTS_PER_SOL * sol_usd

    def _tx_fee(self, sol_usd: Decimal) -> Decimal:
        return self._lamports(self.p.base_fee_lamports + self.p.priority_fee_lamports, sol_usd)

    def _tfee(self, mint: str) -> Decimal:
        return Decimal(self._fee_bps.get(mint, 0)) / 10_000

    def _buy_tokens(self, mint: str, usd: Decimal, price: Decimal) -> Decimal:
        paid_price = price * (ONE + self._impact())
        return usd / paid_price * (ONE - self._mev()) * (ONE - self._tfee(mint))

    def _sell_usd(self, mint: str, tokens: Decimal, price: Decimal) -> Decimal:
        arriving = tokens * (ONE - self._tfee(mint))
        return arriving * price * (ONE - self._impact()) * (ONE - self._mev())

    async def _trader_price(self, ev: TraderEvent) -> Decimal | None:
        """Precio en USD al que operó el trader (los pagos en SOL, con el precio de SOL
        estimado de las operaciones de esa ventana)."""
        if ev.amount <= 0:
            return None
        if ev.usd is not None:
            return ev.usd / ev.amount
        if ev.sol is not None:
            quote = await self._prices.quote(ev.mint, ev.at, self.p.delay)
            if quote is not None and quote.sol_usd:
                return ev.sol * quote.sol_usd / ev.amount
        return None

    # --- Ejecución ------------------------------------------------------------------------

    async def run(self, events: list[TraderEvent], start: int, end: int) -> CopyResult:
        """Copia los eventos de ``[start, end)``. Los anteriores solo sirven para conocer
        el saldo del trader (y así la fracción que vende)."""
        run = _Run(self, start, end)
        for ev in sorted(events, key=lambda e: (e.at, e.order)):
            await run.event(ev)
        await run.finish()
        return CopyResult(
            params=self.p,
            start=start,
            end=end,
            positions=run.positions,
            skipped=dict(run.skipped),
            equity_curve=run.curve,
            halts=run.halts,
            initial_usd=self.limits.virtual_capital_usd,
        )


class _Run:
    """Estado de una pasada del backtest (cartera, libro de señales y contadores)."""

    def __init__(self, bt: CopyBacktest, start: int, end: int) -> None:
        self.bt = bt
        self.p = bt.p
        self.start, self.end = start, end
        self.balance: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        self.positions: list[CopyPosition] = []
        self.open: dict[tuple[str, str, str], CopyPosition] = {}  # (libro, wallet, mint)
        self.skipped: dict[str, int] = defaultdict(int)
        self.cash = bt.limits.virtual_capital_usd
        self.peak = self.day_start = self.cash
        self.day: object = None
        self.halted = False
        self.halts: list[tuple[int, str]] = []
        self.curve: list[tuple[int, Decimal]] = []

    # --- Cartera ----------------------------------------------------------------------------

    def state(self) -> BookState:
        held = [p for (book, _, _), p in self.open.items() if book == PORTFOLIO]
        return BookState(
            initial_usd=self.bt.limits.virtual_capital_usd,
            cash_usd=self.cash,
            reserved_usd=ZERO,
            positions_value_usd=sum((p.value() for p in held), ZERO),
            open_positions=len(held),
            open_mints=frozenset(p.mint for p in held),
            peak_equity_usd=self.peak,
            day_start_equity_usd=self.day_start,
        )

    def roll(self, now: int) -> None:
        today = datetime.fromtimestamp(now, UTC).date()
        equity = self.state().equity_usd
        if self.day != today:
            self.day, self.day_start = today, equity
        self.peak = max(self.peak, equity)
        reasons = halt_reasons(self.state(), self.bt.limits)
        if reasons and not self.halted:
            self.halted = True
            self.halts.append((now, "; ".join(d for _, d in reasons)))
        self.curve.append((now, equity))

    # --- Eventos ----------------------------------------------------------------------------

    async def event(self, ev: TraderEvent) -> None:
        key = (ev.wallet, ev.mint)
        before = self.balance[key]
        self.balance[key] += ev.amount if ev.kind in ("buy", "transfer_in") else -ev.amount
        if ev.at < self.start or ev.at >= self.end or ev.kind not in ("buy", "sell"):
            return
        await self.expire(ev.at)
        held = any(p.mint == ev.mint for p in self.open.values())
        if ev.kind == "sell" and not held:
            self.roll(ev.at)
            return  # nada que vender ni que vigilar: ni siquiera se pide el precio
        trader_price = await self.bt._trader_price(ev)
        if trader_price is not None:
            await self.observe(ev.mint, ev.at, trader_price)
        if ev.kind == "buy":
            await self.enter(ev, trader_price)
        else:
            fraction = ONE if before <= 0 else min(ONE, ev.amount / before)
            for book in (PORTFOLIO, SIGNALS):
                pos = self.open.get((book, ev.wallet, ev.mint))
                if pos is not None:
                    await self.close(pos, ev.at, "copia", fraction, theo_price=trader_price)
        self.roll(ev.at)

    async def observe(self, mint: str, at: int, price: Decimal) -> None:
        """Un precio observado del token: valora las posiciones y vigila el stop-loss."""
        threshold = ONE - self.p.stop_loss_pct / 100
        for pos in [p for p in self.open.values() if p.mint == mint]:
            pos.last_price = price
            if pos.value() <= (pos.invested_usd - pos.rent_usd) * threshold:
                await self.close(pos, at, "stop_loss")

    async def expire(self, now: int) -> None:
        """Venta forzada de lo que lleva más de ``max_hold_hours`` abierto."""
        limit = self.p.max_hold_hours * 3600
        for pos in list(self.open.values()):
            if pos.opened_at + limit <= now:
                await self.close(pos, pos.opened_at + limit, "max_hold")

    async def finish(self) -> None:
        await self.expire(self.end)
        for pos in list(self.open.values()):
            await self.close(pos, self.end, "final")
        self.roll(self.end)

    # --- Compras y ventas -------------------------------------------------------------------

    async def enter(self, ev: TraderEvent, trader_price: Decimal | None) -> None:
        bt, p = self.bt, self.p
        quote = await bt._prices.quote(ev.mint, ev.at, p.delay)
        if quote is None:
            self.skipped["sin precio en caché"] += 1
            return
        if quote.price_usd is None:
            self.skipped["nadie operó el token después"] += 1
            return
        price = quote.price_usd
        sol_usd = quote.sol_usd or p.fallback_sol_usd
        fee = bt._tx_fee(sol_usd)
        theo = None if not trader_price else p.size_usd / trader_price
        for book in (PORTFOLIO, SIGNALS):
            existing = self.open.get((book, ev.wallet, ev.mint))
            if book == PORTFOLIO:
                if self.halted:
                    self.skipped["cartera parada por riesgo"] += 1
                    continue
                reasons = entry_rejections(self.state(), ev.mint, p.size_usd, bt.limits)
                if reasons:
                    self.skipped["cartera: " + _reason_head(reasons[0])] += 1
                    continue
            rent = ZERO if existing else bt._lamports(p.rent_lamports, sol_usd)
            swap = p.size_usd - fee - rent
            if swap <= 0:
                self.skipped["comisiones mayores que el tamaño"] += 1
                continue
            tokens = bt._buy_tokens(ev.mint, swap, price)
            if book == PORTFOLIO:
                self.cash -= p.size_usd
            if existing is not None:  # el trader vuelve a comprar (libro de señales)
                existing.tokens += tokens
                existing.invested_usd += p.size_usd
                existing.last_price = price
                if existing.theo_tokens is not None and theo is not None:
                    existing.theo_tokens += theo
                    existing.theo_invested += p.size_usd
                else:
                    existing.theo_tokens = None
                continue
            pos = CopyPosition(
                book=book,
                wallet=ev.wallet,
                mint=ev.mint,
                opened_at=ev.at,
                tokens=tokens,
                invested_usd=p.size_usd,
                trader_entry_price=trader_price,
                entry_price=price,
                rent_usd=rent,
                last_price=price,
                theo_tokens=theo,
                theo_invested=p.size_usd if theo is not None else ZERO,
            )
            self.positions.append(pos)
            self.open[(book, ev.wallet, ev.mint)] = pos

    async def close(
        self,
        pos: CopyPosition,
        at: int,
        reason: str,
        fraction: Decimal = ONE,
        *,
        theo_price: Decimal | None = None,
    ) -> None:
        """Vende ``fraction`` de la posición. La copia teórica vende la misma fracción al
        precio del trader (``theo_price``) o, si no lo hay, al de mercado sin costes."""
        bt, p = self.bt, self.p
        window = p.delay if reason in ("copia", "stop_loss") else (0, 600)
        quote = await bt._prices.quote(pos.mint, at, window)
        price = None if quote is None else quote.price_usd
        whole = fraction >= ONE or (ONE - fraction) <= p.exit_dust_fraction
        if price is None:
            if reason in ("copia", "stop_loss"):
                return  # nadie operó: lo resolverán la venta forzada o el final
            # Nadie compra el token: se da por perdido (sin recuperar la renta).
            pos.exits.append(Exit(at, "perdida", pos.tokens, None, ZERO))
            self._theo_sell(pos, ONE, ZERO)
            pos.tokens = ZERO
            self._closed(pos, at, "written_off")
            return
        sol_usd = (quote.sol_usd if quote and quote.sol_usd else None) or p.fallback_sol_usd
        tokens = pos.tokens if whole else pos.tokens * fraction
        proceeds = bt._sell_usd(pos.mint, tokens, price) - bt._tx_fee(sol_usd)
        if whole:
            proceeds += pos.rent_usd
        pos.proceeds_usd += proceeds
        pos.exits.append(Exit(at, reason, tokens, price, proceeds))
        pos.last_price = price
        pos.tokens -= tokens
        if pos.book == PORTFOLIO:
            self.cash += proceeds
        self._theo_sell(pos, ONE if whole else fraction, theo_price or price)
        if whole:
            pos.tokens = ZERO
            self._closed(pos, at, "closed")

    def _closed(self, pos: CopyPosition, at: int, status: str) -> None:
        pos.status, pos.closed_at = status, at
        self.open.pop((pos.book, pos.wallet, pos.mint), None)

    @staticmethod
    def _theo_sell(pos: CopyPosition, fraction: Decimal, price: Decimal) -> None:
        if pos.theo_tokens is None:
            return
        sold = pos.theo_tokens if fraction >= ONE else pos.theo_tokens * fraction
        pos.theo_proceeds += sold * price
        pos.theo_tokens -= sold


def _reason_head(reason: str) -> str:
    """Motivo de rechazo sin cifras, para contarlo."""
    return re.sub(r"\d[\d.,]*", "N", reason.split(":")[0])[:50]

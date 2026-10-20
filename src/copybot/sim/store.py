"""Persistencia de la simulación: estado, señales, órdenes, posiciones, caja y diario.

Garantías (todas en la base de datos, no solo en el código):

* **Sin duplicados.** Cada orden tiene una clave de idempotencia única; repetir la misma
  decisión no crea otra orden ni vuelve a reservar efectivo.
* **Atomicidad.** Comprobar el estado del sistema y los límites, crear la orden y
  reservar el efectivo ocurren en la misma transacción, con la caja bloqueada.
* **Sin saldos negativos ni ventas de más.** Restricciones ``CHECK`` sobre la caja y
  las posiciones; una venta nunca supera lo que hay en la posición.
* **Doble entrada.** Los apuntes del diario de cada operación suman cero. Todos los
  importes se redondean a 18 decimales (la escala de las columnas) antes de apuntarlos,
  así que lo que se comprueba es exactamente lo que se guarda.
* **Un solo motor a la vez** (``exclusive_run``, bloqueo de PostgreSQL).

Cuentas del diario (en USD): ``cash`` y ``reserved`` (efectivo), ``position`` (coste de
lo que se tiene: lo pagado, la comisión de la compra y la renta de la cuenta de token),
``fees`` (comisiones de las ventas y de las órdenes fallidas), ``pnl`` (resultado: una
ganancia es un apunte negativo) y ``capital`` (aportación inicial; en el libro de
señales, que no tiene límite de capital, sustituye al efectivo). Mientras una posición
está abierta, sus apuntes en ``position`` suman ``cost_usd + rent_usd``; al cerrarla,
cero.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Any

import structlog
from sqlalchemy import and_, case, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.analysis.positions import USD_QUOTES
from copybot.config import RiskLimits
from copybot.db.models import (
    BookCash,
    ControlState,
    LedgerEntry,
    PortfolioSnapshot,
    RawTransaction,
    RiskEvent,
    ServiceHeartbeat,
    Signal,
    SimFill,
    SimOrder,
    SimPosition,
    TokenSafetyCheck,
    TrackedWallet,
)
from copybot.db.models import WalletEvent as WalletEventRow
from copybot.sim.executor import Execution
from copybot.sim.risk import BookState, entry_rejections, halt_reasons

log = structlog.get_logger(__name__)

ZERO = Decimal(0)
PORTFOLIO = "portfolio"
SIGNALS = "signals"
# Escala de las columnas de importes (numeric(38, 18)); PostgreSQL redondea así.
QUANTUM = Decimal("1e-18")
RUN_LOCK_ID = 74_201_810  # pg_advisory_lock del motor de simulación


# Precisión de sobra para 20 cifras enteras y 18 decimales (la del contexto por defecto,
# 28, no llega: un token muy barato puede dar 1e10 unidades o más).
_CONTEXT = Context(prec=60)


def q(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP, context=_CONTEXT)


def position_value(p: SimPosition) -> Decimal:
    """Valor de una posición abierta: lo que daría venderla (con la renta que se
    recupera al cerrar), su coste si aún no se ha valorado, o 0 si no se puede vender."""
    if p.unsellable:
        return ZERO
    return p.last_value_usd if p.last_value_usd is not None else p.cost_usd + p.rent_usd


def fraction_from_payload(payload: dict[str, Any] | None, wallet: str, mint: str) -> Decimal | None:
    """Fracción de sus tokens que vende el trader, con los saldos de la propia
    transacción (antes y después). ``None`` si no se puede saber."""
    meta = (payload or {}).get("meta") or {}

    def total(key: str) -> int | None:
        rows = meta.get(key)
        if rows is None:
            return None
        return sum(
            int((r.get("uiTokenAmount") or {}).get("amount") or 0)
            for r in rows
            if r.get("owner") == wallet and r.get("mint") == mint
        )

    pre, post = total("preTokenBalances"), total("postTokenBalances")
    if pre is None or post is None or pre <= 0:
        return None
    return min(Decimal(1), max(ZERO, Decimal(pre - post) / Decimal(pre)))


def idempotency_key(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


@dataclass(frozen=True)
class EntryResult:
    order_id: int | None
    reasons: list[str] = field(default_factory=list)
    duplicate: bool = False

    @property
    def accepted(self) -> bool:
        return self.order_id is not None and not self.reasons


class RunLock:
    """Bloqueo de PostgreSQL (``pg_advisory_lock``) del motor, en su propia conexión."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._engine = sessions.kw["bind"]
        self._conn: Any = None
        self.held = False

    async def acquire(self) -> bool:
        await self._close()
        conn = await self._engine.connect()
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        got = bool(await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": RUN_LOCK_ID}))
        if got:
            self._conn = conn
        else:
            await conn.close()
        self.held = got
        return got

    async def ensure(self) -> bool:
        """¿Sigue siendo nuestro? Si la conexión se cayó, lo vuelve a pedir."""
        try:
            if self._conn is not None:
                await self._conn.scalar(text("SELECT 1"))
                return True
        except Exception as exc:  # la conexión se ha perdido, y el bloqueo con ella
            log.warning("run_lock_lost", error=type(exc).__name__)
        return await self.acquire()

    async def release(self) -> None:
        if self._conn is not None and self.held:
            with contextlib.suppress(Exception):
                await self._conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": RUN_LOCK_ID})
        await self._close()
        self.held = False

    async def _close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None


class SimStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    # --- Estado ---------------------------------------------------------------------

    async def initialize(self, initial_usd: Decimal) -> None:
        """Crea el estado (RECOVERING) y la caja de la cartera si no existen."""
        now = self._clock()
        async with self._sessions.begin() as s:
            await s.execute(
                insert(ControlState)
                .values(
                    id=1, state="RECOVERING", reason="arranque", updated_by="engine", updated_at=now
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            created = await s.execute(
                insert(BookCash)
                .values(
                    book=PORTFOLIO,
                    initial_usd=initial_usd,
                    cash_usd=initial_usd,
                    reserved_usd=ZERO,
                    peak_equity_usd=initial_usd,
                    day=now.date(),
                    day_start_equity_usd=initial_usd,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["book"])
                .returning(BookCash.book)
            )
            if created.scalar_one_or_none() is not None:
                self._ledger(
                    s,
                    PORTFOLIO,
                    None,
                    None,
                    now,
                    "capital inicial",
                    cash=initial_usd,
                    capital=-initial_usd,
                )

    async def state(self) -> ControlState:
        async with self._sessions() as s:
            row = await s.get(ControlState, 1)
            if row is None:
                raise RuntimeError("estado no inicializado")
            return row

    async def set_state(
        self, state: str, reason: str, by: str, *, only_from: Iterable[str] | None = None
    ) -> bool:
        stmt = update(ControlState).where(ControlState.id == 1)
        if only_from is not None:
            stmt = stmt.where(ControlState.state.in_(list(only_from)))
        stmt = stmt.values(
            state=state, reason=reason[:300], updated_by=by, updated_at=self._clock()
        ).returning(ControlState.id)
        async with self._sessions.begin() as s:
            return (await s.execute(stmt)).scalar_one_or_none() is not None

    async def monitor_heartbeat(self) -> tuple[float | None, dict[str, Any]]:
        """Segundos desde el último latido del monitor y sus detalles."""
        async with self._sessions() as s:
            row = await s.get(ServiceHeartbeat, "monitor")
        if row is None:
            return None, {}
        return (self._clock() - row.beat_at).total_seconds(), dict(row.details or {})

    @asynccontextmanager
    async def exclusive_run(self) -> AsyncIterator[RunLock]:
        """Bloqueo para que solo corra un motor a la vez (``lock.held``: lo tenemos).

        Va en una conexión propia en modo *autocommit* (sin transacción abierta todo el
        rato). Si esa conexión se cae, el bloqueo se pierde: ``lock.ensure()`` lo
        comprueba en cada vuelta y lo vuelve a pedir.
        """
        lock = RunLock(self._sessions)
        await lock.acquire()
        try:
            yield lock
        finally:
            await lock.release()

    # --- Señales --------------------------------------------------------------------

    async def ingest_signals(self, limit: int = 500) -> int:
        """Crea una señal por cada compraventa nueva (confianza alta) de una wallet seguida.

        Solo eventos detectados en vivo o recuperados (no los del historial importado).
        Para las ventas calcula qué fracción de su posición vende el trader: con los saldos
        de la propia transacción si están; si no, con su historial guardado.
        """
        e = WalletEventRow
        stmt = (
            select(e)
            .join(TrackedWallet, and_(TrackedWallet.address == e.wallet, TrackedWallet.active))
            .where(
                e.kind.in_(["buy", "sell"]),
                e.confidence == "high",
                e.detected_via.in_(["live", "backfill"]),
                e.mint.is_not(None),
                ~exists().where(Signal.event_id == e.id),
            )
            .order_by(e.id)
            .limit(limit)
        )
        async with self._sessions.begin() as s:
            events = list(await s.scalars(stmt))
            for ev in events:
                if not ev.token_delta:
                    continue
                fraction = None
                if ev.kind == "sell":
                    payload = await s.scalar(
                        select(RawTransaction.payload).where(
                            RawTransaction.signature == ev.signature
                        )
                    )
                    fraction = fraction_from_payload(payload, ev.wallet, ev.mint)
                if ev.kind == "sell" and fraction is None:
                    held = await s.scalar(
                        select(func.coalesce(func.sum(e.token_delta), 0)).where(
                            e.wallet == ev.wallet,
                            e.mint == ev.mint,
                            e.kind != "ambiguous",
                            (e.slot < ev.slot) | ((e.slot == ev.slot) & (e.id < ev.id)),
                        )
                    )
                    sold = abs(Decimal(ev.token_delta))
                    if held and Decimal(held) > 0:
                        fraction = min(Decimal(1), sold / Decimal(held))
                usd = abs(Decimal(ev.quote_delta)) if ev.quote_symbol in USD_QUOTES else None
                await s.execute(
                    insert(Signal)
                    .values(
                        event_id=ev.id,
                        wallet=ev.wallet,
                        mint=ev.mint,
                        side=ev.kind,
                        trader_token_amount=abs(Decimal(ev.token_delta)),
                        trader_usd=usd,
                        trader_sell_fraction=fraction,
                        block_time=ev.block_time,
                        detected_at=ev.detected_at,
                        detected_via=ev.detected_via,
                    )
                    .on_conflict_do_nothing(index_elements=["event_id"])
                )
        return len(events)

    async def pending_signals(self, books: Iterable[str], limit: int = 50) -> list[Signal]:
        """Señales a las que les falta la decisión de algún libro, de la más antigua."""
        books = list(books)
        missing = [
            ~exists().where(SimOrder.signal_id == Signal.id, SimOrder.book == b) for b in books
        ]
        stmt = select(Signal).where(or_(*missing)).order_by(Signal.id).limit(limit)
        async with self._sessions() as s:
            return list(await s.scalars(stmt))

    async def books_decided(self, signal_id: int) -> set[str]:
        async with self._sessions() as s:
            rows = await s.scalars(select(SimOrder.book).where(SimOrder.signal_id == signal_id))
            return set(rows)

    # --- Órdenes de entrada -----------------------------------------------------------

    async def open_entry(
        self,
        *,
        book: str,
        signal: Signal,
        key: str,
        size: Decimal,
        decision: dict[str, Any],
        limits: RiskLimits | None,
        allowed_states: Iterable[str],
    ) -> EntryResult:
        """Comprueba estado y riesgo, crea la orden y reserva el efectivo, todo a la vez."""
        now = self._clock()
        async with self._sessions.begin() as s:
            state = await s.scalar(
                select(ControlState.state).where(ControlState.id == 1).with_for_update(read=True)
            )
            reasons: list[str] = []
            if state not in set(allowed_states):
                reasons.append(f"el sistema está en estado {state}: no se abren posiciones")
            cash_row = None
            if book == PORTFOLIO and not reasons:
                cash_row = await s.scalar(
                    select(BookCash).where(BookCash.book == PORTFOLIO).with_for_update()
                )
                if cash_row is None or limits is None:
                    raise RuntimeError("cartera no inicializada")
                book_state = await self._book_state(s, cash_row, with_pending=True)
                if cash_row.day != now.date():  # empieza un día nuevo (UTC)
                    cash_row.day = now.date()
                    cash_row.day_start_equity_usd = q(book_state.equity_usd)
                    book_state = await self._book_state(s, cash_row, with_pending=True)
                # Las paradas por pérdida diaria y caída máxima se comprueban aquí también,
                # en la misma transacción: ningún camino (reanudar, reiniciar) se las salta.
                reasons = [
                    f"parada por riesgo: {detail}" for _, detail in halt_reasons(book_state, limits)
                ]
                reasons += entry_rejections(book_state, signal.mint, size, limits)
            status = "rejected" if reasons else "pending"
            reserve = size if (book == PORTFOLIO and status == "pending") else ZERO
            order_id = await s.scalar(
                insert(SimOrder)
                .values(
                    book=book,
                    idempotency_key=key,
                    signal_id=signal.id,
                    side="buy",
                    purpose="entry",
                    mint=signal.mint,
                    wallet=signal.wallet,
                    requested_usd=size,
                    reserved_usd=reserve,
                    status=status,
                    status_reason="; ".join(reasons)[:300],
                    decision=decision | {"riesgo": reasons},
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(SimOrder.id)
            )
            if order_id is None:
                return EntryResult(None, ["decisión ya tomada (idempotencia)"], duplicate=True)
            if reserve and cash_row is not None:
                cash_row.cash_usd -= reserve
                cash_row.reserved_usd += reserve
                cash_row.updated_at = now
                self._ledger(
                    s,
                    book,
                    order_id,
                    None,
                    now,
                    "reserva de la compra",
                    cash=-reserve,
                    reserved=reserve,
                )
            return EntryResult(order_id, reasons)

    async def record_rejection(
        self,
        *,
        book: str,
        signal: Signal,
        key: str,
        side: str,
        purpose: str,
        reasons: list[str],
        decision: dict[str, Any],
    ) -> int | None:
        now = self._clock()
        async with self._sessions.begin() as s:
            return await s.scalar(
                insert(SimOrder)
                .values(
                    book=book,
                    idempotency_key=key,
                    signal_id=signal.id,
                    side=side,
                    purpose=purpose,
                    mint=signal.mint,
                    wallet=signal.wallet,
                    reserved_usd=ZERO,
                    status="rejected",
                    status_reason="; ".join(reasons)[:300],
                    decision=decision | {"motivos": reasons},
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(SimOrder.id)
            )

    async def fill_entry(
        self,
        order_id: int,
        ex: Execution,
        *,
        rent_lamports: int,
        rent_usd: Decimal,
        theoretical_price: Decimal | None,
        theoretical_tokens: Decimal | None = None,
    ) -> int | None:
        """Registra la compra. Devuelve la posición, o ``None`` si la orden ya no estaba
        pendiente (idempotente).

        ``theoretical_*``: la misma compra sin costes, al precio que pagó el trader.
        """
        now = self._clock()
        async with self._sessions.begin() as s:
            order = await s.scalar(
                update(SimOrder)
                .where(SimOrder.id == order_id, SimOrder.status == "pending")
                .values(status="filled", updated_at=now)
                .returning(SimOrder)
            )
            if order is None:
                return None
            tokens, usd, fees, rent_usd = (
                q(ex.token_amount),
                q(ex.usd_amount),
                q(ex.fees_usd),
                q(rent_usd),
            )
            if theoretical_tokens is not None:
                theoretical_tokens = q(theoretical_tokens)
            if theoretical_price is not None:
                theoretical_price = q(theoretical_price)
            cost = usd + fees
            spent = cost + rent_usd
            s.add(
                SimFill(
                    order_id=order_id,
                    token_amount=tokens,
                    usd_amount=usd,
                    fees_usd=fees,
                    price_usd=q(cost / tokens),
                    detail=ex.detail,
                    filled_at=now,
                )
            )
            position = await s.scalar(
                select(SimPosition)
                .where(
                    SimPosition.book == order.book,
                    SimPosition.mint == order.mint,
                    SimPosition.wallet == order.wallet,
                    SimPosition.status == "open",
                )
                .with_for_update()
            )
            if position is None:
                position = SimPosition(
                    book=order.book,
                    mint=order.mint,
                    wallet=order.wallet,
                    status="open",
                    amount=tokens,
                    cost_usd=cost,
                    invested_usd=spent,
                    proceeds_usd=ZERO,
                    realized_pnl_usd=ZERO,
                    rent_lamports=rent_lamports,
                    rent_usd=rent_usd,
                    theoretical_entry_price=theoretical_price,
                    theoretical_amount=theoretical_tokens,
                    theoretical_proceeds_usd=ZERO,
                    unsellable=False,
                    exit_pending=False,
                    opened_at=now,
                    entry_order_id=order_id,
                )
                s.add(position)
            else:  # el trader vuelve a comprar el mismo token (solo en el libro de señales)
                position.amount += tokens
                position.cost_usd += cost
                position.invested_usd += spent
                position.rent_lamports += rent_lamports
                position.rent_usd += rent_usd
                if position.last_value_usd is not None:  # lo nuevo, a su coste hasta valorar
                    position.last_value_usd += spent
                old, old_price = position.theoretical_amount, position.theoretical_entry_price
                if None in (old, old_price, theoretical_tokens, theoretical_price):
                    position.theoretical_amount = position.theoretical_entry_price = None
                else:
                    total = old + theoretical_tokens
                    position.theoretical_entry_price = q(
                        (old * old_price + theoretical_tokens * theoretical_price) / total
                    )
                    position.theoretical_amount = total
            await s.flush()
            order.position_id = position.id
            if order.book == PORTFOLIO:
                cash = await s.scalar(
                    select(BookCash).where(BookCash.book == PORTFOLIO).with_for_update()
                )
                cash.reserved_usd -= order.reserved_usd
                cash.cash_usd += order.reserved_usd - spent
                cash.updated_at = now
                self._ledger(
                    s,
                    order.book,
                    order_id,
                    position.id,
                    now,
                    "compra",
                    reserved=-order.reserved_usd,
                    cash=order.reserved_usd - spent,
                    position=spent,
                )
            else:
                self._ledger(
                    s,
                    order.book,
                    order_id,
                    position.id,
                    now,
                    "compra",
                    capital=-spent,
                    position=spent,
                )
            return position.id

    async def fail_order(self, order_id: int, reason: str, fee_usd: Decimal) -> bool:
        """Marca la orden como fallida, libera la reserva y cobra la comisión pagada."""
        now = self._clock()
        fee_usd = q(fee_usd)
        async with self._sessions.begin() as s:
            order = await s.scalar(
                update(SimOrder)
                .where(SimOrder.id == order_id, SimOrder.status == "pending")
                .values(status="failed", status_reason=reason[:300], updated_at=now)
                .returning(SimOrder)
            )
            if order is None:
                return False
            if order.book == PORTFOLIO:
                cash = await s.scalar(
                    select(BookCash).where(BookCash.book == PORTFOLIO).with_for_update()
                )
                cash.reserved_usd -= order.reserved_usd
                cash.cash_usd += order.reserved_usd - fee_usd
                cash.updated_at = now
                self._ledger(
                    s,
                    order.book,
                    order_id,
                    order.position_id,
                    now,
                    "orden fallida",
                    reserved=-order.reserved_usd,
                    cash=order.reserved_usd - fee_usd,
                    fees=fee_usd,
                )
            elif fee_usd:
                self._ledger(
                    s,
                    order.book,
                    order_id,
                    order.position_id,
                    now,
                    "orden fallida",
                    capital=-fee_usd,
                    fees=fee_usd,
                )
            if order.side == "sell" and order.position_id is not None:
                await s.execute(
                    update(SimPosition)
                    .where(SimPosition.id == order.position_id)
                    .values(last_exit_attempt_at=now)
                )
            return True

    async def recover_pending(
        self, reason: str = "interrumpida por un reinicio: se da por no ejecutada"
    ) -> int:
        """Las órdenes a medias se dan por fallidas (no se ejecutaron). Al arrancar, o al
        empezar cada vuelta del motor: entre vuelta y vuelta no queda ninguna en curso."""
        async with self._sessions() as s:
            pending = list(await s.scalars(select(SimOrder.id).where(SimOrder.status == "pending")))
        for order_id in pending:
            await self.fail_order(order_id, reason, ZERO)
        return len(pending)

    # --- Salidas -------------------------------------------------------------------------

    async def open_exit(
        self,
        *,
        position_id: int,
        key: str,
        amount: Decimal,
        purpose: str,
        signal_id: int | None,
        decision: dict[str, Any],
    ) -> int | None:
        now = self._clock()
        async with self._sessions.begin() as s:
            pos = await s.scalar(
                select(SimPosition).where(SimPosition.id == position_id).with_for_update()
            )
            if pos is None or pos.status != "open" or amount <= 0:
                return None
            order_id = await s.scalar(
                insert(SimOrder)
                .values(
                    book=pos.book,
                    idempotency_key=key,
                    signal_id=signal_id,
                    position_id=position_id,
                    side="sell",
                    purpose=purpose,
                    mint=pos.mint,
                    wallet=pos.wallet,
                    requested_amount=min(amount, pos.amount),
                    reserved_usd=ZERO,
                    status="pending",
                    decision=decision,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(SimOrder.id)
            )
            if order_id is not None:
                pos.exit_pending = True
                pos.exit_purpose = purpose
                pos.last_exit_attempt_at = now
            return order_id

    async def fill_exit(
        self,
        order_id: int,
        ex: Execution,
        *,
        rent_refund_usd: Decimal,
        theoretical_price: Decimal | None = None,
    ) -> bool:
        """Registra la venta. Nunca vende más de lo que hay; si no queda nada, cierra la
        posición y recupera la renta de la cuenta de token.

        La copia teórica vende la misma fracción de sus tokens a ``theoretical_price``
        (sin precio, la copia teórica de esta posición deja de ser comparable).
        """
        now = self._clock()
        async with self._sessions.begin() as s:
            order = await s.scalar(
                update(SimOrder)
                .where(SimOrder.id == order_id, SimOrder.status == "pending")
                .values(status="filled", updated_at=now)
                .returning(SimOrder)
            )
            if order is None:
                return False
            pos = await s.scalar(
                select(SimPosition).where(SimPosition.id == order.position_id).with_for_update()
            )
            before = pos.amount
            sold = min(q(ex.token_amount), before)
            if sold <= 0:
                raise RuntimeError(f"venta sin tokens en la posición {pos.id}")
            closing = sold == before
            cost_part = pos.cost_usd if closing else q(pos.cost_usd * sold / before)
            if pos.theoretical_amount is not None and theoretical_price is not None:
                theo_sold = (
                    pos.theoretical_amount if closing else q(pos.theoretical_amount * sold / before)
                )
                pos.theoretical_amount -= theo_sold
                pos.theoretical_proceeds_usd += q(theo_sold * theoretical_price)
            else:
                pos.theoretical_amount = None
            proceeds, fees, refund = q(ex.usd_amount), q(ex.fees_usd), q(rent_refund_usd)
            s.add(
                SimFill(
                    order_id=order_id,
                    token_amount=sold,
                    usd_amount=proceeds,
                    fees_usd=fees,
                    price_usd=q(proceeds / sold),
                    detail=ex.detail,
                    filled_at=now,
                )
            )
            pos.amount -= sold
            pos.cost_usd -= cost_part
            pos.proceeds_usd += proceeds
            pos.exit_pending = False
            pos.exit_purpose = None
            cash_in = proceeds - fees
            position_out = cost_part
            if closing:
                pos.status = "closed"
                pos.closed_at = now
                cash_in += refund
                position_out += pos.rent_usd
            elif pos.last_value_usd is not None:
                # Lo vendido ya no está en la posición: su valor baja en proporción (la
                # renta se recupera entera al final).
                tokens_value = max(pos.last_value_usd - refund, ZERO)
                pos.last_value_usd = refund + q(tokens_value * pos.amount / before)
            pnl = cash_in - position_out  # resultado de esta venta (comisión incluida)
            pos.realized_pnl_usd += pnl
            entries = {
                "cash": cash_in,
                "position": -position_out,
                "fees": fees,
                "pnl": -(pnl + fees),
            }
            if pos.book == PORTFOLIO:
                cash = await s.scalar(
                    select(BookCash).where(BookCash.book == PORTFOLIO).with_for_update()
                )
                cash.cash_usd += entries["cash"]
                cash.updated_at = now
            else:
                entries["capital"] = entries.pop("cash")
            self._ledger(s, pos.book, order_id, pos.id, now, f"venta ({order.purpose})", **entries)
            return True

    # --- Posiciones y valoración --------------------------------------------------------

    async def open_positions(self, book: str | None = None) -> list[SimPosition]:
        stmt = select(SimPosition).where(SimPosition.status == "open")
        if book is not None:
            stmt = stmt.where(SimPosition.book == book)
        async with self._sessions() as s:
            return list(await s.scalars(stmt.order_by(SimPosition.id)))

    async def position_for(self, book: str, mint: str, wallet: str) -> SimPosition | None:
        async with self._sessions() as s:
            return await s.scalar(
                select(SimPosition).where(
                    SimPosition.book == book,
                    SimPosition.mint == mint,
                    SimPosition.wallet == wallet,
                    SimPosition.status == "open",
                )
            )

    async def exit_attempts(self, position_id: int, purpose: str) -> int:
        async with self._sessions() as s:
            return int(
                await s.scalar(
                    select(func.count()).where(
                        SimOrder.position_id == position_id, SimOrder.purpose == purpose
                    )
                )
                or 0
            )

    async def last_exit_order(self, position_id: int) -> SimOrder | None:
        async with self._sessions() as s:
            return await s.scalar(
                select(SimOrder)
                .where(SimOrder.position_id == position_id, SimOrder.side == "sell")
                .order_by(SimOrder.id.desc())
                .limit(1)
            )

    async def consecutive_exit_failures(self, position_id: int) -> int:
        """Ventas fallidas seguidas desde la última que salió bien."""
        async with self._sessions() as s:
            last_ok = await s.scalar(
                select(func.max(SimOrder.id)).where(
                    SimOrder.position_id == position_id,
                    SimOrder.side == "sell",
                    SimOrder.status == "filled",
                )
            )
            stmt = select(func.count()).where(
                SimOrder.position_id == position_id,
                SimOrder.side == "sell",
                SimOrder.status == "failed",
            )
            if last_ok is not None:
                stmt = stmt.where(SimOrder.id > last_ok)
            return int(await s.scalar(stmt) or 0)

    async def has_pending_order(self, position_id: int) -> bool:
        async with self._sessions() as s:
            return bool(
                await s.scalar(
                    select(
                        exists().where(
                            SimOrder.position_id == position_id, SimOrder.status == "pending"
                        )
                    )
                )
            )

    async def stale_positions(self, before: datetime, limit: int) -> list[SimPosition]:
        """Posiciones abiertas sin valorar desde ``before`` (las más antiguas primero)."""
        stmt = (
            select(SimPosition)
            .where(
                SimPosition.status == "open",
                (SimPosition.last_valued_at.is_(None)) | (SimPosition.last_valued_at < before),
            )
            .order_by(
                # Primero la cartera: su riesgo no puede esperar a las del libro de señales.
                case((SimPosition.book == PORTFOLIO, 0), else_=1),
                SimPosition.last_valued_at.asc().nulls_first(),
                SimPosition.id,
            )
            .limit(limit)
        )
        async with self._sessions() as s:
            return list(await s.scalars(stmt))

    async def mark_value(self, position_id: int, value: Decimal, unsellable: bool) -> None:
        async with self._sessions.begin() as s:
            await s.execute(
                update(SimPosition)
                .where(SimPosition.id == position_id)
                .values(last_value_usd=value, last_valued_at=self._clock(), unsellable=unsellable)
            )

    async def book_state(self, book: str = PORTFOLIO) -> BookState:
        async with self._sessions() as s:
            cash = await s.scalar(select(BookCash).where(BookCash.book == book))
            if cash is None:
                raise RuntimeError("cartera no inicializada")
            return await self._book_state(s, cash)

    async def _book_state(
        self, s: AsyncSession, cash: BookCash, *, with_pending: bool = False
    ) -> BookState:
        """Estado del libro. Con ``with_pending``, las compras en curso cuentan ya como
        posiciones (para que dos órdenes simultáneas no superen los límites)."""
        positions = list(
            await s.scalars(
                select(SimPosition).where(
                    SimPosition.book == cash.book, SimPosition.status == "open"
                )
            )
        )
        value = sum((position_value(p) for p in positions), ZERO)
        mints = [p.mint for p in positions]
        if with_pending:
            mints += list(
                await s.scalars(
                    select(SimOrder.mint).where(
                        SimOrder.book == cash.book,
                        SimOrder.purpose == "entry",
                        SimOrder.status == "pending",
                    )
                )
            )
        return BookState(
            initial_usd=cash.initial_usd,
            cash_usd=cash.cash_usd,
            reserved_usd=cash.reserved_usd,
            positions_value_usd=value,
            open_positions=len(mints),
            open_mints=frozenset(mints),
            peak_equity_usd=cash.peak_equity_usd,
            day_start_equity_usd=cash.day_start_equity_usd,
        )

    async def roll_equity(self, book: str = PORTFOLIO) -> BookState:
        """Actualiza el máximo de patrimonio y, si cambia el día (UTC), el de inicio."""
        now = self._clock()
        async with self._sessions.begin() as s:
            cash = await s.scalar(select(BookCash).where(BookCash.book == book).with_for_update())
            state = await self._book_state(s, cash)
            equity = state.equity_usd
            if cash.day != now.date():
                cash.day = now.date()
                cash.day_start_equity_usd = equity
            cash.peak_equity_usd = max(cash.peak_equity_usd, equity)
            cash.updated_at = now
            return await self._book_state(s, cash)

    async def snapshot(self, book: str, state: BookState, details: dict[str, Any]) -> None:
        async with self._sessions.begin() as s:
            s.add(
                PortfolioSnapshot(
                    book=book,
                    at=self._clock(),
                    cash_usd=state.cash_usd + state.reserved_usd,
                    positions_value_usd=state.positions_value_usd,
                    equity_usd=state.equity_usd,
                    open_positions=state.open_positions,
                    details=details,
                )
            )

    async def risk_event(self, kind: str, book: str | None, details: dict[str, Any]) -> None:
        async with self._sessions.begin() as s:
            s.add(RiskEvent(at=self._clock(), book=book, kind=kind, details=details))

    # --- Filtros de seguridad (caché) ----------------------------------------------------

    async def latest_safety_check(self, mint: str, since: datetime) -> TokenSafetyCheck | None:
        async with self._sessions() as s:
            return await s.scalar(
                select(TokenSafetyCheck)
                .where(TokenSafetyCheck.mint == mint, TokenSafetyCheck.checked_at >= since)
                .order_by(TokenSafetyCheck.checked_at.desc())
                .limit(1)
            )

    async def save_safety_check(
        self, mint: str, passed: bool, reasons: list[str], details: dict[str, Any]
    ) -> None:
        async with self._sessions.begin() as s:
            s.add(
                TokenSafetyCheck(
                    mint=mint,
                    checked_at=self._clock(),
                    passed=passed,
                    reasons=reasons,
                    details=details,
                )
            )

    # --- Diario --------------------------------------------------------------------------

    @staticmethod
    def _ledger(
        s: AsyncSession,
        book: str,
        order_id: int | None,
        position_id: int | None,
        at: datetime,
        memo: str,
        **entries: Decimal,
    ) -> None:
        total = sum(entries.values(), ZERO)
        if total != 0 or any(q(v) != v for v in entries.values()):
            raise AssertionError(f"apuntes descuadrados o sin redondear ({memo}): {entries}")
        for account, delta in entries.items():
            if delta:
                s.add(
                    LedgerEntry(
                        book=book,
                        order_id=order_id,
                        position_id=position_id,
                        account=account,
                        delta_usd=delta,
                        memo=memo,
                        at=at,
                    )
                )

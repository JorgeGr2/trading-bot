"""Persistencia de la simulación: estado, señales, órdenes, posiciones, caja y diario.

Garantías (todas en la base de datos, no solo en el código):

* **Sin duplicados.** Cada orden tiene una clave de idempotencia única; repetir la misma
  decisión no crea otra orden ni vuelve a reservar efectivo.
* **Atomicidad.** Comprobar el estado del sistema y los límites, crear la orden y
  reservar el efectivo ocurren en la misma transacción, con la caja bloqueada.
* **Sin saldos negativos ni ventas de más.** Restricciones ``CHECK`` sobre la caja y
  las posiciones; una venta nunca supera lo que hay en la posición.
* **Doble entrada.** Los apuntes del diario de cada operación suman cero.

Cuentas del diario (en USD): ``cash`` y ``reserved`` (efectivo), ``position`` (coste de
lo que se tiene: lo pagado, la comisión de la compra y la renta de la cuenta de token),
``fees`` (comisiones de las ventas y de las órdenes fallidas), ``pnl`` (resultado: una
ganancia es un apunte negativo) y ``capital`` (aportación inicial; en el libro de
señales, que no tiene límite de capital, sustituye al efectivo). Mientras una posición
está abierta, sus apuntes en ``position`` suman ``cost_usd + rent_usd``; al cerrarla,
cero.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.analysis.positions import USD_QUOTES
from copybot.config import RiskLimits
from copybot.db.models import (
    BookCash,
    ControlState,
    LedgerEntry,
    PortfolioSnapshot,
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
from copybot.sim.risk import BookState, entry_rejections

ZERO = Decimal(0)
PORTFOLIO = "portfolio"
SIGNALS = "signals"


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

    async def monitor_heartbeat_age(self) -> float | None:
        async with self._sessions() as s:
            beat = await s.scalar(
                select(ServiceHeartbeat.beat_at).where(ServiceHeartbeat.service == "monitor")
            )
        return None if beat is None else (self._clock() - beat).total_seconds()

    # --- Señales --------------------------------------------------------------------

    async def ingest_signals(self, limit: int = 500) -> int:
        """Crea una señal por cada compraventa nueva (confianza alta) de una wallet seguida.

        Solo eventos detectados en vivo o recuperados (no los del historial importado).
        Para las ventas calcula qué fracción de su posición vende el trader.
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
                reasons = entry_rejections(
                    await self._book_state(s, cash_row, with_pending=True),
                    signal.mint,
                    size,
                    limits,
                )
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
    ) -> int | None:
        """Registra la compra. Devuelve la posición, o ``None`` si la orden ya no estaba
        pendiente (idempotente)."""
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
            s.add(
                SimFill(
                    order_id=order_id,
                    token_amount=ex.token_amount,
                    usd_amount=ex.usd_amount,
                    fees_usd=ex.fees_usd,
                    price_usd=(ex.usd_amount + ex.fees_usd) / ex.token_amount,
                    detail=ex.detail,
                    filled_at=now,
                )
            )
            cost = ex.usd_amount + ex.fees_usd
            spent = cost + rent_usd
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
                    amount=ex.token_amount,
                    cost_usd=cost,
                    invested_usd=spent,
                    proceeds_usd=ZERO,
                    realized_pnl_usd=ZERO,
                    rent_lamports=rent_lamports,
                    rent_usd=rent_usd,
                    theoretical_entry_price=theoretical_price,
                    theoretical_proceeds_usd=ZERO,
                    unsellable=False,
                    exit_pending=False,
                    opened_at=now,
                    entry_order_id=order_id,
                )
                s.add(position)
            else:  # el trader vuelve a comprar el mismo token (solo en el libro de señales)
                position.amount += ex.token_amount
                position.cost_usd += cost
                position.invested_usd += spent
                position.rent_lamports += rent_lamports
                position.rent_usd += rent_usd
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

    async def recover_pending(self) -> int:
        """Tras un reinicio: las órdenes a medias se dan por fallidas (no se ejecutaron)."""
        async with self._sessions() as s:
            pending = list(await s.scalars(select(SimOrder.id).where(SimOrder.status == "pending")))
        for order_id in pending:
            await self.fail_order(
                order_id, "interrumpida por un reinicio: se da por no ejecutada", ZERO
            )
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
        theoretical_proceeds_usd: Decimal = ZERO,
    ) -> bool:
        """Registra la venta. Nunca vende más de lo que hay; si no queda nada, cierra la
        posición y recupera la renta de la cuenta de token."""
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
            sold = min(ex.token_amount, pos.amount)
            if sold <= 0:
                raise RuntimeError(f"venta sin tokens en la posición {pos.id}")
            cost_part = pos.cost_usd * sold / pos.amount
            proceeds, fees = ex.usd_amount, ex.fees_usd
            s.add(
                SimFill(
                    order_id=order_id,
                    token_amount=sold,
                    usd_amount=proceeds,
                    fees_usd=fees,
                    price_usd=proceeds / sold,
                    detail=ex.detail,
                    filled_at=now,
                )
            )
            pos.amount -= sold
            pos.cost_usd -= cost_part
            pos.proceeds_usd += proceeds
            pos.realized_pnl_usd += proceeds - fees - cost_part
            pos.theoretical_proceeds_usd += theoretical_proceeds_usd
            pos.exit_pending = False
            pos.exit_purpose = None
            entries = {
                "cash": proceeds - fees,
                "position": -cost_part,
                "fees": fees,
                "pnl": -(proceeds - cost_part),
            }
            if pos.amount == 0:
                pos.status = "closed"
                pos.closed_at = now
                pos.cost_usd = ZERO
                pos.realized_pnl_usd += rent_refund_usd - pos.rent_usd
                entries["cash"] += rent_refund_usd
                entries["position"] -= pos.rent_usd
                entries["pnl"] -= rent_refund_usd - pos.rent_usd
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
        value = sum(
            (
                ZERO
                if p.unsellable
                else (p.last_value_usd if p.last_value_usd is not None else p.cost_usd)
                for p in positions
            ),
            ZERO,
        )
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
        if total != 0:
            raise AssertionError(f"apuntes descuadrados ({memo}): {entries}")
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

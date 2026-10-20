"""Motor de la simulación con un mercado de mentira y PostgreSQL de verdad."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from copybot.config import RiskLimits
from copybot.db.models import (
    BookCash,
    LedgerEntry,
    RawTransaction,
    RiskEvent,
    ServiceHeartbeat,
    SimOrder,
    SimPosition,
    TrackedWallet,
    WalletEvent,
)
from copybot.db.session import make_sessionmaker
from copybot.sim.engine import Engine
from copybot.sim.executor import SimExecutor
from copybot.sim.params import EngineParams, SafetyParams
from copybot.sim.quotes import Quote
from copybot.sim.safety import MintInfo
from copybot.sim.store import PORTFOLIO, SIGNALS, SimStore
from copybot.solana.constants import USDC_MINT

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
TRADER = "Trader1111111111111111111111111111111111111"


def mint(i: int) -> str:
    return f"Mint{i:040d}"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class FakeQuotes:
    """Pool con precio fijo por token (USD por token, 6 decimales) e impacto opcional."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.price: dict[str, Decimal] = {}
        self.impact_pct: dict[str, Decimal] = {}
        self.no_route: set[str] = set()
        self.calls = 0

    async def quote(self, input_mint, output_mint, amount_raw, slippage_bps):
        self.calls += 1
        token = output_mint if input_mint == USDC_MINT else input_mint
        if token in self.no_route:
            return None
        price = self.price[token]
        impact = self.impact_pct.get(token, Decimal("0.2"))
        keep = 1 - impact / 100
        amount = Decimal(amount_raw)
        out = amount / price * keep if input_mint == USDC_MINT else amount * price * keep
        return Quote(input_mint, output_mint, amount_raw, int(out), impact, ("Fake",), self.clock())


class FakeMarket:
    def __init__(self) -> None:
        self.sol = Decimal(200)
        self.liquidity: dict[str, Decimal] = {}
        self.info: dict[str, MintInfo] = {}

    async def sol_usd(self):
        return self.sol

    async def liquidity_usd(self, m):
        return self.liquidity.get(m)

    async def mint_info(self, m):
        return self.info.get(m)


class World:
    def __init__(self, engine) -> None:
        self.sessions = make_sessionmaker(engine)
        self.clock = Clock()
        self.quotes = FakeQuotes(self.clock)
        self.market = FakeMarket()
        self.n = 0
        self.moves: list[tuple[str, Decimal]] = []  # cambios de precio durante la latencia

    def token(self, i: int, price: str = "0.01", **info) -> str:
        m = mint(i)
        self.quotes.price[m] = Decimal(price)
        self.market.liquidity[m] = Decimal(500_000)
        self.market.info[m] = MintInfo(
            mint=m,
            program=info.get("program", "spl-token"),
            decimals=6,
            mint_authority=info.get("mint_authority"),
            freeze_authority=None,
            extensions=info.get("extensions", {}),
        )
        return m

    async def latency(self, seconds: float) -> None:
        self.clock.advance(seconds=seconds)
        for m, factor in self.moves:
            self.quotes.price[m] *= factor
        self.moves.clear()

    def build(self, **params) -> Engine:
        store = SimStore(self.sessions, clock=self.clock)
        p = EngineParams(**params)
        executor = SimExecutor(
            self.quotes,
            latency_ms=p.latency_ms,
            slippage_tolerance_bps=p.slippage_tolerance_bps,
            sleep=self.latency,
        )
        return Engine(
            store,
            self.market,
            self.quotes,
            executor,
            params=p,
            safety=SafetyParams(),
            limits=RiskLimits(),
            clock=self.clock,
            sleep=self.latency,
        )

    async def heartbeat(self, ws_connected: bool = True) -> None:
        details = {"ws_connected": ws_connected}
        async with self.sessions.begin() as s:
            await s.execute(
                insert(ServiceHeartbeat)
                .values(service="monitor", beat_at=self.clock(), details=details)
                .on_conflict_do_update(
                    index_elements=["service"], set_={"beat_at": self.clock(), "details": details}
                )
            )

    async def follow(self, wallet: str = TRADER) -> None:
        async with self.sessions.begin() as s:
            s.add(TrackedWallet(address=wallet, source="manual", active=True))

    async def trade(
        self,
        kind: str,
        m: str,
        tokens: str,
        usd: str,
        *,
        age_s: float = 2,
        via: str = "live",
        wallet: str = TRADER,
    ) -> None:
        self.n += 1
        sig = f"sig{self.n:085d}"
        when = self.clock() - timedelta(seconds=age_s)
        delta = Decimal(tokens) if kind == "buy" else -Decimal(tokens)
        async with self.sessions.begin() as s:
            s.add(
                RawTransaction(
                    signature=sig,
                    slot=self.n,
                    block_time=when,
                    fee_payer=wallet,
                    via_fomo=True,
                    succeeded=True,
                    source="test",
                    detected_via=via,
                    first_seen_at=self.clock(),
                    fetched_at=self.clock(),
                    payload={},
                )
            )
            await s.flush()
            s.add(
                WalletEvent(
                    signature=sig,
                    wallet=wallet,
                    kind=kind,
                    confidence="high",
                    mint=m,
                    token_delta=delta,
                    quote_symbol="USDC",
                    quote_delta=-Decimal(usd) if kind == "buy" else Decimal(usd),
                    wallet_is_signer=True,
                    via_fomo=True,
                    reasons=[],
                    slot=self.n,
                    block_time=when,
                    detected_via=via,
                    detected_at=self.clock(),
                    classifier_version="2",
                )
            )

    async def orders(self, **where) -> list[SimOrder]:
        async with self.sessions() as s:
            stmt = select(SimOrder).order_by(SimOrder.id)
            for k, v in where.items():
                stmt = stmt.where(getattr(SimOrder, k) == v)
            return list(await s.scalars(stmt))

    async def positions(self, book: str) -> list[SimPosition]:
        async with self.sessions() as s:
            return list(
                await s.scalars(
                    select(SimPosition).where(SimPosition.book == book).order_by(SimPosition.id)
                )
            )

    async def cash(self) -> BookCash:
        async with self.sessions() as s:
            return await s.scalar(select(BookCash).where(BookCash.book == PORTFOLIO))

    async def ledger_is_balanced(self) -> bool:
        async with self.sessions() as s:
            total = await s.scalar(select(func.coalesce(func.sum(LedgerEntry.delta_usd), 0)))
            row = await s.scalar(select(BookCash))
            cash = await s.scalar(
                select(func.coalesce(func.sum(LedgerEntry.delta_usd), 0)).where(
                    LedgerEntry.book == PORTFOLIO, LedgerEntry.account == "cash"
                )
            )
        return total == 0 and cash == row.cash_usd


@pytest.fixture
async def world(engine) -> World:
    w = World(engine)
    await w.follow()
    await w.heartbeat()
    return w


async def test_buy_signal_is_copied_in_both_books_with_all_costs(world):
    m = world.token(1)
    eng = world.build()
    await eng.start()
    await world.trade("buy", m, "50000", "500")
    await eng.tick()

    orders = await world.orders(purpose="entry")
    assert {(o.book, o.status) for o in orders} == {(PORTFOLIO, "filled"), (SIGNALS, "filled")}
    (pos,) = await world.positions(PORTFOLIO)
    fee = Decimal(105_000) / Decimal(10**9) * 200  # base + prioritaria, a 200 $/SOL
    rent = Decimal(1_488_440) / Decimal(10**9) * 200
    swap = (Decimal(10) - fee - rent).quantize(Decimal("0.000001"))
    # 0,2 % de impacto, 0,5 % de MEV; 0,01 $ por token.
    expected = (
        (swap * 10**6 / Decimal("0.01") * Decimal("0.998")).to_integral_value(rounding="ROUND_DOWN")
        / 10**6
        * Decimal("0.995")
    )
    assert pos.amount == expected
    assert pos.cost_usd == swap + fee and pos.rent_usd == rent
    assert pos.theoretical_entry_price == Decimal("0.01")
    assert pos.theoretical_amount == 1000
    cash = await world.cash()
    assert cash.cash_usd == Decimal(100) - swap - fee - rent and cash.reserved_usd == 0
    assert orders[0].decision["mercado"]["ruta"] == ["Fake"]
    assert await world.ledger_is_balanced()


async def test_old_or_backfilled_signal_is_rejected(world):
    m = world.token(1)
    eng = world.build()
    await eng.start()
    await world.trade("buy", m, "50000", "500", age_s=300, via="backfill")
    await eng.tick()
    orders = await world.orders()
    assert len(orders) == 2 and all(o.status == "rejected" for o in orders)
    assert "oportunidad ya pasó" in orders[0].status_reason
    assert "recuperada" in orders[0].status_reason
    assert world.quotes.calls == 0  # ni siquiera se cotiza


async def test_unsafe_token_and_thin_market_are_rejected(world):
    bad = world.token(1, mint_authority="Creator11111111111111111111111111111111111")
    thin = world.token(2)
    world.market.liquidity[thin] = Decimal(5_000)
    eng = world.build()
    await eng.start()
    await world.trade("buy", bad, "50000", "500")
    await world.trade("buy", thin, "50000", "500")
    await eng.tick()
    reasons = {o.mint: o.status_reason for o in await world.orders(book=PORTFOLIO)}
    assert "mint authority" in reasons[bad]
    assert "liquidez de 5,000 $" in reasons[thin]
    assert (await world.cash()).cash_usd == 100


async def test_slippage_fails_the_order_and_pays_the_fee(world):
    m = world.token(1)
    eng = world.build()
    await eng.start()
    await world.trade("buy", m, "50000", "500")
    world.moves.append((m, Decimal("1.05")))  # el precio sube un 5 % durante la latencia
    await eng.tick()
    orders = await world.orders(purpose="entry")
    assert all(o.status == "failed" and "slippage" in o.status_reason for o in orders)
    fee = Decimal(105_000) / Decimal(10**9) * 200
    cash = await world.cash()
    assert cash.cash_usd == Decimal(100) - fee and cash.reserved_usd == 0
    assert await world.positions(PORTFOLIO) == []
    assert await world.ledger_is_balanced()


async def test_copy_exit_is_proportional_and_closes_with_rent_refund(world):
    m = world.token(1)
    eng = world.build()
    await eng.start()
    await world.trade("buy", m, "50000", "500")
    await eng.tick()
    (pos,) = await world.positions(PORTFOLIO)
    held = pos.amount

    world.quotes.price[m] = Decimal("0.02")  # se dobla
    await world.trade("sell", m, "25000", "500")  # el trader vende la mitad
    await eng.tick()
    (pos,) = await world.positions(PORTFOLIO)
    assert pos.status == "open" and pos.amount == held / 2

    await world.trade("sell", m, "25000", "500")  # y el resto
    await eng.tick()
    (pos,) = await world.positions(PORTFOLIO)
    assert pos.status == "closed" and pos.amount == 0
    assert pos.realized_pnl_usd > Decimal("9")  # ~+10 $ menos costes
    # Copia teórica: 1.000 tokens comprados a 0,01 y vendidos a 0,02.
    assert pos.theoretical_proceeds_usd == 20
    cash = await world.cash()
    assert cash.cash_usd == 100 + pos.realized_pnl_usd
    assert await world.ledger_is_balanced()


async def test_sell_without_position_is_recorded_not_traded(world):
    m = world.token(1)
    eng = world.build()
    await eng.start()
    await world.trade("sell", m, "100", "1")
    await eng.tick()
    orders = await world.orders()
    assert {o.status for o in orders} == {"rejected"}
    assert "no tenemos posición" in orders[0].status_reason


async def test_stop_loss_and_max_hold(world):
    a, b = world.token(1), world.token(2)
    eng = world.build()
    await eng.start()
    await world.trade("buy", a, "50000", "500")
    await world.trade("buy", b, "50000", "500")
    await eng.tick()
    world.quotes.price[a] = Decimal("0.004")  # −60 %
    world.clock.advance(seconds=61)
    await world.heartbeat()
    await eng.tick()
    exits = {o.mint: o.purpose for o in await world.orders(side="sell", book=PORTFOLIO)}
    assert exits == {a: "stop_loss"}

    world.clock.advance(hours=49)
    await world.heartbeat()
    await eng.tick()
    exits = {o.mint: o.purpose for o in await world.orders(side="sell", book=PORTFOLIO)}
    assert exits == {a: "stop_loss", b: "max_hold"}
    assert all(p.status == "closed" for p in await world.positions(PORTFOLIO))
    assert await world.ledger_is_balanced()


async def test_position_limit_applies_to_portfolio_only(world):
    eng = world.build()
    await eng.start()
    for i in range(1, 8):
        await world.trade("buy", world.token(i), "50000", "500")
    await eng.tick()
    portfolio = await world.orders(book=PORTFOLIO)
    assert sum(o.status == "filled" for o in portfolio) == 5
    rejected = [o for o in portfolio if o.status == "rejected"]
    assert len(rejected) == 2 and "posiciones abiertas" in rejected[0].status_reason
    assert len(await world.positions(SIGNALS)) == 7
    cash = await world.cash()
    assert cash.cash_usd >= Decimal(100) * RiskLimits().min_cash_reserve_pct / 100


async def test_drawdown_halts_portfolio_but_signals_book_keeps_measuring(world):
    eng = world.build(stop_loss_pct=Decimal(100))
    await eng.start()
    for i in range(1, 6):
        await world.trade("buy", world.token(i), "50000", "500")
    await eng.tick()
    for i in range(1, 6):
        world.quotes.price[mint(i)] = Decimal("0.004")  # −60 % en las 5 posiciones
    world.clock.advance(seconds=61)
    await world.heartbeat()
    await eng.tick()
    state = await eng.store.state()
    assert state.state == "HALTED_RISK" and "caída" in state.reason
    async with world.sessions() as s:
        kinds = set(await s.scalars(select(RiskEvent.kind)))
    assert "max_drawdown" in kinds

    await world.trade("buy", world.token(9), "50000", "500")
    await eng.tick()
    new = [o for o in await world.orders(purpose="entry") if o.mint == mint(9)]
    by_book = {o.book: o for o in new}
    assert by_book[PORTFOLIO].status == "rejected"
    assert "HALTED_RISK" in by_book[PORTFOLIO].status_reason
    assert by_book[SIGNALS].status == "filled"
    # Reiniciar no reanuda: hace falta una decisión humana.
    await world.build().start()
    assert (await eng.store.state()).state == "HALTED_RISK"


async def test_stale_monitor_switches_to_read_only_and_back(world):
    m = world.token(1)
    eng = world.build()
    await eng.start()
    world.clock.advance(seconds=300)
    await world.trade("buy", m, "50000", "500")
    await eng.tick()
    assert (await eng.store.state()).state == "READ_ONLY"
    assert all("READ_ONLY" in o.status_reason for o in await world.orders())
    await world.heartbeat()
    await eng.tick()
    assert (await eng.store.state()).state == "RUNNING"


async def test_pause_blocks_entries_but_not_exits(world):
    m = world.token(1)
    eng = world.build()
    await eng.start()
    await world.trade("buy", m, "50000", "500")
    await eng.tick()
    await eng.store.set_state("PAUSED", "manual", "cli")
    await world.trade("buy", world.token(2), "50000", "500")
    await world.trade("sell", m, "50000", "500")
    await eng.tick()
    statuses = {(o.mint, o.side, o.book): o.status for o in await world.orders()}
    assert statuses[(mint(2), "buy", PORTFOLIO)] == "rejected"
    assert statuses[(m, "sell", PORTFOLIO)] == "filled"


async def test_unsellable_position_is_valued_at_zero_and_retried(world):
    m = world.token(1)
    eng = world.build()
    await eng.start()
    await world.trade("buy", m, "50000", "500")
    await eng.tick()
    world.quotes.no_route.add(m)
    await world.trade("sell", m, "50000", "500")
    await eng.tick()
    (pos,) = await world.positions(PORTFOLIO)
    assert pos.status == "open" and pos.unsellable and pos.exit_pending
    world.quotes.no_route.clear()
    world.clock.advance(seconds=31)
    await world.heartbeat()
    await eng.tick()
    (pos,) = await world.positions(PORTFOLIO)
    assert pos.status == "closed"
    sells = await world.orders(side="sell", book=PORTFOLIO)
    assert [o.status for o in sells] == ["failed", "filled"]
    assert await world.ledger_is_balanced()


async def test_kill_during_execution_then_restart_without_duplicates(world):
    m = world.token(1)
    eng = world.build()
    await eng.start()
    await world.trade("buy", m, "50000", "500")

    async def killed(seconds: float) -> None:
        raise asyncio.CancelledError  # el proceso muere mientras espera la confirmación

    eng._executor._sleep = killed  # type: ignore[attr-defined]
    with pytest.raises(asyncio.CancelledError):
        await eng.tick()
    pending = await world.orders(status="pending")
    assert {o.book for o in pending} == {PORTFOLIO, SIGNALS}  # se mata con las dos en curso
    assert (await world.cash()).reserved_usd == 10

    restarted = world.build()
    assert await restarted.start() == 2
    await restarted.tick()
    await restarted.tick()
    orders = await world.orders(purpose="entry")
    assert len(orders) == 2  # una por libro: el reinicio no repite la decisión
    portfolio = next(o for o in orders if o.book == PORTFOLIO)
    assert portfolio.status == "failed" and "reinicio" in portfolio.status_reason
    cash = await world.cash()
    assert (cash.cash_usd, cash.reserved_usd) == (100, 0)
    assert await world.ledger_is_balanced()


async def test_insufficient_data_is_rejected_with_reason(world):
    unknown = mint(1)  # sin cuenta del token
    world.quotes.price[unknown] = Decimal("0.01")
    eng = world.build()
    await eng.start()
    await world.trade("buy", unknown, "50000", "500")
    await eng.tick()
    (o,) = await world.orders(book=PORTFOLIO)
    assert o.status == "rejected" and "no se pudo leer la cuenta del token" in o.status_reason

    world.market.sol = None  # sin precio de SOL no se pueden calcular las comisiones
    world.clock.advance(seconds=601)  # el último precio conocido ha caducado
    await world.heartbeat()
    m = world.token(2)
    await world.trade("buy", m, "50000", "500")
    await eng.tick()
    o = (await world.orders(book=PORTFOLIO, mint=m))[0]
    assert o.status == "rejected" and "precio de SOL" in o.status_reason

    world.market.sol = Decimal(200)
    m3 = world.token(3)

    async def broken(*args, **kwargs):
        raise RuntimeError("Jupiter caído")

    world.quotes.quote = broken  # type: ignore[method-assign]
    await world.trade("buy", m3, "50000", "500")
    await eng.tick()
    o = (await world.orders(book=PORTFOLIO, mint=m3))[0]
    assert o.status == "rejected" and "no hay datos de mercado" in o.status_reason
    assert (await world.cash()).cash_usd == 100


async def test_report_compares_realistic_and_theoretical_copy(world):
    from copybot.sim.report import build_report, render

    m = world.token(1)
    eng = world.build()
    await eng.start()
    await world.trade("buy", m, "50000", "500")
    await eng.tick()
    world.quotes.price[m] = Decimal("0.02")
    await world.trade("sell", m, "50000", "1000")
    await eng.tick()

    report = await build_report(world.sessions, world.clock())
    signals = report.books[SIGNALS]
    (pos,) = await world.positions(SIGNALS)
    assert signals.closed_positions == 1 and signals.wins == 1
    assert signals.realized_usd == pos.realized_pnl_usd
    # Teórica: 10 $ a 0,01 y venta a 0,02 → +10 $. La realista pierde por costes.
    assert report.comparison.theoretical_usd == 10
    assert Decimal(9) < report.comparison.realistic_comparable_usd < 10
    assert report.comparison.realistic_usd == report.comparison.realistic_comparable_usd
    assert "MUESTRA INSUFICIENTE" in signals.sample_warning
    text = render(report)
    assert "frente a teórica +10.00 $" in text and "Estado: RUNNING" in text
    assert report.equity_usd == 100 + report.books[PORTFOLIO].realized_usd


# --- Correcciones de la revisión adversarial ------------------------------------------


async def test_risk_halt_cannot_be_bypassed_by_resume_or_restart(world):
    eng = world.build(stop_loss_pct=Decimal(100))
    await eng.start()
    for i in range(1, 6):
        await world.trade("buy", world.token(i), "50000", "500")
    await eng.tick()
    for i in range(1, 6):
        world.quotes.price[mint(i)] = Decimal("0.004")
    world.clock.advance(seconds=61)
    await world.heartbeat()
    await eng.tick()
    assert (await eng.store.state()).state == "HALTED_RISK"
    # Aunque alguien fuerce RUNNING (p. ej. «stop» y «run»), la cartera no compra.
    await eng.store.set_state("STOPPED", "parada manual", "cli")
    restarted = world.build(stop_loss_pct=Decimal(100))
    await restarted.start()
    assert (await restarted.store.state()).state == "HALTED_RISK"
    await restarted.store.set_state("RUNNING", "forzado", "test")
    await world.trade("buy", world.token(9), "50000", "500")
    await restarted.tick()
    (o,) = [o for o in await world.orders(book=PORTFOLIO) if o.mint == mint(9)]
    assert o.status == "rejected" and "parada por riesgo" in o.status_reason


async def test_partial_exit_scales_the_position_value(world):
    m = world.token(1)
    eng = world.build()
    await eng.start()
    await world.trade("buy", m, "50000", "500")
    await eng.tick()
    world.quotes.price[m] = Decimal("0.03")  # x3
    world.clock.advance(seconds=61)
    await world.heartbeat()
    await eng.tick()
    (pos,) = await world.positions(PORTFOLIO)
    before = pos.last_value_usd
    await world.trade("sell", m, "25000", "750")  # el trader vende la mitad
    await eng.tick()
    (pos,) = await world.positions(PORTFOLIO)
    rent = pos.rent_usd
    # El valor de lo que queda es la mitad del de antes (la renta, entera).
    assert abs((pos.last_value_usd - rent) - (before - rent) / 2) < Decimal("0.01")
    state = await eng.store.book_state(PORTFOLIO)
    assert state.equity_usd < Decimal(100) + (before - Decimal(10)) + Decimal("0.1")


async def test_unexpected_errors_never_leave_orders_pending(world):
    m = world.token(1)
    eng = world.build()
    await eng.start()

    async def boom(*args, **kwargs):
        raise ValueError("respuesta rara")

    real_buy = eng._executor.buy
    eng._executor.buy = boom  # type: ignore[method-assign]
    await world.trade("buy", m, "50000", "500")
    await eng.tick()
    assert await world.orders(status="pending") == []
    cash = await world.cash()
    assert cash.reserved_usd == 0 and cash.cash_usd < 100  # paga la comisión

    eng._executor.buy = real_buy  # type: ignore[method-assign]
    await world.trade("buy", world.token(2), "50000", "500")
    await eng.tick()
    eng._executor.sell = boom  # type: ignore[method-assign]
    await world.trade("sell", mint(2), "50000", "500")
    world.quotes.quote = boom  # también falla la valoración
    world.clock.advance(seconds=61)
    await world.heartbeat()
    assert await eng.tick()  # el motor sigue vivo
    assert await world.orders(status="pending") == []
    assert await world.ledger_is_balanced()


async def test_only_one_engine_at_a_time(world):
    from sqlalchemy import text

    from copybot.sim.store import RUN_LOCK_ID

    async def drop_lock_connection() -> None:
        async with world.sessions() as s:
            await s.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND objid = :k"
                ),
                {"k": RUN_LOCK_ID},
            )

    store = world.build().store
    async with store.exclusive_run() as first, store.exclusive_run() as second:
        assert first.held and not second.held
        # Si se cae la conexión del bloqueo, ensure() lo nota y lo vuelve a pedir…
        await drop_lock_connection()
        assert await first.ensure()
        # …y si entretanto lo ha tomado otro motor, avisa para que este se pare.
        await drop_lock_connection()
        assert await second.acquire()
        assert not await first.ensure()


async def test_disconnected_websocket_switches_to_read_only(world):
    eng = world.build()
    await eng.start()
    await world.heartbeat(ws_connected=False)
    await eng.tick()
    state = await eng.store.state()
    assert state.state == "READ_ONLY" and "WebSocket" in state.reason


async def test_stop_loss_still_watches_after_a_failed_partial_copy(world):
    m = world.token(1)
    eng = world.build(revalue_seconds=10)  # se valora antes de que toque reintentar (30 s)
    await eng.start()
    await world.trade("buy", m, "50000", "500")
    await eng.tick()
    world.moves.append((m, Decimal("0.9")))  # la venta copiada falla por slippage
    await world.trade("sell", m, "5000", "50")
    await eng.tick()
    (pos,) = await world.positions(PORTFOLIO)
    assert pos.exit_pending and pos.exit_purpose == "copy_exit"
    world.quotes.price[m] = Decimal("0.003")  # y luego se hunde
    world.clock.advance(seconds=15)
    await world.heartbeat()
    await eng.tick()
    (pos,) = await world.positions(PORTFOLIO)
    assert pos.status == "closed"
    purposes = [o.purpose for o in await world.orders(side="sell", book=PORTFOLIO)]
    assert purposes == ["copy_exit", "stop_loss"]


# --- Segunda revisión ------------------------------------------------------------------


async def test_start_waits_for_the_monitor_without_a_false_alarm(world):
    eng = world.build()
    await eng.start()
    assert (await eng.store.state()).state == "READ_ONLY"
    await eng.tick()  # el monitor ya late: pasa a RUNNING sin dejar aviso de riesgo
    assert (await eng.store.state()).state == "RUNNING"
    async with world.sessions() as s:
        assert list(await s.scalars(select(RiskEvent.kind))) == []


async def test_orders_left_pending_by_an_error_are_recovered_next_tick(world):
    m = world.token(1)
    eng = world.build()
    await eng.start()
    await world.trade("buy", m, "50000", "500")
    real_fill = eng.store.fill_entry
    real_fail = eng.store.fail_order

    async def db_down(*args, **kwargs):
        raise OSError("conexión perdida")

    eng.store.fill_entry = db_down  # type: ignore[method-assign]
    eng.store.fail_order = db_down  # type: ignore[method-assign]
    await eng.tick()
    assert len(await world.orders(status="pending")) == 2  # se quedaron a medias
    eng.store.fill_entry = real_fill  # type: ignore[method-assign]
    eng.store.fail_order = real_fail  # type: ignore[method-assign]
    await eng.tick()
    pending = await world.orders(status="pending")
    assert pending == []
    reasons = {o.status_reason for o in await world.orders(purpose="entry")}
    assert any("a medias por un error" in r for r in reasons)
    assert (await world.cash()).reserved_usd == 0


async def test_run_survives_a_database_hiccup(world):
    eng = world.build()
    calls = 0
    real_tick = eng.tick

    async def flaky_tick() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("PostgreSQL reiniciándose")
        if calls == 3:
            await eng.store.set_state("STOPPED", "fin de la prueba", "test")
        return await real_tick()

    eng.tick = flaky_tick  # type: ignore[method-assign]
    await eng.run()
    assert calls == 3
    assert (await eng.store.state()).state == "STOPPED"


async def test_unsellable_position_that_already_won_counts_as_a_win(world):
    from copybot.sim.report import build_report

    m = world.token(1)
    eng = world.build()
    await eng.start()
    await eng.tick()
    await world.trade("buy", m, "50000", "500")
    await eng.tick()
    world.quotes.price[m] = Decimal("0.05")  # x5
    await world.trade("sell", m, "35000", "1750")  # el trader vende el 70 %
    await eng.tick()
    world.quotes.no_route.add(m)  # y el resto ya no se puede vender
    world.clock.advance(seconds=61)
    await world.heartbeat()
    await eng.tick()
    report = await build_report(world.sessions, world.clock())
    signals = report.books[SIGNALS]
    assert signals.written_off == 1 and signals.wins == 1 and signals.gross_loss_usd == 0

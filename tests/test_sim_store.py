"""Almacén de la simulación contra PostgreSQL: idempotencia, límites, saldos y diario."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from copybot.config import RiskLimits
from copybot.db.models import (
    BookCash,
    LedgerEntry,
    RawTransaction,
    Signal,
    SimFill,
    SimOrder,
    SimPosition,
    TrackedWallet,
    WalletEvent,
)
from copybot.db.session import make_sessionmaker
from copybot.sim.executor import Execution
from copybot.sim.store import PORTFOLIO, SIGNALS, SimStore, idempotency_key

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
TRADER = "Trader1111111111111111111111111111111111111"
OTHER = "Other11111111111111111111111111111111111111"
RUNNING = ["RUNNING"]


def mint(i: int) -> str:
    return f"Mint{i:040d}"


@pytest.fixture
async def store(engine) -> SimStore:
    store = SimStore(make_sessionmaker(engine), clock=lambda: NOW)
    await store.initialize(Decimal(100))
    await store.set_state("RUNNING", "test", "test")
    return store


class Events:
    """Inserta transacciones y eventos clasificados directamente."""

    def __init__(self, engine) -> None:
        self.sessions = make_sessionmaker(engine)
        self.n = 0

    async def wallet(self, address: str, active: bool = True) -> None:
        async with self.sessions.begin() as s:
            s.add(TrackedWallet(address=address, source="manual", active=active))

    async def add(
        self,
        wallet: str,
        kind: str,
        token_delta: str,
        *,
        mint_: str = mint(1),
        usd: str = "10",
        via: str = "live",
        confidence: str = "high",
    ) -> int:
        self.n += 1
        sig = f"sig{self.n:085d}"
        async with self.sessions.begin() as s:
            s.add(
                RawTransaction(
                    signature=sig,
                    slot=self.n,
                    block_time=NOW,
                    fee_payer=wallet,
                    via_fomo=True,
                    succeeded=True,
                    source="test",
                    detected_via=via,
                    first_seen_at=NOW,
                    fetched_at=NOW,
                    payload=None if via == "history" else {},
                )
            )
            await s.flush()
            ev = WalletEvent(
                signature=sig,
                wallet=wallet,
                kind=kind,
                confidence=confidence,
                mint=mint_,
                token_delta=Decimal(token_delta),
                quote_symbol="USDC",
                quote_delta=-Decimal(usd) if kind == "buy" else Decimal(usd),
                wallet_is_signer=True,
                via_fomo=True,
                reasons=[],
                slot=self.n,
                block_time=NOW,
                detected_via=via,
                detected_at=NOW,
                classifier_version="2",
            )
            s.add(ev)
            await s.flush()
            return ev.id


def buy_ex(tokens: str = "1000", usd: str = "9.5", fees: str = "0.02") -> Execution:
    return Execution(
        True, token_amount=Decimal(tokens), usd_amount=Decimal(usd), fees_usd=Decimal(fees)
    )


def sell_ex(tokens: str, usd: str, fees: str = "0.02") -> Execution:
    return Execution(
        True, token_amount=Decimal(tokens), usd_amount=Decimal(usd), fees_usd=Decimal(fees)
    )


async def signal(store: SimStore, engine, i: int = 1, wallet: str = TRADER) -> Signal:
    ev = Events(engine)
    ev.n = 1000 + i
    with contextlib.suppress(IntegrityError):
        await ev.wallet(wallet)
    await ev.add(wallet, "buy", "1000", mint_=mint(i))
    await store.ingest_signals()
    async with make_sessionmaker(engine)() as s:
        return await s.scalar(select(Signal).where(Signal.mint == mint(i)))


async def entry(store: SimStore, sig: Signal, book: str = PORTFOLIO, size: str = "10"):
    return await store.open_entry(
        book=book,
        signal=sig,
        key=idempotency_key(book, sig.id, "entry"),
        size=Decimal(size),
        decision={"tamaño": Decimal(size)},
        limits=RiskLimits() if book == PORTFOLIO else None,
        allowed_states=RUNNING,
    )


async def cash(engine) -> BookCash:
    async with make_sessionmaker(engine)() as s:
        return await s.scalar(select(BookCash).where(BookCash.book == PORTFOLIO))


async def ledger_by_account(engine, book: str = PORTFOLIO) -> dict[str, Decimal]:
    async with make_sessionmaker(engine)() as s:
        rows = await s.execute(
            select(LedgerEntry.account, func.sum(LedgerEntry.delta_usd))
            .where(LedgerEntry.book == book)
            .group_by(LedgerEntry.account)
        )
        return {a: Decimal(v) for a, v in rows}


async def assert_books_balance(engine) -> None:
    """Diario cuadrado y coherente con la caja y las posiciones."""
    async with make_sessionmaker(engine)() as s:
        unbalanced = await s.scalar(
            text(
                "SELECT count(*) FROM (SELECT order_id, sum(delta_usd) AS t FROM ledger_entries "
                "GROUP BY book, order_id, memo) x WHERE t <> 0"
            )
        )
        assert unbalanced == 0
        positions = list(await s.scalars(select(SimPosition)))
        for p in positions:
            total = await s.scalar(
                select(func.coalesce(func.sum(LedgerEntry.delta_usd), 0)).where(
                    LedgerEntry.position_id == p.id, LedgerEntry.account == "position"
                )
            )
            expected = p.cost_usd + p.rent_usd if p.status == "open" else Decimal(0)
            assert Decimal(total) == expected
    books = await ledger_by_account(engine)
    row = await cash(engine)
    assert books.get("cash", 0) == row.cash_usd
    assert books.get("reserved", 0) == row.reserved_usd


async def test_initialize_is_idempotent(engine):
    store = SimStore(make_sessionmaker(engine), clock=lambda: NOW)
    await store.initialize(Decimal(100))
    await store.initialize(Decimal(100))
    assert (await store.state()).state == "RECOVERING"
    assert (await ledger_by_account(engine)) == {"cash": 100, "capital": -100}


async def test_set_state_only_from(store):
    assert await store.set_state("PAUSED", "manual", "cli", only_from=["RUNNING"])
    assert not await store.set_state("RUNNING", "auto", "engine", only_from=["READ_ONLY"])
    assert (await store.state()).state == "PAUSED"


async def test_ingest_signals_filters_and_computes_sell_fraction(store, engine):
    ev = Events(engine)
    await ev.wallet(TRADER)
    await ev.wallet(OTHER, active=False)
    await ev.add(TRADER, "buy", "600", via="history")  # historial: no es señal
    await ev.add(TRADER, "buy", "400")
    await ev.add(TRADER, "buy", "5", confidence="medium")
    await ev.add(OTHER, "buy", "100")  # wallet inactiva
    await ev.add(TRADER, "sell", "-250", via="backfill")
    assert await store.ingest_signals() == 2
    assert await store.ingest_signals() == 0  # sin duplicados
    async with make_sessionmaker(engine)() as s:
        sigs = list(await s.scalars(select(Signal).order_by(Signal.id)))
    assert [x.side for x in sigs] == ["buy", "sell"]
    assert sigs[0].trader_usd == 10 and sigs[0].trader_sell_fraction is None
    # Tenía 600 + 400 + 5 (la de confianza media también cuenta para su saldo).
    assert sigs[1].trader_sell_fraction == round(Decimal(250) / Decimal(1005), 18)


async def test_entry_reserves_cash_once_and_fill_balances(store, engine):
    sig = await signal(store, engine)
    first = await entry(store, sig)
    again = await entry(store, sig)
    assert first.accepted and again.duplicate
    row = await cash(engine)
    assert (row.cash_usd, row.reserved_usd) == (90, 10)

    pos_id = await store.fill_entry(
        first.order_id,
        buy_ex(usd="9.68"),
        rent_lamports=1_488_440,
        rent_usd=Decimal("0.30"),
        theoretical_price=Decimal("0.01"),
    )
    assert (
        await store.fill_entry(
            first.order_id, buy_ex(), rent_lamports=0, rent_usd=0, theoretical_price=None
        )
        is None
    )  # ya ejecutada
    row = await cash(engine)
    assert (row.cash_usd, row.reserved_usd) == (90, 0)  # 9.68 + 0.02 + 0.30 = 10
    async with make_sessionmaker(engine)() as s:
        pos = await s.get(SimPosition, pos_id)
    assert (
        pos.amount == 1000 and pos.cost_usd == Decimal("9.70") and pos.rent_usd == Decimal("0.30")
    )
    await assert_books_balance(engine)


async def test_entry_rejected_when_state_does_not_allow_it(store, engine):
    sig = await signal(store, engine)
    await store.set_state("PAUSED", "manual", "cli")
    result = await entry(store, sig)
    assert not result.accepted and "PAUSED" in result.reasons[0]
    row = await cash(engine)
    assert (row.cash_usd, row.reserved_usd) == (100, 0)


async def test_risk_limits_with_concurrent_entries(store, engine):
    sigs = [await signal(store, engine, i) for i in range(1, 9)]
    results = await asyncio.gather(*(entry(store, s) for s in sigs))
    accepted = [r for r in results if r.accepted]
    assert len(accepted) == RiskLimits().max_open_positions
    assert all("posiciones abiertas" in r.reasons[0] for r in results if not r.accepted)
    row = await cash(engine)
    assert row.cash_usd == 50 and row.reserved_usd == 50
    await assert_books_balance(engine)


async def test_cash_reserve_is_respected(store, engine):
    sig = await signal(store, engine)
    result = await entry(store, sig, size="81")  # dejaría 19 $ con 20 $ de reserva
    assert not result.accepted and "reserva" in result.reasons[0]


async def test_partial_and_full_exit_never_oversell_and_refund_rent(store, engine):
    sig = await signal(store, engine)
    order = await entry(store, sig)
    pos_id = await store.fill_entry(
        order.order_id,
        buy_ex(usd="9.68"),
        rent_lamports=1,
        rent_usd=Decimal("0.30"),
        theoretical_price=None,
    )
    sell1 = await store.open_exit(
        position_id=pos_id,
        key="k1",
        amount=Decimal(400),
        purpose="copy_exit",
        signal_id=None,
        decision={},
    )
    assert await store.fill_exit(sell1, sell_ex("400", "6"), rent_refund_usd=Decimal("0.30"))
    async with make_sessionmaker(engine)() as s:
        pos = await s.get(SimPosition, pos_id)
    assert pos.status == "open" and pos.amount == 600
    assert pos.realized_pnl_usd == Decimal("6") - Decimal("0.02") - Decimal("3.88")
    await assert_books_balance(engine)

    # Pide vender más de lo que queda: solo vende lo que hay y cierra.
    sell2 = await store.open_exit(
        position_id=pos_id,
        key="k2",
        amount=Decimal(10_000),
        purpose="stop_loss",
        signal_id=None,
        decision={},
    )
    assert await store.fill_exit(sell2, sell_ex("10000", "3"), rent_refund_usd=Decimal("0.29"))
    async with make_sessionmaker(engine)() as s:
        pos = await s.get(SimPosition, pos_id)
        sold = await s.scalar(
            select(func.sum(SimFill.token_amount))
            .join(SimOrder, SimOrder.id == SimFill.order_id)
            .where(SimOrder.position_id == pos_id, SimOrder.side == "sell")
        )
    assert pos.status == "closed" and pos.amount == 0 and sold == 1000
    # Cobrado 6 + 3 + 0.29 de renta − 0.04 de comisiones; pagado 10.
    assert pos.realized_pnl_usd == Decimal("9.25") - 10
    row = await cash(engine)
    assert row.cash_usd == Decimal(100) + pos.realized_pnl_usd
    books = await ledger_by_account(engine)
    assert -books["pnl"] - books["fees"] == pos.realized_pnl_usd
    await assert_books_balance(engine)
    # No se puede volver a vender una posición cerrada.
    assert (
        await store.open_exit(
            position_id=pos_id,
            key="k3",
            amount=Decimal(1),
            purpose="stop_loss",
            signal_id=None,
            decision={},
        )
        is None
    )


async def test_failed_order_releases_reservation_and_charges_fee(store, engine):
    sig = await signal(store, engine)
    order = await entry(store, sig)
    assert await store.fail_order(order.order_id, "slippage", Decimal("0.02"))
    assert not await store.fail_order(order.order_id, "otra vez", Decimal("0.02"))
    row = await cash(engine)
    assert (row.cash_usd, row.reserved_usd) == (Decimal("99.98"), 0)
    await assert_books_balance(engine)


async def test_restart_fails_pending_orders_without_duplicating(store, engine):
    sig = await signal(store, engine)
    order = await entry(store, sig)
    # «Se mata el proceso» con la orden a medias: un almacén nuevo la recupera.
    restarted = SimStore(make_sessionmaker(engine), clock=lambda: NOW + timedelta(minutes=1))
    assert await restarted.recover_pending() == 1
    assert await restarted.recover_pending() == 0
    async with make_sessionmaker(engine)() as s:
        o = await s.get(SimOrder, order.order_id)
    assert o.status == "failed" and "reinicio" in o.status_reason
    assert [s.id for s in await restarted.pending_signals([PORTFOLIO])] == []
    assert (await entry(restarted, sig)).duplicate
    row = await cash(engine)
    assert (row.cash_usd, row.reserved_usd) == (100, 0)
    await assert_books_balance(engine)


async def test_signals_book_has_no_capital_limit(store, engine):
    sigs = [await signal(store, engine, i) for i in range(1, 8)]
    for sg in sigs:
        result = await entry(store, sg, book=SIGNALS)
        assert result.accepted
        await store.fill_entry(
            result.order_id,
            buy_ex(usd="9.68"),
            rent_lamports=1,
            rent_usd=Decimal("0.30"),
            theoretical_price=None,
        )
    assert len(await store.open_positions(SIGNALS)) == 7
    assert (await cash(engine)).cash_usd == 100  # la cartera no se toca
    books = await ledger_by_account(engine, SIGNALS)
    assert books == {"capital": Decimal(-70), "position": Decimal(70)}


async def test_database_rejects_negative_cash(store, engine):
    async with make_sessionmaker(engine).begin() as s:
        row = await s.scalar(select(BookCash).where(BookCash.book == PORTFOLIO))
        row.cash_usd = Decimal(-1)
        with pytest.raises(IntegrityError):
            await s.flush()


def test_sell_fraction_from_the_transaction_balances():
    from copybot.sim.store import fraction_from_payload

    def bal(owner, amount):
        return {"owner": owner, "mint": mint(1), "uiTokenAmount": {"amount": str(amount)}}

    payload = {
        "meta": {
            "preTokenBalances": [bal(TRADER, 1000), bal(OTHER, 5)],
            "postTokenBalances": [bal(TRADER, 250), bal(OTHER, 5)],
        }
    }
    assert fraction_from_payload(payload, TRADER, mint(1)) == Decimal("0.75")
    assert fraction_from_payload(payload, TRADER, mint(2)) is None
    assert fraction_from_payload(None, TRADER, mint(1)) is None


async def test_ledger_balances_with_awkward_decimals(store, engine):
    """Precios de SOL con muchos decimales (como los de Jupiter): el diario sigue cuadrando
    al guardarse con 18 decimales, en ventas parciales y totales."""
    sig = await signal(store, engine)
    order = await entry(store, sig)
    sol = Decimal("147.8079789639263")
    fee = Decimal(105_000) / Decimal(10**9) * sol
    rent = Decimal(1_488_440) / Decimal(10**9) * sol
    ex = Execution(
        True,
        token_amount=Decimal("32330.312208") * Decimal("0.995"),
        usd_amount=Decimal(10) - fee - rent,
        fees_usd=fee,
    )
    pos_id = await store.fill_entry(
        order.order_id,
        ex,
        rent_lamports=1_488_440,
        rent_usd=rent,
        theoretical_price=Decimal("0.0003"),
        theoretical_tokens=Decimal(10) / Decimal("0.0003"),
    )
    async with make_sessionmaker(engine)() as s:
        held = (await s.get(SimPosition, pos_id)).amount
    for n, part in enumerate([held / 3, held]):
        oid = await store.open_exit(
            position_id=pos_id,
            key=f"x{n}",
            amount=part,
            purpose="copy_exit",
            signal_id=None,
            decision={},
        )
        await store.fill_exit(
            oid,
            sell_ex(str(part), str(Decimal("14.4814330") / 3)),
            rent_refund_usd=Decimal(1_488_440) / Decimal(10**9) * sol,
            theoretical_price=Decimal("0.0004"),
        )
    async with make_sessionmaker(engine)() as s:
        pos = await s.get(SimPosition, pos_id)
    assert pos.status == "closed" and pos.amount == 0
    await assert_books_balance(engine)

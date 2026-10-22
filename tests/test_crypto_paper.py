"""Cartera de prueba en vivo (PostgreSQL): idempotencia, ejecución al bid/ask, libro de
señales, paradas y fin de una prueba."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from copybot.config import RiskLimits
from copybot.crypto.binance import BookTicker
from copybot.crypto.paper import (
    Filters,
    PaperError,
    paper_step,
    set_status,
    start_run,
)
from copybot.db.models import (
    CryptoPaperBook,
    CryptoPaperOrder,
    CryptoPaperPosition,
    CryptoPaperRun,
    CryptoPaperSnapshot,
)
from copybot.db.session import make_sessionmaker
from tests.test_crypto_strategy import coin, view

pytestmark = pytest.mark.postgres
D = Decimal
DAY = date(2026, 10, 20)
LIMITS = RiskLimits.model_construct()
NOW = datetime(2026, 10, 21, 0, 12, tzinfo=UTC)
COINS = [coin(f"C{k}USDT", k) for k in range(1, 16)]


def quotes(price: str = "10", spread: str = "0.01") -> dict[str, BookTicker]:
    p, h = D(price), D(spread)
    out = {c.symbol: BookTicker(c.symbol, p - h, p + h, D(1000), D(1000)) for c in COINS}
    out["BTCUSDT"] = BookTicker("BTCUSDT", D(99_990), D(100_010), D(5), D(5))
    return out


FILTERS = {c.symbol: Filters(D("0.001"), D(5)) for c in COINS}


async def test_daily_step_buys_at_the_ask_and_is_idempotent(engine):
    sessions = make_sessionmaker(engine)
    run_id = await start_run(
        sessions, kind="continua", start_day=DAY, limits=LIMITS, btc_price=D(100_000)
    )
    v = view(COINS)
    r1 = await paper_step(sessions, run_id, v, quotes(), FILTERS, now=NOW)
    buys = [o for o in r1.orders if o[1] == "buy" and o[3] == "filled"]
    assert len([o for o in buys if o[2] == "entrada"]) == 5  # cartera
    assert len([o for o in buys if o[2] == "entrada"]) + 0 == 5
    again = await paper_step(sessions, run_id, v, quotes(), FILTERS, now=NOW)
    assert again.status == "ya_hecho"
    async with sessions() as s:
        orders = list(await s.scalars(select(CryptoPaperOrder)))
        book = await s.get(CryptoPaperBook, (run_id, "portfolio"))
        positions = list(
            await s.scalars(
                select(CryptoPaperPosition).where(CryptoPaperPosition.book == "portfolio")
            )
        )
    filled = [o for o in orders if o.book == "portfolio" and o.status == "filled"]
    assert len(filled) == 5 and all(o.price == D("10.01") for o in filled)  # al ask
    spent = sum(p.cost_usd for p in positions)
    assert spent <= 5 * D(18) and book.cash_usd == D(100) - spent
    assert book.cash_usd >= D(10)  # la reserva
    assert all(p.qty == p.qty.quantize(D("0.001")) for p in positions)  # stepSize
    signals = [o for o in orders if o.book == "signals"]
    assert len(signals) == 5 and all(o.requested_usd == D(10) for o in signals)
    assert len({o.idempotency_key for o in orders}) == len(orders)


async def test_sells_at_the_bid_and_signals_book_closes_after_7_days(engine):
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="continua", start_day=DAY, limits=LIMITS)
    await paper_step(sessions, run_id, view(COINS), quotes(), FILTERS, now=NOW)
    # Una semana después C1 cae al puesto 13 y el mercado sigue favorable.
    later = DAY + timedelta(days=7)
    moved = [coin("C1USDT", 13), *[coin(f"C{k}USDT", k - 1) for k in range(2, 14)]]
    v = view(moved)
    v = type(v)(later, v.universe, v.ranked, v.regime, v.close, v.low)
    r = await paper_step(sessions, run_id, v, quotes("12"), FILTERS, now=NOW + timedelta(7))
    assert (
        "C1USDT",
        "sell",
        "puesto",
        "filled",
        "bajó al puesto 13 (se vende a partir del 13)",
    ) in r.orders
    async with sessions() as s:
        sold = await s.scalar(
            select(CryptoPaperOrder).where(
                CryptoPaperOrder.book == "portfolio", CryptoPaperOrder.side == "sell"
            )
        )
        closed_signals = await s.scalar(
            select(func.count())
            .select_from(CryptoPaperPosition)
            .where(CryptoPaperPosition.book == "signals", CryptoPaperPosition.status == "closed")
        )
    assert sold.price == D("11.99")  # al bid
    assert closed_signals == 5  # los 5 del primer día cumplen 7 días


async def test_one_active_run_per_kind_and_manual_pause(engine):
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="continua", start_day=DAY, limits=LIMITS)
    with pytest.raises(PaperError):
        await start_run(sessions, kind="continua", start_day=DAY, limits=LIMITS)
    test_id = await start_run(sessions, kind="prueba", start_day=DAY, limits=LIMITS, days=7)
    assert test_id != run_id
    await set_status(sessions, run_id, "PAUSED", "lo pido yo")
    r = await paper_step(sessions, run_id, view(COINS), quotes(), FILTERS, now=NOW)
    assert not [o for o in r.orders if o[1] == "buy" and o[2] == "entrada"]


async def test_bear_market_sells_everything(engine):
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="continua", start_day=DAY, limits=LIMITS)
    await paper_step(sessions, run_id, view(COINS), quotes(), FILTERS, now=NOW)
    bear = view(COINS, favorable=False)
    nxt = DAY + timedelta(days=1)
    bear = type(bear)(nxt, bear.universe, bear.ranked, bear.regime, bear.close, bear.low)
    r = await paper_step(sessions, run_id, bear, quotes(), FILTERS, now=NOW + timedelta(1))
    portfolio_sells = [o for o in r.orders if o[1] == "sell" and o[2] == "regimen"]
    assert len(portfolio_sells) == 5
    async with sessions() as s:
        book = await s.get(CryptoPaperBook, (run_id, "portfolio"))
        snap = await s.get(CryptoPaperSnapshot, (run_id, "portfolio", nxt))
    assert book.cash_usd > D(98) and snap.open_positions == 0


async def test_a_test_run_finishes_and_freezes_its_verdict(engine):
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="prueba", start_day=DAY, limits=LIMITS, days=1)
    await paper_step(sessions, run_id, view(COINS), quotes(), FILTERS, now=NOW)
    nxt = DAY + timedelta(days=1)
    v = view(COINS)
    v = type(v)(nxt, v.universe, v.ranked, v.regime, v.close, v.low)
    r = await paper_step(sessions, run_id, v, quotes(), FILTERS, now=NOW + timedelta(1))
    assert "fin de la prueba" in r.events
    async with sessions() as s:
        run = await s.get(CryptoPaperRun, run_id)
        left = await s.scalar(
            select(func.count())
            .select_from(CryptoPaperPosition)
            .where(
                CryptoPaperPosition.run_id == run_id,
                CryptoPaperPosition.book == "portfolio",
                CryptoPaperPosition.status == "open",
            )
        )
    assert run.status == "FINISHED" and left == 0
    assert run.verdict["semaforo"] == "gris" and run.verdict["dias"] == 1

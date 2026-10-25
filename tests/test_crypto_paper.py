"""Cartera de prueba en vivo (PostgreSQL): idempotencia, ejecución al bid/ask, libro de
señales, paradas, filtros solo en vivo, retiradas, polvo y fin de una prueba."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from copybot.config import RiskLimits
from copybot.crypto.binance import BookTicker
from copybot.crypto.paper import (
    Filters,
    PaperError,
    live_blocks,
    paper_step,
    resume,
    set_status,
    start_run,
)
from copybot.db.models import (
    CryptoPaperBook,
    CryptoPaperEvent,
    CryptoPaperOrder,
    CryptoPaperPosition,
    CryptoPaperRun,
    CryptoPaperSnapshot,
    MarketBar,
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


def _on(v, day):
    """La vista ``v`` con otra fecha."""
    return replace(v, day=day)


def _c1(book: str = "portfolio"):
    return select(CryptoPaperPosition).where(
        CryptoPaperPosition.book == book, CryptoPaperPosition.symbol == "C1USDT"
    )


def test_live_blocks_name_the_reason_and_only_touch_buy_candidates():
    q = quotes()
    q["C2USDT"] = BookTicker("C2USDT", D("9.95"), D("10.05"), D(1000), D(1000))  # 1 %
    del q["C5USDT"]
    filters = FILTERS | {
        "C3USDT": Filters(D("0.001"), D(5), status="BREAK"),
        "C4USDT": Filters(D("0.001"), D(5), status="TRADING", spot=False),
        "C9USDT": Filters(D("0.001"), D(5), status="BREAK"),  # puesto 9: no es candidata
    }
    blocks = live_blocks(view(COINS), q, filters, held=frozenset({"C1USDT"}))
    assert blocks == {
        "C2USDT": "diferencial del 1,00 % (máximo 0,5 %)",
        "C3USDT": "el par no está en TRADING (estado BREAK)",
        "C4USDT": "Binance no permite negociarlo al contado",
        "C5USDT": "sin precio de compra ahora (bookTicker)",
    }


async def test_blocked_candidate_is_skipped_and_recorded(engine):
    """F4: una candidata con el diferencial por encima del 0,5 % no se compra; se apunta
    «bloqueada en vivo: motivo» y decide() pasa a la siguiente."""
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="continua", start_day=DAY, limits=LIMITS)
    coins = [*COINS[:5], coin("C6USDT", 5), *COINS[6:]]  # dos en el puesto 5: C5 y C6
    q = quotes()
    q["C2USDT"] = BookTicker("C2USDT", D("9.95"), D("10.05"), D(1000), D(1000))
    r = await paper_step(sessions, run_id, view(coins), q, FILTERS, now=NOW)
    bought = {o[0] for o in r.orders if o[2] == "entrada" and o[3] == "filled"}
    assert bought == {"C1USDT", "C3USDT", "C4USDT", "C5USDT", "C6USDT"}
    assert r.blocked == [("C2USDT", "bloqueada en vivo: diferencial del 1,00 % (máximo 0,5 %)")]
    async with sessions() as s:
        event = await s.scalar(select(CryptoPaperEvent).where(CryptoPaperEvent.kind == "bloqueo"))
    assert event.details == {"dia": DAY.isoformat(), "bloqueadas": dict(r.blocked)}


async def test_a_pair_that_stops_trading_is_sold_and_not_bought_again(engine):
    """F5 (en vivo): un par que deja de estar en TRADING se vende al bid en el paso
    siguiente y no se vuelve a comprar aunque siga en el puesto 1."""
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="continua", start_day=DAY, limits=LIMITS)
    await paper_step(sessions, run_id, view(COINS), quotes(), FILTERS, now=NOW)
    nxt = DAY + timedelta(days=1)
    filters = FILTERS | {"C1USDT": Filters(D("0.001"), D(5), status="BREAK")}
    r = await paper_step(sessions, run_id, _on(view(COINS), nxt), quotes(), filters, now=NOW)
    sells = [o for o in r.orders if o[0] == "C1USDT" and o[1] == "sell"]
    assert sells == [
        ("C1USDT", "sell", "retirada", "filled", "el par dejó de cotizar en Binance (estado BREAK)")
    ]
    assert not [o for o in r.orders if o[2] == "entrada"]
    assert r.blocked == [("C1USDT", "bloqueada en vivo: el par no está en TRADING (estado BREAK)")]
    async with sessions() as s:
        pos = await s.scalar(_c1())
        order = await s.scalar(
            select(CryptoPaperOrder).where(CryptoPaperOrder.purpose == "retirada")
        )
    assert pos.status == "closed" and pos.exit_purpose == "retirada"
    assert order.price == D("9.99")  # al bid


async def test_seven_days_without_a_traded_candle_close_at_last_close_minus_10(engine):
    """F5 (como el backtest): sin vela con negociación durante 7 días seguidos, la posición
    se cierra al último cierre × 0,90. Una vela con 0 operaciones no cuenta."""
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="continua", start_day=DAY, limits=LIMITS)
    async with sessions.begin() as s:
        for k, trades in ((-1, 9), (0, 9), (2, 0)):  # el 22 hay vela, pero sin operaciones
            s.add(
                MarketBar(
                    exchange="binance",
                    symbol="C1USDT",
                    interval="1d",
                    open_time=datetime(2026, 10, 20 + k, tzinfo=UTC),
                    open=D(10),
                    high=D(10),
                    low=D(10),
                    close=D(10),
                    volume=D(1),
                    quote_volume=D(10),
                    trades=trades,
                )
            )
    await paper_step(sessions, run_id, view(COINS), quotes(), FILTERS, now=NOW)
    without_c1 = view(COINS[1:])  # C1 ya no tiene vela: ni universo ni cierre
    six = await paper_step(
        sessions, run_id, _on(without_c1, DAY + timedelta(days=6)), quotes(), FILTERS, now=NOW
    )
    assert not [o for o in six.orders if o[0] == "C1USDT" and o[2] == "retirada"]
    async with sessions() as s:
        before = (await s.get(CryptoPaperBook, (run_id, "portfolio"))).cash_usd
        assert (await s.scalar(_c1())).status == "open"
    seven = await paper_step(
        sessions, run_id, _on(without_c1, DAY + timedelta(days=7)), quotes(), FILTERS, now=NOW
    )
    assert ("C1USDT", "sell", "retirada") in {o[:3] for o in seven.orders}
    async with sessions() as s:
        pos = await s.scalar(_c1())
        order = await s.scalar(
            select(CryptoPaperOrder).where(CryptoPaperOrder.purpose == "retirada")
        )
        book = await s.get(CryptoPaperBook, (run_id, "portfolio"))
    assert order.price == D(9) and order.status == "filled"  # último cierre (10) × 0,90
    assert pos.status == "closed" and pos.exit_purpose == "retirada"
    assert pos.proceeds_usd == order.qty * D(9)
    assert book.cash_usd == before + order.qty * D(9)  # C2-C5 siguen: no hay compras


async def test_dust_is_closed_at_the_bid_minus_fee_in_both_books(engine):
    """P3: lo que vale menos de 5 $ (o del minNotional) no se puede vender nunca; se da por
    cerrado al bid menos la comisión, con motivo de salida «polvo»."""
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="continua", start_day=DAY, limits=LIMITS)
    await paper_step(sessions, run_id, view(COINS), quotes(), FILTERS, now=NOW)
    later = DAY + timedelta(days=7)
    moved = [coin("C1USDT", 13), *[coin(f"C{k}USDT", k - 1) for k in range(2, 14)]]
    q = quotes()
    q["C1USDT"] = BookTicker("C1USDT", D(2), D("2.02"), D(1000), D(1000))  # ~3,6 $ y ~2 $
    r = await paper_step(sessions, run_id, _on(view(moved), later), q, FILTERS, now=NOW)
    dust = [o for o in r.orders if o[0] == "C1USDT" and o[1] == "sell"]
    assert {o[2] for o in dust} == {"puesto", "horizonte_7d"}
    assert all(o[3] == "filled" and "polvo" in o[4] for o in dust)
    async with sessions() as s:
        positions = list(
            await s.scalars(
                select(CryptoPaperPosition).where(CryptoPaperPosition.symbol == "C1USDT")
            )
        )
        orders = {
            o.book: o
            for o in await s.scalars(
                select(CryptoPaperOrder).where(
                    CryptoPaperOrder.symbol == "C1USDT", CryptoPaperOrder.side == "sell"
                )
            )
        }
    assert {p.book for p in positions} == {"portfolio", "signals"}
    for p in positions:
        o = orders[p.book]
        assert p.status == "closed" and p.exit_purpose == "polvo" and p.qty == 0
        assert o.price == D(2) and o.fee_usd == o.qty * D(2) * D("0.001")
        assert p.proceeds_usd == o.qty * D(2) - o.fee_usd < D(5)


async def test_two_signal_positions_of_the_same_coin_close_on_the_same_day(engine):
    """P6: con un día sin paso, dos posiciones del libro de señales de la misma moneda
    cumplen sus 7 días el mismo día: cada venta tiene su clave."""
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="continua", start_day=DAY, limits=LIMITS)
    for k in (0, 1, 8):  # el día 7 no hay paso
        d = DAY + timedelta(days=k)
        await paper_step(sessions, run_id, _on(view(COINS), d), quotes(), FILTERS, now=NOW)
    async with sessions() as s:
        c1 = list(
            await s.scalars(
                _c1("signals").where(CryptoPaperPosition.opened_day <= DAY + timedelta(1))
            )
        )
        keys = list(await s.scalars(select(CryptoPaperOrder.idempotency_key)))
    assert len(c1) == 2 and all(p.status == "closed" for p in c1)
    assert len(keys) == len(set(keys))


async def test_resume_is_refused_while_the_50_pct_drawdown_still_applies(engine):
    """P7: reanudar una cartera parada por la caída del 50 % se deshacía en el paso
    siguiente; ahora no se reanuda mientras siga por debajo del 50 % del máximo."""
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="continua", start_day=DAY, limits=LIMITS)
    async with sessions.begin() as s:
        book = await s.get(CryptoPaperBook, (run_id, "portfolio"))
        book.cash_usd, book.prev_equity_usd, book.peak_equity_usd = D(40), D(40), D(100)
        (await s.get(CryptoPaperRun, run_id)).status = "HALTED_RISK"
    with pytest.raises(PaperError, match="caída del 50 % sigue vigente"):
        await resume(sessions, run_id)
    async with sessions() as s:
        assert (await s.get(CryptoPaperRun, run_id)).status == "HALTED_RISK"
    async with sessions.begin() as s:
        book = await s.get(CryptoPaperBook, (run_id, "portfolio"))
        book.cash_usd, book.prev_equity_usd = D(60), D(60)
    await resume(sessions, run_id)
    async with sessions() as s:
        assert (await s.get(CryptoPaperRun, run_id)).status == "RUNNING"


async def test_last_day_of_a_test_run_does_not_buy_before_selling_everything(engine):
    """P12: el último día de una prueba no se compra (se vendería al momento)."""
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="prueba", start_day=DAY, limits=LIMITS, days=1)
    await paper_step(sessions, run_id, view(COINS), quotes(), FILTERS, now=NOW)
    last = DAY + timedelta(days=1)
    moved = [coin("C1USDT", 13), *[coin(f"C{k}USDT", k - 1) for k in range(2, 14)]]
    r = await paper_step(sessions, run_id, _on(view(moved), last), quotes(), FILTERS, now=NOW)
    assert ("C1USDT", "sell", "puesto") in {o[:3] for o in r.orders}
    assert "fin de la prueba" in r.events
    async with sessions() as s:
        buys = await s.scalar(
            select(func.count())
            .select_from(CryptoPaperOrder)
            .where(
                CryptoPaperOrder.book == "portfolio",
                CryptoPaperOrder.side == "buy",
                CryptoPaperOrder.day == last,
            )
        )
    assert buys == 0


async def test_a_run_without_btc_price_takes_it_from_its_first_step(engine):
    """P11: una prueba lanzada desde la consola (sin red) no tenía referencia de BTC; se
    fija con el ask de BTC de su primer paso y no cambia después."""
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="prueba", start_day=DAY, limits=LIMITS, days=7)
    v = view(COINS)
    v = replace(v, close=v.close | {"BTCUSDT": 100_000.0})
    await paper_step(sessions, run_id, v, quotes(), FILTERS, now=NOW)
    q = quotes()
    q["BTCUSDT"] = BookTicker("BTCUSDT", D(50_000), D(50_001), D(5), D(5))
    await paper_step(sessions, run_id, _on(v, DAY + timedelta(days=1)), q, FILTERS, now=NOW)
    async with sessions() as s:
        start = await s.scalar(
            select(CryptoPaperEvent.details).where(CryptoPaperEvent.kind == "inicio")
        )
        snaps = list(
            await s.scalars(
                select(CryptoPaperSnapshot.btc_benchmark_usd).order_by(CryptoPaperSnapshot.day)
            )
        )
    assert start["btc_precio"] == "100010"
    assert [x.quantize(D("0.000001")) for x in snaps] == [D("99.990001")] * 2  # 100·1e5/100.010

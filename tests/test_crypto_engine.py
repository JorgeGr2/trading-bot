"""Motor del backtest con paneles sintéticos: sin mirar el futuro, ejecución en la
apertura siguiente, retiradas y libro de señales."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest

from copybot.config import RiskLimits
from copybot.crypto import costs, engine
from copybot.crypto.engine import RunConfig, RunResult, build_views, signals_book, simulate
from copybot.crypto.panel import Panel, Series
from copybot.crypto.signals import CoinSignal
from copybot.crypto.strategy import (
    STRATEGY_V1,
    BookView,
    DayView,
    Decision,
    Holding,
    Order,
    Regime,
    day_view,
    decide,
)
from copybot.crypto.universe import Member, Universe

START = date(2024, 1, 1)
LIMITS = RiskLimits()
COINS = [f"C{k:02d}USDT" for k in range(1, 16)]


def make_panel(n: int = 12, days: int = 260, *, seed: int = 1, drift=None) -> Panel:
    """``n`` monedas con paseos aleatorios (y BTC); ``drift(k)`` da la deriva diaria de la
    moneda k (por defecto, ninguna)."""
    rng = random.Random(seed)  # noqa: S311
    series = {}
    for k in range(n + 1):
        symbol = "BTCUSDT" if k == 0 else f"C{k:02d}USDT"
        s = Series(symbol, symbol[:-4], "TRADING")
        price = 100.0
        mu = 0.002 if k == 0 else (drift(k) if drift else 0.0)
        for d in range(days):
            day = START + timedelta(days=d)
            o = price
            price *= math.exp(mu + rng.gauss(0, 0.03))
            s.days.append(day)
            s.open.append(o)
            s.close.append(price)
            s.high.append(max(o, price) * 1.01)
            s.low.append(min(o, price) * 0.99)
            s.quote_volume.append(5e7 + k * 1e6)
            s.trades.append(1000)
        series[symbol] = s
    return Panel(series)


def days_from(panel: Panel, first: int = 130) -> list[date]:
    return panel.days[first:]


def flat_panel(symbols: list[str], days: int, *, price=lambda symbol, d: 10.0) -> Panel:
    """Velas hechas a mano: ``price(symbol, d)`` es la apertura y el cierre del día d."""
    series = {}
    for symbol in symbols:
        s = Series(symbol, symbol[:-4], "TRADING")
        for d in range(days):
            p = price(symbol, d)
            s.days.append(START + timedelta(days=d))
            for attr in ("open", "high", "low", "close"):
                getattr(s, attr).append(p)
            s.quote_volume.append(1e8)
            s.trades.append(1000)
        series[symbol] = s
    return Panel(series)


def drop_days(series: Series, gone) -> None:
    keep = [i for i, d in enumerate(series.days) if d not in gone]
    for attr in ("days", "open", "high", "low", "close", "quote_volume", "trades"):
        setattr(series, attr, [getattr(series, attr)[i] for i in keep])
    series._episode = []


def round_trip(median_qv: float) -> float:
    """Lo que vuelve de 1 $ comprado y vendido al mismo precio, con costes."""
    c = costs.cost_per_side(median_qv)
    return (1 - c) * (1 - costs.FEE) / ((1 + c) * (1 + costs.FEE))


def manual_view(panel: Panel, day: date, ranked: list[str], *, qv=1e8) -> DayView:
    """Vista hecha a mano: los puestos de ``ranked``, todas con tendencia 3 de 3, mercado
    favorable y los cierres del panel. ``qv`` es la mediana de volumen (o un dict)."""
    vol = qv if isinstance(qv, dict) else dict.fromkeys(ranked, qv)
    above = {20: True, 50: True, 100: True}
    sigs = [
        CoinSignal(s, s[:-4], 10.0, 0.03, {30: 0.1}, {30: 1.0}, above, 100 - k, k)
        for k, s in enumerate(ranked, 1)
    ]
    members = [Member(s, s[:-4], vol[s], k) for k, s in enumerate(ranked, 1)]
    close, low = {}, {}
    for s in panel.series.values():
        i = s.at(day)
        if i is not None and s.traded(i):
            close[s.symbol], low[s.symbol] = s.close[i], s.low[i]
    return DayView(day, Universe(day, members, {}), sigs, Regime(100, 90, True), close, low)


@pytest.mark.parametrize("mode", ["porcentaje", "dolares"])
def test_one_day_late_never_exceeds_n_nor_spends_the_reserve(monkeypatch, mode):
    """Con un día de retraso, la decisión de t+1 se toma con las órdenes de t aún sin
    ejecutar: deben contar ya (huecos y efectivo), o la cartera llega a 6 posiciones."""
    panel = flat_panel(["BTCUSDT", *COINS], 40)
    # Las 5 primeras cambian cada día (los puestos rotan de 3 en 3).
    views = {
        d: manual_view(panel, d, COINS[3 * k % 15 :] + COINS[: 3 * k % 15])
        for k, d in enumerate(panel.days)
    }
    seen = []
    real = engine._execute

    def spy(panel_, book, decision, day, cfg, result, view):
        real(panel_, book, decision, day, cfg, result, view)
        equity = book.cash + sum(h.qty * 10.0 for h in book.holdings.values())
        seen.append((len(book.holdings), book.cash, equity))

    monkeypatch.setattr(engine, "_execute", spy)
    cfg = RunConfig(STRATEGY_V1, LIMITS, mode=mode, exec_delay=2)
    result = simulate(panel, views, panel.days, cfg)
    assert len(result.trades) > 10
    assert max(n for n, _, _ in seen) == STRATEGY_V1.n_positions
    for _, cash, equity in seen:
        reserve = 0.10 * equity if mode == "porcentaje" else 10.0  # apartado 6.2
        assert cash >= reserve - 1e-9


def test_a_buy_that_no_longer_fits_is_rejected_not_shrunk():
    """Si la venta del día no se pudo hacer (sin negociación), la compra no cabe: se
    rechaza entera, como en vivo, en vez de comprar con lo que quede."""
    panel = flat_panel(["BTCUSDT", *COINS[:6]], 5)
    day = panel.days[2]
    panel.series["C01USDT"].trades[2] = 0  # hoy no se puede vender C01
    view = manual_view(panel, day, COINS[:6])
    cfg = RunConfig(STRATEGY_V1, LIMITS, mode="dolares")
    for held_symbols, cash in ((COINS[:5], 10.0), (COINS[:4], 10.0)):
        book = engine._Book(cash)
        for s in held_symbols:
            book.holdings[s] = Holding(s, 1.8, 10.0, panel.days[0], 10.0)
            book.open_trades[s] = engine.Trade(s, panel.days[0], None, 1.8, 10.0, 18.0)
        orders = [
            Order("C01USDT", "sell", "puesto", "bajó de puesto"),
            Order("C06USDT", "buy", "entrada", "puesto 1", usd=18.0),
        ]
        decision = Decision(panel.days[1], 100.0, 100.0, orders, [], True, 18.0)
        engine._execute(panel, book, decision, day, cfg, RunResult(cfg, [], [], []), view)
        assert "C06USDT" not in book.holdings
        assert set(book.holdings) == set(held_symbols) and book.cash == cash


def test_signals_book_waits_for_the_first_traded_open_after_a_gap():
    """Sin vela en t+8 pero la moneda vuelve a cotizar en t+10 un 60 % más abajo: la
    pérdida cuenta (antes la operación desaparecía) y BTC se mide en la misma ventana."""
    panel = flat_panel(
        ["BTCUSDT", "C01USDT"],
        40,
        price=lambda s, d: 100.0 + d if s == "BTCUSDT" else (100.0 if d < 15 else 40.0),
    )
    t = panel.days[5]
    gap = {t + timedelta(days=k) for k in range(5, 10)}  # de t+5 a t+9, sin velas
    drop_days(panel.series["C01USDT"], gap)
    views = {t: manual_view(panel, t, ["C01USDT"])}
    [trade] = signals_book(panel, views, [t], STRATEGY_V1)
    assert trade.exit_day == t + timedelta(days=10)
    assert trade.ret == pytest.approx(0.4 * round_trip(1e8) - 1)
    # BTC no está en este universo de prueba: paga el tramo más caro.
    assert trade.btc_ret == pytest.approx((100 + 15) / (100 + 6) * round_trip(5e6) - 1)


def test_signals_book_closes_a_delisting_on_its_seventh_day_without_trading():
    panel = flat_panel(
        ["BTCUSDT", "C01USDT"], 40, price=lambda s, d: 100.0 + d if s == "BTCUSDT" else 50.0
    )
    t = panel.days[5]
    s = panel.series["C01USDT"]
    drop_days(s, {d for d in s.days if d > t + timedelta(days=3)})  # deja de cotizar
    s.close[-1] = 60.0  # último cierre negociado (t+3)
    views = {t: manual_view(panel, t, ["C01USDT"])}
    [trade] = signals_book(panel, views, [t], STRATEGY_V1)
    assert trade.exit_day == t + timedelta(days=10)  # 7.º día sin negociación
    assert trade.ret == pytest.approx(60.0 * 0.9 / 50.0 * round_trip(1e8) - 1)
    assert trade.btc_ret == pytest.approx((100 + 15) / (100 + 6) * round_trip(5e6) - 1)
    # Si los datos se acaban antes de saber nada, la operación aún no tiene resultado.
    for series in panel.series.values():
        drop_days(series, {d for d in series.days if d > t + timedelta(days=6)})
    assert signals_book(panel, views, [t], STRATEGY_V1) == []


def test_a_candle_without_trades_is_not_a_price():
    """Vela con 0 operaciones (apartado 3.5): no hay cierre ese día; la posición se valora
    al último cierre × 0,90, no salta el stop y no se puede vender."""
    panel = make_panel()
    day = panel.days[150]
    s = panel.series["C01USDT"]
    i = s.at(day)
    last = s.close[i - 1]
    s.trades[i], s.close[i], s.low[i] = 0, last * 0.01, last * 0.01
    view = day_view(panel, day)
    assert "C01USDT" not in view.close and "C01USDT" not in view.low
    h = Holding("C01USDT", 1.0, last, panel.days[140], last)
    book = BookView(50.0, {"C01USDT": h}, 50.0 + last, 50.0 + last, 100.0)
    d = decide(view, book, STRATEGY_V1, LIMITS)
    assert "C01USDT" not in {o.symbol for o in d.sells}
    assert d.equity == pytest.approx(50.0 + 0.9 * last)


def test_never_uses_future_candles():
    """Canario: multiplicar todas las velas posteriores a t por factores al azar no cambia
    ninguna decisión ni el patrimonio hasta t."""
    panel = make_panel()
    days = days_from(panel)
    cfg = RunConfig(STRATEGY_V1, LIMITS, mode="dolares")
    base = simulate(panel, build_views(panel, days, STRATEGY_V1), days, cfg, keep_decisions=True)
    rng = random.Random(3)  # noqa: S311
    for cut in rng.sample(range(10, len(days) - 5), 5):
        t = days[cut]
        shocked = make_panel()
        for s in shocked.series.values():
            for i, day in enumerate(s.days):
                if day > t:
                    f = rng.uniform(0.2, 5)
                    s.open[i] *= f
                    s.close[i] *= f
                    s.high[i] *= f
                    s.low[i] *= f
        run = simulate(
            shocked, build_views(shocked, days, STRATEGY_V1), days, cfg, keep_decisions=True
        )
        before = [d for d in base.decisions if d.day <= t]
        after = [d for d in run.decisions if d.day <= t]
        assert [(d.day, [(o.symbol, o.side) for o in d.orders]) for d in before] == [
            (d.day, [(o.symbol, o.side) for o in d.orders]) for d in after
        ]
        assert [e for day, e in base.curve if day <= t] == pytest.approx(
            [e for day, e in run.curve if day <= t]
        )


def test_orders_execute_at_the_next_open_with_costs():
    panel = make_panel(drift=lambda k: 0.01 if k == 1 else 0.0)
    days = days_from(panel)
    cfg = RunConfig(STRATEGY_V1, LIMITS, mode="dolares")
    result = simulate(panel, build_views(panel, days, STRATEGY_V1), days, cfg)
    assert result.trades
    for trade in result.trades:
        assert trade.decided_day is not None and trade.entry_day == trade.decided_day + timedelta(1)
        s = panel.series[trade.symbol]
        assert trade.entry_price == s.open[s.at(trade.entry_day)]
        assert trade.qty * trade.entry_price < trade.cost  # comisión y diferencial
    assert all(t.cost <= 25 for t in result.trades)  # tope por posición


def test_one_day_delay_executes_two_days_later():
    panel = make_panel(drift=lambda k: 0.01 if k == 1 else 0.0)
    days = days_from(panel)
    cfg = RunConfig(STRATEGY_V1, LIMITS, mode="dolares", exec_delay=2)
    result = simulate(panel, build_views(panel, days, STRATEGY_V1), days, cfg)
    assert all(t.entry_day == t.decided_day + timedelta(2) for t in result.trades)


def test_delisted_position_is_closed_at_the_last_close_minus_10_percent():
    panel = make_panel(drift=lambda k: 0.02 if k == 3 else 0.0)
    days = days_from(panel)
    cfg = RunConfig(STRATEGY_V1, LIMITS, mode="dolares")
    first = simulate(panel, build_views(panel, days, STRATEGY_V1), days, cfg)
    held = next(t for t in first.trades if t.symbol == "C03USDT")
    # La moneda deja de cotizar dos días después de comprarla.
    cut = held.entry_day + timedelta(days=2)
    s = panel.series["C03USDT"]
    keep = [i for i, d in enumerate(s.days) if d <= cut]
    for attr in ("days", "open", "high", "low", "close", "quote_volume", "trades"):
        setattr(s, attr, [getattr(s, attr)[i] for i in keep])
    s._episode = []
    result = simulate(panel, build_views(panel, days, STRATEGY_V1), days, cfg)
    trade = next(t for t in result.trades if t.symbol == "C03USDT")
    assert trade.exit_purpose == "retirada"
    assert trade.exit_day == cut + timedelta(days=7)
    assert trade.proceeds == pytest.approx(trade.qty * s.close[-1] * 0.9)


def test_percent_mode_grows_without_the_dollar_cap():
    panel = make_panel(drift=lambda k: 0.01)
    days = days_from(panel)
    cfg = RunConfig(STRATEGY_V1, LIMITS, mode="porcentaje")
    result = simulate(panel, build_views(panel, days, STRATEGY_V1), days, cfg)
    assert result.final > 150 and max(t.cost for t in result.trades) > 25


def test_signals_book_compares_with_btc_in_the_same_window():
    panel = make_panel()
    days = days_from(panel)[:60]
    trades = signals_book(panel, build_views(panel, days, STRATEGY_V1), days, STRATEGY_V1)
    per_day: dict = {}
    for tr in trades:
        per_day[tr.day] = per_day.get(tr.day, 0) + 1
    assert trades and max(per_day.values()) <= 5  # las 5 primeras con tendencia
    t = trades[0]
    s, b = panel.series[t.symbol], panel.series["BTCUSDT"]
    entry, exit_ = t.day + timedelta(1), t.day + timedelta(8)
    gross = s.open[s.at(exit_)] / s.open[s.at(entry)]
    assert t.ret < gross - 1  # con costes
    btc_gross = b.open[b.at(exit_)] / b.open[b.at(entry)]
    assert t.btc_ret == pytest.approx(btc_gross - 1, abs=0.01)
    assert t.excess == pytest.approx(math.log1p(t.ret) - math.log1p(t.btc_ret))

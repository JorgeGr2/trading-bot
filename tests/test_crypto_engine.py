"""Motor del backtest con paneles sintéticos: sin mirar el futuro, ejecución en la
apertura siguiente, retiradas y libro de señales."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest

from copybot.config import RiskLimits
from copybot.crypto.engine import RunConfig, build_views, signals_book, simulate
from copybot.crypto.panel import Panel, Series
from copybot.crypto.strategy import STRATEGY_V1

START = date(2024, 1, 1)
LIMITS = RiskLimits()


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

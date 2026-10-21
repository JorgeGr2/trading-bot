"""Estadística del backtest (cifras a mano y reproducibilidad)."""

from __future__ import annotations

from decimal import Decimal

from copybot.backtest.stats import max_drawdown_pct, trade_stats

D = Decimal


def test_stats_by_hand():
    pnls = [D(5), D(-2), D(3), D(-1)]
    returns = [D(50), D(-20), D(30), D(-10)]
    s = trade_stats(pnls, returns)
    assert s.n == 4 and s.total_usd == 5
    assert s.mean_return_pct == D("12.5") and s.median_return_pct == D(10)
    assert s.win_rate == D("0.5") and s.profit_factor == D(8) / D(3)
    assert s.ci_low_pct < s.mean_return_pct < s.ci_high_pct
    assert "insuficiente" in s.warning


def test_bootstrap_is_reproducible_with_the_seed():
    returns = [D(i % 7 - 3) for i in range(60)]
    a = trade_stats(returns, returns, seed=3)
    b = trade_stats(returns, returns, seed=3)
    c = trade_stats(returns, returns, seed=4)
    assert (a.ci_low_pct, a.ci_high_pct) == (b.ci_low_pct, b.ci_high_pct)
    assert (a.ci_low_pct, a.ci_high_pct) != (c.ci_low_pct, c.ci_high_pct)
    assert "pequeña" in a.warning


def test_empty_and_drawdown():
    assert trade_stats([], []).n == 0
    curve = [(0, D(100)), (1, D(120)), (2, D(90)), (3, D(130)), (4, D(117))]
    assert max_drawdown_pct(curve) == 25

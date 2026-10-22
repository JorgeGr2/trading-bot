"""Protocolo del backtest con paneles sintéticos: controles positivo y negativo, y que
todas las piezas se calculan y son reproducibles."""

from __future__ import annotations

import statistics

from copybot.config import RiskLimits
from copybot.crypto.backtest import calibration, run_backtest
from copybot.crypto.engine import build_views
from copybot.crypto.strategy import STRATEGY_V1
from tests.test_crypto_engine import make_panel

LIMITS = RiskLimits()


def test_negative_control_without_signal():
    """Paseos aleatorios sin deriva: el IC de rangos no se aleja de cero."""
    panel = make_panel(n=24, days=320, seed=11)
    days = panel.days[130:300]
    views = build_views(panel, days, STRATEGY_V1)
    _, ic, _ = calibration(panel, views, days)
    assert abs(ic["media"]) < 0.1


def test_positive_control_with_persistent_drift():
    """Monedas con deriva persistente y distinta: la puntuación la detecta (IC > 0) y los
    quintiles crecen."""
    panel = make_panel(n=24, days=320, seed=11, drift=lambda k: (k - 12) * 0.0015)
    days = panel.days[130:300]
    views = build_views(panel, days, STRATEGY_V1)
    _, ic, quint = calibration(panel, views, days)
    assert ic["media"] > 0.2 and ic["ic"][0] > 0
    assert quint[-1] > quint[0]


def test_full_protocol_runs_and_is_reproducible():
    panel = make_panel(n=24, days=330, seed=5, drift=lambda k: (k - 12) * 0.001)
    end = panel.days[-10]
    a = run_backtest(panel, end=end, limits=LIMITS, null_seeds=5)
    b = run_backtest(panel, end=end, limits=LIMITS, null_seeds=5)
    assert a.curve_a.curve == b.curve_a.curve and a.null_finals == b.null_finals
    assert a.data_hash == b.data_hash and len(a.data_hash) == 64
    assert set(a.stress) >= {"costes_x2", "un_dia_tarde", "sin_top3", "solo_supervivientes"}
    assert set(a.benchmarks) == {"usdt", "btc", "btc_filtro", "btc90", "cesta10"}
    assert a.excess_a.ci_low <= a.excess_a.annual <= a.excess_a.ci_high
    assert 0 <= a.null_percentile <= 100 and len(a.null_finals) == 5
    assert a.signals and a.signals_excess["operaciones"] == len(a.signals)
    assert 28 in a.starts and 0 <= a.starts[28]["pierden"] <= 1
    # Más costes nunca mejoran el resultado.
    assert a.stress["costes_x2"].final <= a.curve_a.final
    assert statistics.fmean(a.universe_sizes.values()) >= 20

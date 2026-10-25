"""Protocolo del backtest con paneles sintéticos: controles positivo y negativo, y que
todas las piezas se calculan y son reproducibles."""

from __future__ import annotations

import statistics
from datetime import date, timedelta

import pytest

from copybot.backtest import stats
from copybot.config import RiskLimits
from copybot.crypto import costs
from copybot.crypto.backtest import (
    Period,
    _forward,
    _slice,
    calibration,
    curve_metrics,
    evaluation_days,
    run_backtest,
    signals_summary,
)
from copybot.crypto.benchmarks import top10_basket
from copybot.crypto.engine import RunConfig, build_views, signals_book, simulate
from copybot.crypto.strategy import STRATEGY_V1
from tests.test_crypto_engine import (
    COINS,
    drop_days,
    flat_panel,
    make_panel,
    manual_view,
    round_trip,
)

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
    a = run_backtest(panel, end=end, limits=LIMITS, null_seeds=5, resamples=300)
    b = run_backtest(panel, end=end, limits=LIMITS, null_seeds=5, resamples=300)
    assert a.curve_a.curve == b.curve_a.curve and a.null_finals == b.null_finals
    assert a.null_sharpes == b.null_sharpes and a.trades_ci == b.trades_ci
    assert a.data_hash == b.data_hash and len(a.data_hash) == 64
    assert set(a.stress) >= {"costes_x2", "un_dia_tarde", "sin_top3", "solo_supervivientes"}
    assert set(a.benchmarks) == {"usdt", "btc", "btc_filtro", "btc90", "cesta10"}
    assert a.excess_a.ci_low <= a.excess_a.annual <= a.excess_a.ci_high
    assert 0 <= a.null_percentile <= 100 and len(a.null_finals) == 5
    # Percentil también por Sharpe, y operaciones con bootstrap por semana ISO de entrada.
    assert len(a.null_sharpes) == 5 and 0 <= a.null_sharpe_percentile <= 100
    closed = [t for t in a.curve_a.trades if t.exit_day is not None]
    ci = a.trades_ci
    assert ci["operaciones"] == len(closed) and ci["semanas"] >= 2
    assert ci["ic"][0] <= ci["media"] <= ci["ic"][1]
    assert a.end == end and a.requested_end is None
    assert a.signals and a.signals_excess["operaciones"] == len(a.signals)
    assert 28 in a.starts and 0 <= a.starts[28]["pierden"] <= 1
    # Más costes nunca mejoran el resultado.
    assert a.stress["costes_x2"].final <= a.curve_a.final
    assert statistics.fmean(a.universe_sizes.values()) >= 20


def test_report_and_semaforo_render_without_promises():
    from copybot.crypto import btreport, semaforo
    from copybot.crypto.backtest import evidence
    from copybot.crypto.explain import check_text

    panel = make_panel(n=24, days=330, seed=5)
    asked = panel.days[-1] + timedelta(days=20)  # más allá de los datos
    r = run_backtest(panel, end=asked, limits=LIMITS, null_seeds=3, resamples=300)
    assert r.end == panel.days[-1] and r.requested_end == asked
    sem = semaforo.evaluate(
        evidence(r, dsr=0.5, prereg_ok=True), None, version="cripto-v1:x", huella="h"
    )
    assert sem.luces.backtest == "gris"  # menos de 3 años de datos
    text = btreport.render(
        r, k=3, dsr=0.5, min_trl=None, commit=None, semaforo_md=btreport.semaforo_section(sem)
    )
    check_text(text)
    assert "Backtest de la regla cripto-v1" in text and "Semáforo" in text
    assert "los datos no llegan" in text
    assert "Por Sharpe" in text and "semanas de entrada" in text


def test_max_drawdown_uses_close_peak_and_intraday_trough():
    from datetime import date

    from copybot.crypto.backtest import max_drawdown

    d = [date(2024, 1, k) for k in (1, 2, 3)]
    closes = [(d[0], 100.0), (d[1], 120.0), (d[2], 110.0)]
    lows = [(d[0], 100.0), (d[1], 90.0), (d[2], 84.0)]
    # Pico de E = 120 (día 2); peor W después = 84 → 30 %. Con el pico de W saldría 16 %.
    assert abs(max_drawdown(closes, lows) - 0.30) < 1e-12
    assert abs(max_drawdown(closes) - 1 / 12) < 1e-12


def test_forward_return_waits_for_the_first_traded_open():
    """Calibración e IC: si en t+8 no hay negociación (vela con 0 operaciones o sin vela),
    se sale en la primera apertura negociada después, no se descarta la moneda."""
    panel = flat_panel(
        ["BTCUSDT", "C01USDT", "C02USDT"],
        40,
        price=lambda s, d: 100.0 if d < 14 or s == "BTCUSDT" else 50.0,
    )
    t = panel.days[5]
    view = manual_view(panel, t, ["C01USDT", "C02USDT"])
    end = panel.days[-1]
    s1 = panel.series["C01USDT"]
    s1.trades[13], s1.open[13] = 0, 0.01  # t+8: vela sin operaciones (precio sin valor)
    r, sold_on = _forward(panel, "C01USDT", t, 7, view, end=end)
    assert sold_on == t + timedelta(days=9) and r == pytest.approx(0.5 * round_trip(1e8) - 1)
    drop_days(panel.series["C02USDT"], {t + timedelta(days=8), t + timedelta(days=9)})
    r, sold_on = _forward(panel, "C02USDT", t, 7, view, end=end)
    assert sold_on == t + timedelta(days=10) and r == pytest.approx(0.5 * round_trip(1e8) - 1)
    assert _forward(panel, "C02USDT", t, 7, view, end=t + timedelta(days=9)) is None


def test_evaluation_stops_at_the_last_btc_candle():
    """Si los datos acaban antes del final pedido, la evaluación acaba con ellos: si no,
    las posiciones abiertas se darían por retiradas sin serlo."""
    panel = make_panel(n=24, days=200, seed=1, drift=lambda k: 0.003)
    views = build_views(panel, panel.days, STRATEGY_V1)
    days = evaluation_days(panel, views, panel.days[-1] + timedelta(days=30))
    assert days[-1] == panel.days[-1]
    run = simulate(panel, views, days, RunConfig(STRATEGY_V1, LIMITS, mode="porcentaje"))
    assert any(t.exit_day is None for t in run.trades)  # quedan posiciones abiertas
    assert not any(t.exit_purpose == "retirada" for t in run.trades)


def test_periods_and_months_keep_their_first_return():
    d = date
    curve = [(d(2024, 1, 15), 100.0), (d(2024, 1, 31), 90.0), (d(2024, 2, 29), 99.0)]
    assert curve_metrics(curve)["peor_mes"] == pytest.approx(-0.10)  # enero: 100 → 90
    february = _slice(curve, Period("febrero", d(2024, 2, 1), d(2024, 2, 29)))
    assert february[0] == (d(2024, 1, 31), 90.0)  # la base del primer día
    assert curve_metrics(february)["total"] == pytest.approx(0.10)


def test_basket_rebalances_by_difference_with_each_coin_tier():
    coins = COINS[:10]
    panel = flat_panel(["BTCUSDT", *coins], 12)
    qv = {s: 3e8 if k < 5 else 6e6 for k, s in enumerate(coins)}
    views = {day: manual_view(panel, day, coins, qv=qv) for day in panel.days}
    basket = top10_basket(panel, views, panel.days, every=2)
    # Primera compra: 10 $ por moneda, cada una con el coste del tramo de su volumen.
    c = {s: costs.cost_per_side(qv[s]) for s in coins}
    bought = sum(10 / ((1 + c[s]) * (1 + costs.FEE)) for s in coins)
    assert basket.curve[1][1] == pytest.approx(bought, rel=1e-12)
    # Mismos miembros y precios: reequilibrar por diferencia casi no cuesta nada.
    assert basket.curve[-1][1] > bought - 0.001


def test_bootstraps_use_5000_resamples_by_default(monkeypatch):
    seen = []
    real = stats.stationary_bootstrap_ci

    def spy(values, **kw):
        seen.append(kw["resamples"])
        return real(values, **(kw | {"resamples": 20}))

    monkeypatch.setattr(stats, "stationary_bootstrap_ci", spy)
    panel = make_panel(n=24, days=200, seed=11)
    days = panel.days[130:180]
    views = build_views(panel, days, STRATEGY_V1)
    calibration(panel, views, days)
    signals_summary(signals_book(panel, views, days, STRATEGY_V1))
    assert len(seen) > 3 and set(seen) == {5000}  # apartado 8.5

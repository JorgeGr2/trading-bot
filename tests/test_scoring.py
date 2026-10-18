from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from pydantic import ValidationError

from copybot.analysis.metrics import AnalysisParams, compute_window_metrics
from copybot.analysis.scoring import (
    ScoringParams,
    metrics_params_hash,
    score_params_hash,
    score_wallet,
)
from tests.analysis_cases import AS_OF

P = ScoringParams(_env_file=None)
EMPTY = compute_window_metrics(
    [], as_of=AS_OF, window_days=30, params=AnalysisParams(_env_file=None), coverage=None
)
D = Decimal

# Métricas elegidas a mano para poder calcular la puntuación a mano.
GOOD = replace(
    EMPTY,
    coverage_complete=True,
    buys=60,
    sells=60,
    trades_per_day=D(4),
    round_trips=40,
    clipped_mean_return=D("0.16"),
    mean_return=D("0.18"),
    prudent_round_trips=40,
    prudent_mean_return=D("0.15"),
    prudent_stdev_return=D("0.4"),
    prudent_mean_without_top3=D("0.1"),
    first_half_mean_return=D("0.1"),
    second_half_mean_return=D("0.2"),
    pnl_without_top3_usd=D(50),
    median_seconds_to_first_sell=D(600),
    launch_checked=20,
    early_buys=0,
    early_buy_share=D(0),
    max_drawdown_pct=D("0.3"),
    max_drawdown_usd=D(30),
    peak_open_cost_usd=D(100),
    buy_volume_usd=D(1000),
    sell_volume_usd=D(1100),
    unmatched_sell_share=D("0.05"),
)
RECENT = replace(EMPTY, window_days=7, prudent_mean_return=D("0.05"))


def test_score_by_hand():
    result = score_wallet(GOOD, RECENT, P)
    assert result.eligible and not result.exclusions
    comps = {k: v["valor"] for k, v in result.components.items()}
    # Ventaja: 0,15 − 1 · 0,4/√40 = 0,08675; − 3 % de coste = 0,05675; / 10 % = 0,5675
    assert comps["ventaja"] == pytest.approx(D("0.567544468"), abs=D("1e-9"))
    assert comps["consistencia"] == 1
    assert comps["copiable"] == 1
    assert comps["riesgo"] == D("0.5")  # 1 − 0,3/0,6
    assert comps["datos"] == D("0.95")
    # Calidad: 0,25·1 + 0,35·1 + 0,25·0,5 + 0,15·0,95 = 0,8675
    assert result.quality == D("0.8675")
    # 100 · 0,567544 · 0,8675 = 49,23
    assert result.score == D("49.23")
    text = "\n".join(result.explanation())
    assert text.startswith("Puntuación 49.23/100.")
    assert "tras restar el coste estimado de copiar (3%)" in text


@pytest.mark.parametrize(
    ("change", "phrase"),
    [
        ({"coverage_complete": False}, "historial importado no cubre"),
        ({"round_trips": 19}, "muestra insuficiente"),
        ({"median_seconds_to_first_sell": D(45)}, "no copiable"),
        ({"early_buy_share": D("0.5"), "early_buys": 10}, "sniping"),
        ({"unmatched_sell_share": D("0.4")}, "datos incompletos"),
        ({"trades_per_day": D(400)}, "frecuencia imposible"),
    ],
)
def test_exclusions(change, phrase):
    result = score_wallet(replace(GOOD, **change), RECENT, P)
    assert not result.eligible and result.score is None
    assert any(phrase in reason for reason in result.exclusions)
    assert result.explanation()[0] == "Excluido de la puntuación:"


def test_sniping_needs_enough_checks_to_exclude():
    few = replace(GOOD, launch_checked=3, early_buys=3, early_buy_share=D(1))
    result = score_wallet(few, RECENT, P)
    assert result.eligible  # con 3 comprobaciones no se excluye…
    assert "sniping_sin_comprobar" in {f.code for f in result.flags}  # …pero se avisa


def test_abandoned_positions_count_against_the_edge():
    """Quien solo cierra sus aciertos no puede puntuar por ellos: la ventaja sale de la
    serie prudente, que incluye sus posiciones abandonadas."""
    bag_holder = replace(GOOD, clipped_mean_return=D("0.5"), prudent_mean_return=D("-0.2"))
    result = score_wallet(bag_holder, RECENT, P)
    assert result.components["ventaja"]["valor"] == 0 and result.score == 0


def test_flags():
    risky = replace(
        GOOD,
        quick_exit_share=D("0.8"),
        trades_per_day=D(80),
        free_tokens_sold_usd=D(200),
        same_slot_exits=2,
        top1_profit_share=D("0.7"),
        stale_open_cost_usd=D(300),
        round_trips=25,
        non_usd_trades=3,
    )
    codes = {f.code for f in score_wallet(risky, RECENT, P).flags}
    assert codes >= {
        "salidas_rapidas",
        "alta_frecuencia",
        "vende_tokens_recibidos",
        "misma_ranura",
        "beneficio_concentrado",
        "posiciones_abandonadas",
        "muestra_pequeña",
        "operaciones_no_usd",
    }


def test_without_edge_the_score_is_zero_however_good_the_rest():
    result = score_wallet(replace(GOOD, prudent_mean_return=D("0.01")), RECENT, P)
    assert result.components["ventaja"]["valor"] == 0
    assert result.quality > D("0.8")
    assert result.eligible and result.score == 0


def test_params_are_validated():
    with pytest.raises(ValidationError):
        ScoringParams(_env_file=None, weight_data=D("0.5"))  # ya no suman 1
    with pytest.raises(ValidationError):
        ScoringParams(_env_file=None, recent_window_days=40)


def test_params_hash_identifies_the_version():
    a = AnalysisParams(_env_file=None)
    assert score_params_hash(a, P) == score_params_hash(a, ScoringParams(_env_file=None))
    assert score_params_hash(a, P) != score_params_hash(a, ScoringParams(_env_file=None, z=D(2)))
    # Las métricas no dependen de los parámetros de puntuación.
    assert metrics_params_hash(a) == metrics_params_hash(AnalysisParams(_env_file=None))
    assert metrics_params_hash(a) != metrics_params_hash(
        AnalysisParams(_env_file=None, stale_open_hours=24)
    )


def test_scoring_is_deterministic():
    first = score_wallet(GOOD, RECENT, P)
    second = score_wallet(GOOD, RECENT, P)
    assert first.score == second.score
    assert first.explanation() == second.explanation()

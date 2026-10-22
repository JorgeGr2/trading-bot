"""Semáforo «¿Es rentable?» (apartado 9 del diseño): colores, prioridades, revisiones y JSON."""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import replace
from datetime import date

import pytest

from copybot.crypto.semaforo import (
    AMBAR,
    AVISO,
    GRIS,
    MAX_REVIEWS,
    MSG_ELEGIR_NO_APORTA,
    MSG_PEOR_QUE_BTC,
    MSG_SIMPLE_GANA,
    MSG_SIN_VENTAJA,
    ROJO,
    SEQUENTIAL_Z,
    VERDE,
    BacktestEvidence,
    Criterion,
    LiveEvidence,
    backtest_light,
    conclusion,
    evaluate,
    live_light,
    next_review_date,
    review_due,
    reviews_due,
    reviews_exhausted,
)

# Un backtest que cumple todo: con él la luz del backtest es VERDE.
GOOD_BT = BacktestEvidence(
    years_evaluable=6.5,
    share_days_small_universe=0.01,
    coverage=0.995,
    delisted_pairs_in_universe=14,
    prereg_hash_ok=True,
    excess_annual=0.09,
    excess_annual_costs_x2=0.05,
    excess_annual_delay_1d=0.06,
    excess_without_top3=0.03,
    excess_first_half=0.08,
    excess_second_half=0.04,
    excess_2023_on=0.02,
    ci_low_by_block={7: 0.02, 28: 0.01, 56: 0.005},
    dsr=0.97,
    null_percentile=98.0,
    signals_book_mean_excess=0.006,
    signals_book_ci_low=0.001,
    max_drawdown=0.22,
    archive_imported=True,
    btc_filter_beats_strategy=False,
)

# Cartera en vivo que cumple todo en la 4.ª revisión (día 112): luz en vivo VERDE.
GOOD_LIVE = LiveEvidence(
    days=112,
    signals_closed_trades=420,
    portfolio_closed_trades=45,
    limits_match=True,
    halted_50=False,
    fidelity_unexplained_days=0,
    mean_exec_gap_over_model=1.2,
    availability=0.997,
    review_number=4,
    is_review_day=True,
    cumulative_excess_ci_low_portfolio=0.03,
    cumulative_excess_ci_low_signals=0.002,
    max_drawdown=0.12,
    excess_without_top3=0.01,
    excess_first_half=0.02,
    excess_second_half=0.015,
    duplicate_orders=0,
    below_p5_of_starts=False,
    min_trl_days=300,
)

FORBIDDEN = ("seguro que", "va a subir", "sin riesgo", "asegur", "la más rentable")


def _bt_color(**changes) -> str:
    return backtest_light(replace(GOOD_BT, **changes))[0]


def _live_color(backtest_color: str = VERDE, **changes) -> str:
    return live_light(replace(GOOD_LIVE, **changes), backtest_color)[0]


def _by_id(criterios: list[Criterion] | tuple[Criterion, ...]) -> dict[str, Criterion]:
    return {c.id: c for c in criterios}


def _evaluate(bt=GOOD_BT, live=GOOD_LIVE, **kwargs):
    kwargs.setdefault("version", "cripto-v1:abc123")
    kwargs.setdefault("huella", "0123456789abcdef")
    return evaluate(bt, live, **kwargs)


# --- Luz del backtest -----------------------------------------------------------------------


def test_backtest_green_when_every_criterion_passes():
    color, criterios, mensajes = backtest_light(GOOD_BT)
    assert color == VERDE
    assert all(c.cumple for c in criterios)
    ids = [c.id for c in criterios]
    assert ids == [
        *("BG1", "BG2", "BG3", "BG4", "BG5"),
        *("BR1", "BR2", "BR3"),
        *("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8"),
    ]
    assert mensajes == []


@pytest.mark.parametrize(
    "changes",
    [
        {"years_evaluable": 2.99},
        {"share_days_small_universe": 0.051},
        {"coverage": 0.979},
        {"delisted_pairs_in_universe": 0},
        {"prereg_hash_ok": False},
        {"years_evaluable": math.nan},  # dato que no es un número: en contra
    ],
)
def test_backtest_gray_when_data_is_insufficient(changes):
    assert _bt_color(**changes) == GRIS


def test_backtest_gray_without_backtest():
    color, criterios, mensajes = backtest_light(None)
    assert color == GRIS
    assert [c.id for c in criterios] == ["BG0"]
    assert not criterios[0].cumple
    assert mensajes == []


@pytest.mark.parametrize(
    "changes",
    [
        {"excess_annual": 0.0},  # exceso ≤ 0 con los costes normales
        {"excess_annual": -0.04},
        {"excess_annual_costs_x2": 0.0},  # exceso ≤ 0 con costes ×2
        {"max_drawdown": 0.50},  # la cartera de 100 $ se habría parado
        {"max_drawdown": 0.81},
    ],
)
def test_backtest_red_with_enough_data(changes):
    assert _bt_color(**changes) == ROJO


def test_backtest_red_needs_enough_data():
    # «ROJO (con datos suficientes)»: sin ellos, gris, aunque pierda frente a BTC.
    assert _bt_color(years_evaluable=2.0, excess_annual=-0.10) == GRIS


@pytest.mark.parametrize(
    ("changes", "failing"),
    [
        ({"ci_low_by_block": {7: 0.02, 28: -0.001, 56: 0.01}}, "B1"),
        ({"ci_low_by_block": {7: 0.02, 28: 0.01}}, "B1"),  # falta L = 56: en contra
        ({"ci_low_by_block": {7: 0.02, 28: 0.01, 56: 0.0}}, "B1"),
        ({"dsr": 0.949}, "B2"),
        ({"dsr": None}, "B2"),
        ({"excess_annual_delay_1d": 0.0}, "B3"),
        ({"excess_without_top3": -0.01}, "B4"),
        ({"excess_first_half": 0.0}, "B4"),
        ({"excess_second_half": None}, "B4"),
        ({"excess_2023_on": -0.02}, "B4"),
        ({"null_percentile": 95.0}, "B5"),  # hay que **superar** el percentil 95
        ({"null_percentile": 40.0}, "B5"),
        ({"signals_book_mean_excess": 0.0}, "B6"),
        ({"signals_book_ci_low": -0.001}, "B6"),
        ({"max_drawdown": 0.25}, "B7"),  # 25-50 %: ámbar
        ({"max_drawdown": 0.4999}, "B7"),
        ({"max_drawdown": None}, "B7"),  # sin dato: ni verde ni rojo
        ({"archive_imported": False}, "B8"),  # sin el archivo, como mucho ámbar
    ],
)
def test_backtest_amber_when_a_green_criterion_fails(changes, failing):
    color, criterios, _ = backtest_light(replace(GOOD_BT, **changes))
    assert color == AMBAR
    assert not _by_id(criterios)[failing].cumple


def test_backtest_missing_core_metric_is_never_green_nor_red():
    color, criterios, mensajes = backtest_light(replace(GOOD_BT, excess_annual=None))
    assert color == AMBAR
    by_id = _by_id(criterios)
    assert not by_id["BR1"].cumple
    assert by_id["BR1"].valor == "sin datos"
    assert MSG_PEOR_QUE_BTC not in mensajes


def test_drawdown_thresholds_green_amber_red():
    assert _bt_color(max_drawdown=0.2499) == VERDE
    assert _bt_color(max_drawdown=0.25) == AMBAR
    assert _bt_color(max_drawdown=0.4999) == AMBAR
    assert _bt_color(max_drawdown=0.50) == ROJO


def test_choosing_a_coin_does_not_help_message():
    color, criterios, mensajes = backtest_light(replace(GOOD_BT, null_percentile=60.0))
    by_id = _by_id(criterios)
    assert all(by_id[i].cumple for i in ("B1", "B2", "B3", "B4"))
    assert not by_id["B5"].cumple
    assert color == AMBAR
    assert mensajes == [MSG_ELEGIR_NO_APORTA]
    assert MSG_ELEGIR_NO_APORTA == (
        "Elegir moneda no aporta: lo que funciona es salir a USDT a tiempo. "
        "La versión simple (BTC con filtro) es mejor."
    )


def test_choosing_a_coin_message_needs_b1_to_b4_and_a_measured_null():
    # Si ya falla B4, el mensaje no aplica.
    _, _, mensajes = backtest_light(replace(GOOD_BT, null_percentile=60.0, excess_2023_on=-0.01))
    assert MSG_ELEGIR_NO_APORTA not in mensajes
    # Sin el nulo medido no se afirma nada (pero B5 sigue sin cumplirse).
    color, criterios, mensajes = backtest_light(replace(GOOD_BT, null_percentile=None))
    assert MSG_ELEGIR_NO_APORTA not in mensajes
    assert color == AMBAR
    assert not _by_id(criterios)["B5"].cumple


def test_simple_version_wins_message():
    color, _, mensajes = backtest_light(replace(GOOD_BT, btc_filter_beats_strategy=True))
    assert mensajes == [MSG_SIMPLE_GANA]
    assert MSG_SIMPLE_GANA == "La versión simple gana."
    assert color == VERDE  # es un mensaje, no un criterio


def test_header_when_the_past_did_not_beat_btc():
    color, _, mensajes = backtest_light(replace(GOOD_BT, excess_annual=0.0))
    assert color == ROJO
    assert mensajes[0] == MSG_PEOR_QUE_BTC
    assert MSG_PEOR_QUE_BTC == "En el pasado, esta regla lo hizo peor que comprar BTC y esperar."
    # También con datos insuficientes: es un hecho medido.
    color, _, mensajes = backtest_light(replace(GOOD_BT, excess_annual=-0.05, coverage=0.9))
    assert color == GRIS
    assert MSG_PEOR_QUE_BTC in mensajes


def test_backtest_messages_keep_a_fixed_order():
    bt = replace(GOOD_BT, null_percentile=50.0, btc_filter_beats_strategy=True)
    assert backtest_light(bt)[2] == [MSG_ELEGIR_NO_APORTA, MSG_SIMPLE_GANA]


# --- Luz en vivo ----------------------------------------------------------------------------


def test_live_green_when_everything_passes():
    color, criterios = live_light(GOOD_LIVE, VERDE)
    assert color == VERDE
    assert all(c.cumple for c in criterios)
    assert [c.id for c in criterios] == [
        *("VG1", "VG2", "VG3", "VG4"),
        *("VR1", "VR2", "VR3", "VR4", "VR5"),
        *("V1", "V2", "V3", "V4", "V5"),
    ]


def test_live_gray_without_live_data():
    color, criterios = live_light(None, VERDE)
    assert color == GRIS
    assert [c.id for c in criterios] == ["VG0"]


@pytest.mark.parametrize(
    "changes",
    [
        {"signals_closed_trades": 99},
        {"portfolio_closed_trades": 29},
        {"limits_match": False},
    ],
)
def test_live_gray_when_samples_are_insufficient(changes):
    assert _live_color(**changes) == GRIS


def test_live_gray_with_less_than_28_days_even_if_everything_else_passes():
    for days in (1, 9, 27):
        live = replace(GOOD_LIVE, days=days, review_number=0, is_review_day=False)
        color, criterios = live_light(live, VERDE)
        assert color == GRIS
        assert not _by_id(criterios)["VG1"].cumple
    assert _by_id(live_light(replace(GOOD_LIVE, days=9), VERDE)[1])["VG1"].valor == "9 de 28"


@pytest.mark.parametrize(
    "changes",
    [
        {"halted_50": True},
        {"fidelity_unexplained_days": 1},
        {"mean_exec_gap_over_model": 2.01},
        {"availability": 0.989},  # a partir del día 28
        {"below_p5_of_starts": True},  # en una revisión: la regla se ha roto
    ],
)
def test_live_red(changes):
    assert _live_color(**changes) == ROJO


@pytest.mark.parametrize(
    "changes",
    [
        {"halted_50": True},
        {"fidelity_unexplained_days": 2},
        {"mean_exec_gap_over_model": 3.5},
    ],
)
def test_live_red_has_priority_over_gray(changes):
    early = {"days": 9, "review_number": 0, "is_review_day": False, "signals_closed_trades": 20}
    assert _live_color(**early) == GRIS
    assert _live_color(**early, **changes) == ROJO


def test_low_availability_is_red_only_from_day_28():
    early = {"days": 20, "review_number": 0, "is_review_day": False}
    assert _live_color(availability=0.9, **early) == GRIS
    assert _live_color(availability=0.9, days=28, review_number=1) == ROJO


def test_below_p5_of_starts_counts_only_after_a_review():
    early = {"days": 20, "review_number": 0, "is_review_day": False}
    assert _live_color(below_p5_of_starts=True, **early) == GRIS


@pytest.mark.parametrize(
    ("changes", "failing"),
    [
        ({"cumulative_excess_ci_low_portfolio": 0.0}, "V2"),
        ({"cumulative_excess_ci_low_signals": -0.001}, "V2"),
        ({"cumulative_excess_ci_low_signals": None}, "V2"),  # no disponible: en contra
        ({"max_drawdown": 0.25}, "V3"),
        ({"max_drawdown": None}, "V3"),
        ({"excess_without_top3": 0.0}, "V4"),
        ({"excess_first_half": -0.01}, "V4"),
        ({"excess_second_half": None}, "V4"),
        ({"duplicate_orders": 1}, "V5"),
        ({"mean_exec_gap_over_model": None}, "VR3"),  # dudoso: ni rojo ni verde
        ({"availability": None}, "VR4"),
    ],
)
def test_live_amber_when_a_green_criterion_fails(changes, failing):
    color, criterios = live_light(replace(GOOD_LIVE, **changes), VERDE)
    assert color == AMBAR
    assert not _by_id(criterios)[failing].cumple


@pytest.mark.parametrize("backtest_color", [AMBAR, GRIS, ROJO])
def test_live_green_needs_a_green_backtest(backtest_color):
    color, criterios = live_light(GOOD_LIVE, backtest_color)
    assert color == AMBAR
    assert not _by_id(criterios)["V1"].cumple


# --- Revisiones secuenciales ----------------------------------------------------------------


def test_sequential_z_is_bonferroni_over_13_one_sided_reviews():
    assert MAX_REVIEWS == 13
    assert SEQUENTIAL_Z == 2.89
    z = statistics.NormalDist().inv_cdf(1 - 0.025 / MAX_REVIEWS)
    assert z == pytest.approx(SEQUENTIAL_Z, abs=0.005)
    v2 = _by_id(live_light(GOOD_LIVE, VERDE)[1])["V2"]
    assert "z = 2,89" in v2.umbral
    assert "cada 28 días" in v2.umbral


def test_reviews_only_every_28_days_and_at_most_13():
    assert [d for d in range(0, 500) if review_due(d)] == [28 * k for k in range(1, 14)]
    assert reviews_due(27) == 0
    assert reviews_due(28) == 1
    assert reviews_due(55) == 1
    assert reviews_due(364) == 13
    assert reviews_due(1000) == 13


def test_next_review_date():
    first = date(2026, 10, 23)  # día en vivo número 1
    assert next_review_date(first, 9) == date(2026, 11, 19)  # día 28
    assert next_review_date(first, 28) == date(2026, 12, 17)  # día 56
    assert next_review_date(first, 363) == date(2027, 10, 21)  # día 364, la 13.ª
    assert next_review_date(first, 364) is None


def test_interval_does_not_count_before_the_first_review():
    # Intervalos positivos, pero aún no ha habido revisión: no se miran.
    live = replace(GOOD_LIVE, days=40, review_number=0, is_review_day=False)
    color, criterios = live_light(live, VERDE)
    assert color == AMBAR
    v2 = _by_id(criterios)["V2"]
    assert not v2.cumple
    assert "ninguna revisión" in v2.valor


def test_reviews_beyond_the_schedule_do_not_count():
    # Más revisiones de las que tocan con esos días (una cada 28): no cuentan.
    assert _live_color(days=50, review_number=2, is_review_day=False) == AMBAR


def test_between_reviews_the_last_review_holds():
    live = replace(GOOD_LIVE, days=120, review_number=4, is_review_day=False)
    color, criterios = live_light(live, VERDE)
    assert color == VERDE
    assert "última revisión (4 de 13)" in _by_id(criterios)["V2"].valor


def test_13th_review_can_still_turn_green():
    live = replace(GOOD_LIVE, days=364, review_number=13)
    assert live_light(live, VERDE)[0] == VERDE
    assert not reviews_exhausted(live)
    sem = _evaluate(live=live)
    assert sem.semaforo == VERDE
    assert MSG_SIN_VENTAJA not in sem.mensajes


def test_after_13_reviews_without_green_live_can_never_be_green():
    at_13 = replace(GOOD_LIVE, days=364, review_number=13, cumulative_excess_ci_low_portfolio=-0.01)
    assert live_light(at_13, VERDE)[0] == AMBAR
    assert reviews_exhausted(at_13)
    sem = _evaluate(live=at_13, next_review_day=date(2027, 11, 18))
    assert "ventaja no demostrada en un año" in MSG_SIN_VENTAJA
    assert MSG_SIN_VENTAJA in sem.mensajes
    assert sem.revision.hechas == 13
    assert sem.revision.proxima is None  # ya no hay más revisiones

    # Días después, sin más revisiones: sigue sin poder ser verde.
    later = replace(at_13, days=500, is_review_day=False)
    assert live_light(later, VERDE)[0] == AMBAR
    assert MSG_SIN_VENTAJA in _evaluate(live=later).mensajes

    # Una 14.ª «revisión» con intervalos positivos no cuenta.
    extra = replace(GOOD_LIVE, days=392, review_number=14)
    color, criterios = live_light(extra, VERDE)
    assert color == AMBAR
    assert not _by_id(criterios)["V2"].cumple
    assert reviews_exhausted(extra)
    assert MSG_SIN_VENTAJA in _evaluate(live=extra).mensajes


# --- Conclusión y salida --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("light_bt", "light_live", "expected"),
    [
        (ROJO, GRIS, ROJO),
        (GRIS, ROJO, ROJO),
        (ROJO, VERDE, ROJO),
        (VERDE, ROJO, ROJO),
        (GRIS, VERDE, GRIS),
        (AMBAR, GRIS, GRIS),
        (GRIS, GRIS, GRIS),
        (VERDE, VERDE, VERDE),
        (VERDE, AMBAR, AMBAR),
        (AMBAR, VERDE, AMBAR),
        (AMBAR, AMBAR, AMBAR),
    ],
)
def test_conclusion_red_first_then_gray(light_bt, light_live, expected):
    assert conclusion(light_bt, light_live) == expected


def test_conclusion_rejects_unknown_colors():
    with pytest.raises(ValueError, match="color desconocido"):
        conclusion("amarillo", VERDE)
    with pytest.raises(ValueError, match="color desconocido"):
        live_light(GOOD_LIVE, "ámbar")


def test_red_backtest_wins_over_gray_live():
    live = replace(GOOD_LIVE, days=9, review_number=0, is_review_day=False)
    sem = _evaluate(bt=replace(GOOD_BT, excess_annual_costs_x2=-0.01), live=live)
    assert (sem.luces.backtest, sem.luces.en_vivo, sem.semaforo) == (ROJO, GRIS, ROJO)


def test_evaluate_both_green():
    sem = _evaluate()
    assert sem.semaforo == VERDE
    assert sem.aviso == AVISO == "Un semáforo en verde no garantiza beneficios futuros."
    assert sem.dias_necesarios == 300


def test_evaluate_without_any_data():
    sem = _evaluate(bt=None, live=None, next_review_day=None)
    assert (sem.semaforo, sem.luces.backtest, sem.luces.en_vivo) == (GRIS, GRIS, GRIS)
    assert sem.dias_necesarios is None
    assert sem.revision.hechas == 0
    assert sem.aviso == AVISO


def test_json_shape():
    live = replace(
        GOOD_LIVE,
        days=9,
        review_number=0,
        is_review_day=False,
        signals_closed_trades=37,
        portfolio_closed_trades=4,
        cumulative_excess_ci_low_portfolio=None,
        cumulative_excess_ci_low_signals=None,
        min_trl_days=1400,
    )
    bt = replace(GOOD_BT, archive_imported=False)
    sem = _evaluate(bt=bt, live=live, next_review_day=date(2026, 11, 18))
    text = sem.to_json()
    assert text.startswith(
        '{"semaforo":"gris","luces":{"backtest":"ambar","en_vivo":"gris"},'
        '"version":"cripto-v1:abc123","huella":"0123456789abcdef","criterios":[{"id":"BG1",'
    )
    assert "año" in text  # tildes y eñes tal cual, sin escapes
    data = json.loads(text)
    assert list(data) == [
        "semaforo",
        "luces",
        "version",
        "huella",
        "criterios",
        "mensajes",
        "dias_necesarios",
        "revision",
        "aviso",
    ]
    assert data["luces"] == {"backtest": "ambar", "en_vivo": "gris"}
    assert data["dias_necesarios"] == 1400
    assert data["revision"] == {"hechas": 0, "maximo": 13, "proxima": "2026-11-18"}
    assert data["aviso"] == AVISO
    assert data["mensajes"] == []
    for criterio in data["criterios"]:
        assert list(criterio) == ["id", "texto", "valor", "umbral", "cumple"]
        assert isinstance(criterio["cumple"], bool)
        assert all(isinstance(criterio[k], str) and criterio[k] for k in list(criterio)[:4])
    b1 = next(c for c in data["criterios"] if c["id"] == "B1")
    assert b1["texto"] == "Gana a BTC por un margen que no parece suerte"
    assert b1["valor"] == "+9 %/año (margen de error desde +0,5 %)"
    assert b1["umbral"] == "límite inferior > 0 con L = 7, 28 y 56"
    assert b1["cumple"] is True
    assert {data["semaforo"], *data["luces"].values()} <= {VERDE, AMBAR, ROJO, GRIS}


def test_spanish_number_format():
    bt = replace(GOOD_BT, excess_annual=-0.062, coverage=0.9925, delisted_pairs_in_universe=1234)
    by_id = _by_id(backtest_light(bt)[1])
    assert by_id["BR1"].valor == "−6,2 %/año"  # signo menos tipográfico
    assert by_id["BG3"].valor == "99,2 %"
    assert by_id["BG4"].valor == "1.234 pares retirados"
    assert by_id["B2"].valor == "0,97"
    assert by_id["B7"].umbral == "< 25 % (entre 25 y 50 %: ÁMBAR)"


def test_same_input_same_json():
    assert _evaluate().to_json() == _evaluate().to_json()


def test_texts_never_promise():
    cases = [
        _evaluate(),
        _evaluate(bt=replace(GOOD_BT, excess_annual=-0.1, null_percentile=10.0), live=None),
        _evaluate(bt=replace(GOOD_BT, null_percentile=10.0, btc_filter_beats_strategy=True)),
        _evaluate(live=replace(GOOD_LIVE, days=364, review_number=13, max_drawdown=None)),
        _evaluate(
            live=replace(
                GOOD_LIVE, days=392, review_number=14, cumulative_excess_ci_low_signals=None
            )
        ),
    ]
    for sem in cases:
        texts = [*sem.mensajes, *(f"{c.texto} {c.valor} {c.umbral}" for c in sem.criterios)]
        for text in texts:
            lowered = text.lower()
            assert "garantiz" not in lowered, text
            assert not any(expr in lowered for expr in FORBIDDEN), text
        assert sem.aviso == AVISO  # el único texto con «garantiza», y en negativo

"""Plantillas del informe en castellano llano (apartados 10.2 y 10.3 del diseño): formato
español, fechas en hora de Madrid, métricas informativas, nivel de riesgo, motivos de venta,
confianza en la regla, escenario de desplome y expresiones prohibidas."""

from __future__ import annotations

import ast
import enum
import math
import random
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from copybot.crypto import explain
from copybot.crypto.explain import (
    ALLOWED_DISCLAIMERS,
    CONFIDENCE_LEVELS,
    FORBIDDEN,
    SELL_PURPOSES,
    bad_week_text,
    best_day,
    best_day_share,
    breadth,
    breadth_text,
    buy_reason,
    check_text,
    coin_status,
    confidence,
    confidence_text,
    crash_scenario,
    crash_text,
    cutoff_text,
    fmt_clock,
    fmt_date_long,
    fmt_date_short,
    fmt_musd,
    fmt_num,
    fmt_pct,
    fmt_points,
    fmt_pp,
    fmt_trend,
    fmt_usd,
    fmt_vol,
    fmt_weekday_day,
    madrid_time,
    momentum_percentile,
    p5_week,
    ret,
    risk_level,
    risk_reasons,
    sell_reason,
    single_day_warning,
    sma_text,
    why_text,
)

M = 1_000_000
MINUS = "−"
ALL_SMA = {20: True, 50: True, 100: True}


def closes_from_log_returns(rets: list[float], start: float = 100.0) -> list[float]:
    closes = [start]
    for r in rets:
        closes.append(closes[-1] * math.exp(r))
    return closes


# --- Formato español ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("x", "decimals", "sign", "expected"),
    [
        (1234.5, 2, False, "1.234,50"),
        (1234567, 0, False, "1.234.567"),
        (999, 0, False, "999"),
        (1000, 0, False, "1.000"),
        (-1234.567, 1, False, f"{MINUS}1.234,6"),
        (999.995, 2, False, "1.000,00"),  # los medios, hacia fuera
        (2.5, 0, False, "3"),
        (-2.5, 0, False, f"{MINUS}3"),
        (Decimal("0.125"), 2, False, "0,13"),
        (0.0, 1, True, "0,0"),  # el cero no lleva signo
        (-0.04, 1, False, "0,0"),  # nunca «−0,0»
        (12.3, 1, True, "+12,3"),
        (-12.3, 1, True, f"{MINUS}12,3"),
        (1e20, 0, False, "100.000.000.000.000.000.000"),
    ],
)
def test_fmt_num_spanish(x, decimals, sign, expected):
    assert fmt_num(x, decimals, sign=sign) == expected


def test_fmt_num_missing_values():
    assert fmt_num(None) == "—"
    assert fmt_num(float("nan")) == "—"
    assert fmt_num(float("inf"), 2) == "—"
    assert fmt_pct(None) == "—"
    assert fmt_usd(None) == "—"
    assert fmt_musd(None) == "—"
    assert fmt_points(None) == "—"
    assert fmt_pp(None) == "—"


def test_fmt_pct_fractions():
    assert fmt_pct(0.123) == "+12,3 %"
    assert fmt_pct(-0.014) == f"{MINUS}1,4 %"
    assert fmt_pct(0.06, 0) == "+6 %"
    assert fmt_pct(0.5, sign=False) == "50,0 %"
    assert fmt_pct(0) == "0,0 %"
    assert fmt_pct(12.345, 1) == "+1.234,5 %"
    assert fmt_pct(Decimal("0.0005")) == "+0,1 %"


def test_fmt_usd_and_millions():
    assert fmt_usd(1234.5) == "1.234,50 $"
    assert fmt_usd(68_400, 0) == "68.400 $"
    assert fmt_usd(1.9, sign=True) == "+1,90 $"
    assert fmt_usd(-41.4) == f"{MINUS}41,40 $"
    assert fmt_usd(Decimal("108.3")) == "108,30 $"
    assert fmt_musd(2_100 * M) == "2.100 M$"
    assert fmt_musd(18_300 * M) == "18.300 M$"
    assert fmt_musd(1.2 * M) == "1,2 M$"
    assert fmt_musd(20 * M) == "20 M$"
    assert fmt_musd(Decimal(5_000_000)) == "5,0 M$"


def test_fmt_points_trend_vol_and_vs_btc():
    assert fmt_points(93.6) == "94"
    assert fmt_points(94.5) == "95"
    assert fmt_points(0) == "0"
    assert fmt_trend(1.0) == "3 de 3"
    assert fmt_trend(2 / 3) == "2 de 3"
    assert fmt_trend(0.0) == "0 de 3"
    assert fmt_vol(0.039) == "±3,9 %"
    assert fmt_pp(0.09) == "+9 puntos"
    assert fmt_pp(-0.012) == f"{MINUS}1 punto"
    assert fmt_pp(0.001) == "0 puntos"


# --- Fechas en hora de Madrid ---------------------------------------------------------------


def test_madrid_time_summer_and_winter():
    summer = madrid_time(datetime(2026, 7, 15, 0, 0, tzinfo=UTC))
    winter = madrid_time(datetime(2026, 1, 15, 0, 0, tzinfo=UTC))
    assert (summer.hour, summer.utcoffset().total_seconds()) == (2, 7200)
    assert (winter.hour, winter.utcoffset().total_seconds()) == (1, 3600)
    # Un datetime sin zona se toma como UTC.
    assert madrid_time(datetime(2026, 7, 15, 0, 0)) == summer
    assert fmt_clock(datetime(2026, 7, 15, 0, 10, tzinfo=UTC)) == "02:10"
    assert fmt_clock(datetime(2026, 1, 15, 0, 10, tzinfo=UTC)) == "01:10"


def test_madrid_time_around_dst_changes():
    # 2026: cambio de hora el 29 de marzo y el 25 de octubre (a la 01:00 UTC).
    assert fmt_clock(datetime(2026, 3, 29, 0, 0, tzinfo=UTC)) == "01:00"
    assert fmt_clock(datetime(2026, 3, 30, 0, 0, tzinfo=UTC)) == "02:00"
    assert fmt_clock(datetime(2026, 10, 25, 0, 0, tzinfo=UTC)) == "02:00"
    assert fmt_clock(datetime(2026, 10, 26, 0, 0, tzinfo=UTC)) == "01:00"


def test_spanish_dates():
    assert fmt_date_long(date(2026, 10, 21)) == "miércoles 21 de octubre de 2026"
    assert fmt_date_long(date(2026, 1, 4)) == "domingo 4 de enero de 2026"
    # Las 23:30 UTC del 20 de octubre ya son el día 21 en Madrid.
    late = datetime(2026, 10, 20, 23, 30, tzinfo=UTC)
    assert fmt_date_long(late) == "miércoles 21 de octubre de 2026"
    assert fmt_date_short(date(2026, 10, 14)) == "14-oct"
    assert fmt_date_short(date(2026, 10, 23), year=True) == "23-oct-2026"
    assert fmt_weekday_day(date(2026, 10, 20)) == "martes 20"


def test_cutoff_text_summer_and_winter():
    assert cutoff_text(date(2026, 10, 20)) == (
        "Datos de Binance hasta el cierre del martes 20 (02:00, hora de Madrid)"
    )
    assert cutoff_text(date(2026, 12, 1)) == (
        "Datos de Binance hasta el cierre del martes 1 (01:00, hora de Madrid)"
    )


# --- Métricas informativas ------------------------------------------------------------------


def test_ret():
    closes = [100.0, 110.0, 121.0]
    assert ret(closes, 1) == pytest.approx(0.1)
    assert ret(closes, 2) == pytest.approx(0.21)
    assert ret(closes, 3) is None  # no hay C(t−3)
    assert ret(closes, 0) is None
    assert ret([0.0, 1.0], 1) is None


def test_best_day_share_spread_rise():
    closes = closes_from_log_returns([0.01] * 29 + [0.2])
    assert best_day_share(closes) == pytest.approx(0.2 / 0.49)
    assert single_day_warning(best_day_share(closes)) is None


def test_best_day_share_warning():
    closes = closes_from_log_returns([0.0] * 10 + [0.3] + [0.0] * 19)
    share = best_day_share(closes)
    assert share == pytest.approx(1.0)
    idx, day_ret = best_day(closes)
    assert idx == 11
    assert day_ret == pytest.approx(math.exp(0.3) - 1)
    assert single_day_warning(share, date(2026, 10, 14), 0.21) == (
        "Aviso: más de la mitad de la subida vino de un solo día (14-oct, +21 %)."
    )
    assert single_day_warning(share) == "Aviso: más de la mitad de la subida vino de un solo día."
    # Puede pasar de 1 si el resto de días restaron.
    falls = closes_from_log_returns([-0.01] * 29 + [0.5])
    assert best_day_share(falls) == pytest.approx(0.5 / 0.21)
    # Solo por encima de 0,6.
    assert single_day_warning(0.6) is None
    assert single_day_warning(0.61) is not None


def test_best_day_share_without_rise_or_history():
    assert best_day_share(closes_from_log_returns([-0.01] * 30)) is None
    assert best_day_share(closes_from_log_returns([0.0] * 30)) is None
    assert best_day_share(closes_from_log_returns([0.01] * 29)) is None  # 30 velas
    assert best_day(closes_from_log_returns([0.01] * 29)) is None


def test_p5_week_short_history():
    closes = [100.0] * 364
    assert p5_week(closes) is None
    text = bad_week_text(p5_week(closes))
    assert "tiene menos de un año en Binance" in text


def test_p5_week_percentile():
    rng = random.Random(3)  # noqa: S311
    closes = closes_from_log_returns([rng.gauss(0, 0.04) for _ in range(399)])
    window = closes[-365:]
    weekly = sorted(window[k] / window[k - 7] - 1 for k in range(7, 365))
    pos = 0.05 * (len(weekly) - 1)  # interpolación lineal
    lo = math.floor(pos)
    expected = weekly[lo] + (weekly[lo + 1] - weekly[lo]) * (pos - lo)
    assert p5_week(closes) == pytest.approx(expected)
    assert p5_week(closes) < 0


def test_p5_week_constant_growth_and_texts():
    closes = closes_from_log_returns([0.01] * 400)
    assert p5_week(closes) == pytest.approx(math.exp(0.07) - 1)
    assert bad_week_text(-0.16) == "En el último año, 1 de cada 20 semanas cayó más de un 16 %."
    assert bad_week_text(-0.042) == ("En el último año, 1 de cada 20 semanas cayó más de un 4,2 %.")
    assert "19 de cada 20 semanas" in bad_week_text(0.01)


def test_momentum_percentile_with_ties():
    population = [0.1, 0.2, 0.2, 0.3]
    assert momentum_percentile(0.2, population) == pytest.approx(0.5)
    assert momentum_percentile(0.3, population) == pytest.approx(1.0)
    assert momentum_percentile(0.1, population) == pytest.approx(0.0)
    assert momentum_percentile(0.1, [0.1]) is None


def test_breadth():
    assert breadth([True, True, False, True]) == pytest.approx(0.75)
    assert breadth([]) is None
    assert breadth_text(0.62).startswith("Amplitud: el 62 % de las monedas")


# --- Nivel de riesgo ------------------------------------------------------------------------

SAFE = {"vol_daily": 0.02, "episode_days": 900, "median_qv": 500 * M, "best_day_share": 0.2}


@pytest.mark.parametrize(
    "change",
    [
        {"vol_daily": 0.07},
        {"episode_days": 179},
        {"median_qv": 19.9 * M},
        {"best_day_share": 0.61},
        {"spread_pct": 0.25},
    ],
)
def test_risk_high_by_each_criterion(change):
    assert risk_level(**{**SAFE, **change}) == "Alto"
    assert len(risk_reasons(**{**SAFE, **change})) == 1


def test_risk_low_and_medium():
    assert risk_level(**SAFE) == "Bajo"
    assert risk_level(**SAFE, spread_pct=0.05) == "Bajo"
    assert risk_level(**{**SAFE, "best_day_share": None}) == "Bajo"
    assert risk_level(**{**SAFE, "vol_daily": 0.045}) == "Medio"
    # Límites: σ = 6 % no es Alto; σ = 3 % ya no es Bajo; 180 días y 20 M$ no son Alto.
    assert risk_level(**{**SAFE, "vol_daily": 0.06}) == "Medio"
    assert risk_level(**{**SAFE, "vol_daily": 0.03}) == "Medio"
    assert risk_level(**{**SAFE, "episode_days": 180, "median_qv": 20 * M}) == "Bajo"
    assert risk_level(**SAFE, spread_pct=Decimal("0.2")) == "Bajo"
    assert risk_reasons(**SAFE) == []


# --- Motivos de venta -----------------------------------------------------------------------


def test_sell_reasons_of_the_rule_match_section_6_1():
    assert sell_reason("regimen") == (
        "mercado bajista: BTC cerró por debajo de su media de 100 días"
    )
    assert (
        sell_reason("fuera_universo", universe_reason="poco volumen: mediana de 1,2 M $ al día")
        == "fuera del universo: poco volumen: mediana de 1,2 M $ al día"
    )
    assert sell_reason("fuera_universo") == (
        "fuera del universo: sin datos suficientes para puntuarla"
    )
    assert sell_reason("stop") == f"stop de emergencia: {MINUS}25 % desde la compra"
    assert sell_reason("tendencia") == "perdió la tendencia (por debajo de 2 de sus 3 medias)"
    assert sell_reason("puesto", rank=15) == "bajó al puesto 15 (se vende a partir del 13)"


def test_other_sell_reasons():
    assert sell_reason("retirada") == (
        f"retirada de Binance: 7 días seguidos sin cotizar; se cierra al último precio {MINUS}10 %"
    )
    assert sell_reason("retirada", renamed_to="POLUSDT") == (
        "cambio de nombre a POLUSDT: se cierra al último precio, sin penalización"
    )
    assert sell_reason("retirada", status="BREAK") == (
        "dejó de cotizar en Binance (estado BREAK): se vende en la siguiente ejecución"
    )
    assert sell_reason("fin_prueba", test_days=28) == (
        "fin de la prueba de 28 días: se vende al precio de venta del momento, menos la comisión"
    )
    assert sell_reason("fin_prueba").startswith("fin de la prueba: ")
    assert sell_reason("horizonte_7d") == "libro de señales: se cierra a los 7 días de la entrada"


def test_every_sell_purpose_has_a_template():
    for purpose in SELL_PURPOSES:
        assert sell_reason(purpose, rank=14)
    with pytest.raises(ValueError, match="desconocido"):
        sell_reason("entrada")
    with pytest.raises(ValueError, match="puesto"):
        sell_reason("puesto")


def test_buy_reason_and_coin_status():
    assert buy_reason(1, 94.2, 1.0) == "puesto 1 con 94 puntos y tendencia 3 de 3"
    assert coin_status(rank=23, n=84, score=61.2, trend_share=2 / 3) == (
        "puesto 23 de 84; puntos 61; tendencia 2 de 3"
    )
    assert coin_status(exclusion_reason="poco volumen, mediana de 1,2 M $ al día") == (
        "Fuera del universo: poco volumen, mediana de 1,2 M $ al día"
    )


# --- POR QUÉ --------------------------------------------------------------------------------


def test_why_text_reproduces_the_mockup():
    text = why_text(
        "SOL",
        ret_30d=0.18,
        momentum_pct=0.97,
        above_sma=ALL_SMA,
        median_qv=2_100 * M,
        order_usd=18,
        best_share=0.15,
        p5=-0.16,
    )
    assert text == (
        "SOL: sube un 18 % en 30 días, más que 97 de cada 100 monedas analizadas, y está por "
        "encima de sus medias de 20, 50 y 100 días. La subida está repartida: su mejor día "
        "aportó el 15 %. Se negocian 2.100 M$ al día: comprar o vender 18 $ no mueve el "
        "precio. En el último año, 1 de cada 20 semanas cayó más de un 16 %."
    )


def test_why_text_single_day_and_short_history():
    text = why_text(
        "SUI",
        ret_30d=0.25,
        momentum_pct=0.9,
        above_sma={20: True, 50: True, 100: False},
        median_qv=300 * M,
        order_usd=18.5,
        best_share=0.8,
        p5=None,
        best_day_info=(date(2026, 10, 14), 0.21),
    )
    assert "más que 90 de cada 100" in text
    assert "por encima de 2 de sus 3 medias (las de 20 y 50 días)" in text
    assert "La subida está repartida" not in text
    assert "comprar o vender 18,50 $ no mueve el precio" in text
    assert "tiene menos de un año en Binance" in text
    assert text.endswith("Aviso: más de la mitad de la subida vino de un solo día (14-oct, +21 %).")


def test_why_text_falling_coin_and_extremes():
    text = why_text(
        "ADA",
        ret_30d=-0.05,
        momentum_pct=0.4,
        above_sma={20: False, 50: False, 100: False},
        median_qv=0.1 * M,
        order_usd=18,
    )
    assert text.startswith(
        "ADA: baja un 5,0 % en 30 días, mejor que 40 de cada 100 monedas analizadas, y está "
        "por debajo de sus medias de 20, 50 y 100 días."
    )
    assert "puede mover el precio" in text
    top = why_text("BTC", ret_30d=0.3, momentum_pct=1.0, above_sma=ALL_SMA, median_qv=M * 1e4)
    assert "más que todas las demás monedas analizadas" in top
    near_top = why_text(
        "ETH", ret_30d=0.3, momentum_pct=0.998, above_sma=ALL_SMA, median_qv=M * 1e4
    )
    assert "más que 99 de cada 100" in near_top
    assert sma_text({20: False, 50: False, 100: True}) == (
        "está por encima de 1 de sus 3 medias (la de 100 días)"
    )


# --- Confianza en la regla ------------------------------------------------------------------


class Color(enum.Enum):
    VERDE = "verde"
    AMBAR = "ambar"


@pytest.mark.parametrize(
    ("exists", "excess", "bt", "live", "expected"),
    [
        (False, None, "gris", "gris", "SIN DATOS"),
        (False, 0.05, "verde", "verde", "SIN DATOS"),
        (True, None, "gris", "gris", "SIN DATOS"),
        (True, 0.0, "ambar", "gris", "MUY BAJA"),
        (True, -0.03, "rojo", "gris", "MUY BAJA"),
        (True, 0.04, "ambar", "gris", "BAJA"),
        (True, 0.04, "gris", "verde", "BAJA"),
        (True, 0.04, "verde", "ambar", "MEDIA"),
        (True, 0.04, "VERDE", "gris", "MEDIA"),
        (True, 0.04, "verde", "verde", "ALTA"),
        (True, 0.04, Color.VERDE, Color.VERDE, "ALTA"),
        (True, 0.04, Color.VERDE, Color.AMBAR, "MEDIA"),
    ],
)
def test_confidence_table(exists, excess, bt, live, expected):
    assert confidence(exists, excess, bt, live) == expected


def test_confidence_text():
    assert confidence_text("BAJA") == (
        "Confianza en la regla: BAJA. En el pasado ganó a BTC de media, pero el margen de "
        "error incluye perder."
    )
    assert "sin garantía" in confidence_text("ALTA")


# --- Escenario de desplome ------------------------------------------------------------------


def test_crash_scenario():
    positions = {"BTCUSDT": 20.0, "ETHUSDT": 10.0, "SOLUSDT": 30.0, "XRP": 40.0}
    loss, share = crash_scenario(positions)
    assert loss == pytest.approx(0.3 * 20 + 0.3 * 10 + 0.5 * 30 + 0.5 * 40)
    assert share == pytest.approx(44 / 100)
    loss, share = crash_scenario(positions, cash=10.0)
    assert share == pytest.approx(44 / 110)
    assert crash_scenario({}) == (0.0, 0.0)
    assert crash_scenario({"BTC": Decimal(100)}) == (pytest.approx(30.0), pytest.approx(0.3))
    assert crash_text(positions, cash=10.0) == (
        "Si mañana BTC y ETH cayeran un 30 % y el resto un 50 %, perderías unos 44,00 $ (40 %)."
    )
    assert crash_text({}).startswith("Sin posiciones abiertas")


# --- Reglas de redacción --------------------------------------------------------------------


def _rendered_templates() -> list[str]:
    texts = [sell_reason(p, rank=14, test_days=28) for p in SELL_PURPOSES]
    texts += [sell_reason("retirada", renamed_to="POL"), sell_reason("retirada", status="BREAK")]
    texts += [confidence_text(level) for level in CONFIDENCE_LEVELS]
    texts += [bad_week_text(p) for p in (None, -0.16, 0.02)]
    texts += [crash_text({"BTCUSDT": 18.0, "SOLUSDT": 18.0}, 64.0), crash_text({})]
    texts += [cutoff_text(date(2026, 10, 20)), breadth_text(0.5), breadth_text(None)]
    texts += [buy_reason(3, 80, 2 / 3), coin_status(rank=2, n=84, score=91, trend_share=1)]
    texts += risk_reasons(0.07, 10, M, 0.9, 1.0)
    texts.append(single_day_warning(0.9, date(2026, 10, 14), 0.21))
    for share, p5, pct in [(0.15, -0.16, 0.97), (0.9, None, 1.0), (None, 0.01, 0.0)]:
        for ret_30d in (0.18, -0.07, None):
            texts.append(
                why_text(
                    "SOL",
                    ret_30d=ret_30d,
                    momentum_pct=pct,
                    above_sma={20: True, 50: False, 100: True},
                    median_qv=2_100 * M,
                    best_share=share,
                    p5=p5,
                    best_day_info=(date(2026, 10, 14), 0.21),
                )
            )
    return texts


def _module_string_literals() -> list[str]:
    tree = ast.parse(Path(explain.__file__).read_text(encoding="utf-8"))
    skip = set(FORBIDDEN) | set(ALLOWED_DISCLAIMERS)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in skip
    ]


def test_templates_never_promise():
    texts = _rendered_templates() + _module_string_literals()
    assert len(texts) > 50
    for text in texts:
        folded = text.casefold()
        for word in FORBIDDEN:
            assert word not in folded, f"«{word}» en {text!r}"
        assert check_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "Esta moneda va a subir mañana.",
        "Beneficio GARANTIZADO.",
        "Te aseguramos que gana.",
        "Una inversión SIN RIESGO.",
        "Es seguro que sube.",
        "La más rentable del día.",
        "Con beneficio asegurado.",
        "Va A Subir.",
    ],
)
def test_check_text_rejects_promises(text):
    with pytest.raises(ValueError, match="prohibida"):
        check_text(text)


def test_check_text_allows_only_the_fixed_disclaimers():
    for disclaimer in ("Lo pasado no garantiza lo futuro.", explain.ALLOWED_DISCLAIMERS[1]):
        assert check_text(disclaimer) == disclaimer
    with pytest.raises(ValueError):
        check_text("Lo pasado no garantiza lo futuro, pero esto garantiza ganar.")
    with pytest.raises(ValueError):
        check_text("Lo pasado no garantiza lo futuro.", allowed=())

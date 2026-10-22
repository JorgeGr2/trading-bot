"""Informe diario: maqueta, mercado desfavorable, cabecera en rojo y sin promesas."""

from __future__ import annotations

from datetime import UTC, datetime

from copybot.crypto.analysis import analyze
from copybot.crypto.explain import check_text
from copybot.crypto.report import BacktestInfo, PaperInfo, coin_text, render, to_json
from tests.test_crypto_engine import make_panel

NOW = datetime(2026, 10, 21, 0, 14, tzinfo=UTC)


def _analysis(drift_btc: float):
    panel = make_panel(n=24, days=260, seed=3, drift=lambda k: (k - 12) * 0.001)
    btc = panel.series["BTCUSDT"]
    # Fuerza el régimen: BTC muy por encima o muy por debajo de su media de 100 días.
    for i in range(len(btc.close) - 30, len(btc.close)):
        f = (1 + drift_btc) ** (i - (len(btc.close) - 30))
        btc.close[i] *= f
        btc.open[i] *= f
        btc.high[i] *= f * 1.01
        btc.low[i] *= f * 0.99
    return analyze(panel, panel.days[-1])


def test_favorable_report_names_the_best_scored_coin():
    a = _analysis(0.02)
    paper = PaperInfo(
        holdings=["C24", "C23"],
        sells=[("C05", "bajó al puesto 15 (se vende a partir del 13)", 1.9, 0.106)],
        buys=[("C23", 18.0)],
        equity=108.3,
        btc_equity=104.1,
        cash=12.4,
        day_loss=0.012,
        drawdown=0.04,
        positions={"C24USDT": 20.0, "C23USDT": 18.0},
    )
    text = render(a, generated_at=NOW, paper=paper, confidence="BAJA")
    check_text(text)
    assert "Mercado: FAVORABLE" in text and "La mejor puntuada hoy según la regla v1" in text
    assert "LAS 10 MEJOR PUNTUADAS" in text and "POR QUÉ" in text
    assert "Vende C05" in text and "Compra C23 por 18,00 $" in text
    assert "Esto no es una recomendación de inversión" in text
    data = to_json(a)
    assert data["mercado"]["favorable"] and len(data["mejores"]) == 10


def test_bear_market_says_stay_in_usdt_and_red_header():
    a = _analysis(-0.03)
    text = render(a, generated_at=NOW, backtest=BacktestInfo(excess_annual=-0.05, ci=(-0.2, 0.1)))
    assert "Hoy ninguna: quédate en USDT" in text
    assert "LAS MÁS FUERTES (solo informativo: hoy la regla no compra)" in text
    assert "En el pasado, esta regla lo hizo peor que comprar BTC y esperar" in text


def test_coin_explains_why_it_is_in_or_out():
    a = _analysis(0.02)
    ranked = a.ranked[0]
    assert "puesto 1" in coin_text(a, ranked.symbol)
    assert "no cotizaba" in coin_text(a, "NOPEUSDT")

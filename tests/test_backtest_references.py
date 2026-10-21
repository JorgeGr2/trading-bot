"""Estrategias de referencia con precios inventados (cifras comprobables a mano)."""

from __future__ import annotations

from decimal import Decimal

from copybot.backtest.references import RefParams, buy_and_hold, dca, trend

D = Decimal
H = 3600
T = 1_791_000_000
FREE = RefParams(cost_pct=D(0), tx_fee_usd=D(0), trend_hours=3, trend_min_samples=3)


def series(*prices: str, start: int = T) -> list[tuple[int, D]]:
    return [(start + i * H, D(p)) for i, p in enumerate(prices)]


def test_buy_and_hold_with_costs():
    s = series("100", "50", "200")
    free = buy_and_hold("X", s, T, T + 2 * H, FREE)
    assert free.final_usd == 200 and free.max_drawdown_pct == 50
    costly = buy_and_hold("X", s, T, T + 2 * H, RefParams(cost_pct=D(1), tx_fee_usd=D(1)))
    # 99 $ × 0,99 = 0,9801 unidades; al vender: 196,02 × 0,99 − 1
    assert costly.final_usd == D("0.9801") * 200 * D("0.99") - 1


def test_dca_buys_equal_amounts_weekly():
    week = 7 * 86400
    s = [(T, D(100)), (T + week, D(50)), (T + 2 * week, D(100))]
    result = dca("X", s, T, T + 2 * week, FREE)
    # dos compras de 50 $ (la del final del periodo no se hace): 0,5 + 1 unidades → 150 $
    assert result.final_usd == 150 and result.trades == 3


def test_trend_average_needs_enough_hours():
    # Faltan horas antes de empezar: no hay media y no opera.
    s = [(T - 10 * H, D(10)), (T, D(12)), (T + H, D(15))]
    result = trend("X", s, T, T + H, FREE)
    assert result.trades == 0 and result.final_usd == 100


def test_trend_decides_on_one_hour_and_trades_on_the_next():
    # Horas previas (para la media) y luego una subida y una caída.
    s = series("10", "10", "10", "12", "15", "9", "8", "8", start=T - 3 * H)
    result = trend("X", s, T, T + 4 * H, FREE)
    # T (12) > media 10,67 → compra a 15 en T+1; T+2 (9) < media 12 → vende a 8 en T+3.
    assert result.trades == 2
    assert result.final_usd == D(100) / 15 * 8
    assert next(t for t, _ in result.curve) == T  # las horas previas no cuentan


def test_trend_sells_what_it_holds_at_the_end():
    s = series("10", "10", "10", "11", "12", "13", start=T - 3 * H)
    result = trend("X", s, T, T + 2 * H, FREE)
    assert result.trades == 2 and result.final_usd == D(100) / 12 * 13


def test_report_ranks_the_references_and_flags_missing_prices():
    from copybot.backtest.report import Coverage, _references_section

    s = series("100", "50", "200")
    up = buy_and_hold("BTC", s, T, T + 2 * H, FREE)
    down = buy_and_hold("SPYx", series("100", "90"), T, T + H, FREE)
    lines = _references_section([], [(down, down), (up, up)], RefParams(), [Coverage("ETH", 0, 10)])
    rows = [line for line in lines if line.startswith("| comprar")]
    assert "BTC" in rows[0] and "SPYx" in rows[1]  # de más a menos
    assert any("ETH: precio en 0 de 10 horas (demasiados huecos" in line for line in lines)

"""Costes y ejecución (apartado 6.5 del diseño): tramos, compraventa exacta, `stepSize`,
`minNotional`, libro fino y calibración."""

from __future__ import annotations

from decimal import Decimal

import pytest

from copybot.crypto.costs import (
    COST_TIERS,
    FEE,
    buy_qty,
    calibrate,
    cost_per_side,
    cost_table_hash,
    live_buy,
    live_sell,
    min_notional_ok,
    round_qty,
    round_trip_cost,
    sell_usd,
)

D = Decimal
M = 1_000_000


# --- Tramos ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("median_qv", "expected"),
    [
        (5_000 * M, 0.0002),
        (200 * M, 0.0002),  # el límite pertenece al tramo de arriba
        (200 * M - 1, 0.0004),
        (50 * M, 0.0004),
        (49.9 * M, 0.0008),
        (10 * M, 0.0008),
        (9.9 * M, 0.0015),
        (5 * M, 0.0015),
        (1 * M, 0.0015),  # por debajo de 5 M$: el tramo de 5-10 M$
        (0.0, 0.0015),
    ],
)
def test_cost_tiers(median_qv, expected):
    assert cost_per_side(median_qv) == expected


def test_round_trip_matches_the_table_and_stress_multiplies():
    assert [round(round_trip_cost(v), 10) for v, _ in COST_TIERS] == [
        0.0024,
        0.0028,
        0.0036,
        0.0050,
    ]
    assert cost_per_side(300 * M, 2) == pytest.approx(0.0004)
    assert cost_per_side(7 * M, 3) == pytest.approx(0.0045)
    assert round_trip_cost(300 * M, 2) == pytest.approx(2 * round_trip_cost(300 * M))
    with pytest.raises(ValueError):
        cost_per_side(300 * M, -1)


def test_cost_table_hash_is_frozen():
    """Cambiar la tabla exige cambiar este test (y la huella del preregistro)."""
    expected = "b18ea8e3402626614d9279b988588fa5dbf41a6f0d77e2e85aad5910588ba4b4"
    assert cost_table_hash() == expected
    assert cost_table_hash(list(COST_TIERS), FEE) == expected
    raised = ((200_000_000.0, 0.0003), *COST_TIERS[1:])
    assert cost_table_hash(raised) != expected
    assert cost_table_hash(fee=0.00075) != expected


# --- Backtest -------------------------------------------------------------------------------


def test_backtest_round_trip_is_exact():
    qty = buy_qty(18.0, 2.0, 300 * M)
    assert qty == pytest.approx(18.0 / (2.0 * 1.0002 * 1.001), rel=1e-15)
    cash = sell_usd(qty, 2.0, 300 * M)
    assert cash == pytest.approx(18.0 * (0.9998 * 0.999) / (1.0002 * 1.001), rel=1e-15)
    # Sin cambio de precio se pierde ≈ 2·(f + c) = 0,24 %.
    assert 1 - cash / 18.0 == pytest.approx(0.0024, abs=1e-5)
    # Con subida: la misma cuenta escalada por el precio de salida.
    up = sell_usd(buy_qty(18.0, 2.0, 7 * M), 3.0, 7 * M)
    assert up == pytest.approx(18.0 * 1.5 * (0.9985 * 0.999) / (1.0015 * 1.001), rel=1e-15)


def test_stress_multiplies_spread_and_fee():
    qty = buy_qty(100.0, 10.0, 300 * M, mult=2)
    assert qty == pytest.approx(100.0 / (10.0 * 1.0004 * 1.002), rel=1e-15)
    cash = sell_usd(qty, 10.0, 300 * M, mult=2)
    assert cash == pytest.approx(qty * 10.0 * 0.9996 * 0.998, rel=1e-15)
    loss_1 = 1 - sell_usd(buy_qty(100.0, 10.0, 7 * M), 10.0, 7 * M) / 100
    loss_2 = 1 - sell_usd(buy_qty(100.0, 10.0, 7 * M, 2), 10.0, 7 * M, 2) / 100
    assert loss_2 == pytest.approx(2 * loss_1, rel=0.01)


def test_buy_needs_a_positive_open():
    with pytest.raises(ValueError):
        buy_qty(18.0, 0.0, 300 * M)


# --- En vivo: stepSize y minNotional --------------------------------------------------------


def test_round_qty_floors_to_step():
    assert round_qty(D("1.23456"), D("0.001")) == D("1.234")
    assert round_qty(D("1.23456"), D("0.00100000")) == D("1.234")
    assert round_qty(D("1.2349999"), D("0.001")) == D("1.234")  # nunca hacia arriba
    assert round_qty(D("0.00999"), D("0.01")) == 0
    assert round_qty(D("57"), D("10")) == 50
    assert round_qty(D("5"), D("1")) == 5
    assert round_qty(D("1.23456"), None) == D("1.23456")
    assert round_qty(D("1.23456"), D(0)) == D("1.23456")  # MARKET_LOT_SIZE con step 0


def test_min_notional_uses_at_least_five_dollars():
    assert min_notional_ok(D(2), D("2.5"), D(5))  # 5,00 $: justo en el límite
    assert not min_notional_ok(D("1.99"), D("2.5"), D(5))
    assert not min_notional_ok(D(2), D(2), None)  # sin filtro: 5 $ igualmente
    assert not min_notional_ok(D(2), D(2), D(1))  # filtro menor que 5 $: manda 5 $
    assert not min_notional_ok(D(4), D(2), D(10))  # filtro mayor: manda el filtro
    assert min_notional_ok(D(5), D(2), D(10))


def test_live_round_trip_is_exact():
    buy = live_buy(D(18), D(2), D(1000), D("0.01"), D(5))
    assert buy is not None
    assert buy.qty == D("8.99")  # 18 / (2·1,001) = 8,991… → 8,99
    assert buy.price == D(2)
    assert buy.gross_usd == D("17.98")
    assert buy.fee_usd == D("0.01798")
    assert buy.net_usd == D("17.99798") <= D(18)
    assert not buy.extra_slippage and buy.note == ""

    sell = live_sell(buy.qty, D("1.99"), D("0.01"))
    assert sell.qty == D("8.99") and sell.price == D("1.99")
    assert sell.gross_usd == D("17.8901")
    assert sell.fee_usd == D("0.0178901")
    assert sell.net_usd == D("17.8722099")
    assert sell.note == ""


def test_live_buy_rejected_below_min_notional():
    assert live_buy(D(4), D(2), D(1000), D("0.01"), D(1)) is None  # < 5 $
    assert live_buy(D(9), D(2), D(1000), D("0.01"), D(10)) is None  # < minNotional
    # El redondeo al stepSize puede dejarla por debajo: 6 / 2,002 → 2 unidades = 4 $.
    assert live_buy(D(6), D(2), D(1000), D(1), D(5)) is None
    assert live_buy(D(0), D(2), D(1000), D("0.01"), D(5)) is None
    with pytest.raises(ValueError):
        live_buy(D(18), D(0), D(1000), D("0.01"), D(5))


def test_thin_book_adds_half_percent_and_notes_it():
    fill = live_buy(D(18), D(2), D(5), D("0.01"), D(5))  # askQty·ask = 10 $ < 18 $
    assert fill is not None
    assert fill.extra_slippage
    assert fill.price == D("2.01")
    assert fill.qty == D("8.94")  # 18 / (2,01·1,001) = 8,946… → 8,94
    assert fill.gross_usd == D("17.9694")
    assert fill.fee_usd == D("0.0179694")
    assert fill.net_usd == D("17.9873694") <= D(18)
    assert "libro fino" in fill.note and "0,5 %" in fill.note
    assert "10,00 $" in fill.note and "18,00 $" in fill.note
    # Justo lo necesario (askQty·ask = importe) no es libro fino.
    exact = live_buy(D(18), D(2), D(9), D("0.01"), D(5))
    assert exact is not None and not exact.extra_slippage and exact.price == D(2)


def test_live_sell_rounds_down_and_notes_the_rest():
    sell = live_sell(D("8.995"), D(2), D("0.01"))
    assert sell.qty == D("8.99") and sell.gross_usd == D("17.98")
    assert "stepSize" in sell.note
    whole = live_sell(D("8.995"), D(2), None)
    assert whole.qty == D("8.995") and whole.note == ""
    with pytest.raises(ValueError):
        live_sell(D(1), D(0), D("0.01"))


# --- Calibración ----------------------------------------------------------------------------


def _quote(symbol, half_spread_bps, depth_usd, median_qv):
    """Foto con mid = 100 y un medio diferencial dado en puntos básicos."""
    half = D(half_spread_bps) / 100  # (ask − bid)/2 con mid = 100
    bid, ask = D(100) - half, D(100) + half
    return (symbol, bid, ask, D(str(depth_usd)) / ask, median_qv)


def test_calibrate_flags_tiers_above_the_table_and_thin_books():
    snaps = [
        # ≥ 200 M$: medio diferencial 0 → p75 + colchón = 0,02 %, igual a la tabla (no la supera).
        *[_quote(f"A{i}", 0, 10_000, 500 * M) for i in range(3)],
        # 50-200 M$: 1, 2, 3 y 4 pb → p75 = 3,25 pb; + 2 pb = 0,0525 % > 0,04 %.
        *[_quote(f"B{i}", bps, 10_000, 80 * M) for i, bps in enumerate((1, 2, 3, 4), 1)],
        # 10-50 M$: medio diferencial nulo, pero 2 de 4 fotos con askQty·ask < 25 $.
        _quote("C1", 0, 10, 20 * M),
        _quote("C2", 0, 24, 20 * M),
        _quote("C3", 0, 1_000, 20 * M),
        _quote("C4", 0, 1_000, 20 * M),
        # 5-10 M$: 1 de 4 fina (25 %, no es «habitual») y una con 25 $ justos (no es fina).
        _quote("D1", 0, 5, 7 * M),
        _quote("D2", 0, 25, 7 * M),
        _quote("D3", 0, 1_000, 7 * M),
        _quote("D4", 0, 1_000, 7 * M),
        # Descartadas: sin bid, cruzada y por debajo de 5 M$.
        ("X1", D(0), D(1), D(100), 300 * M),
        ("X2", D("1.01"), D(1), D(100), 300 * M),
        ("X3", 0.99, 1.01, 100.0, 1 * M),
    ]
    cal = calibrate(snaps)

    assert [t.samples for t in cal.tiers] == [3, 4, 4, 4]
    assert cal.invalid == 2 and cal.below_min == 1
    top, mid, low, last = cal.tiers
    assert top.measured_cost == pytest.approx(0.0002)
    assert not top.exceeds_table
    assert mid.measured_cost == pytest.approx(0.000525)
    assert mid.exceeds_table
    assert low.thin_share == 0.5 and low.thin_book_common
    assert last.thin_share == 0.25 and not last.thin_book_common
    assert [t.min_volume for t in cal.exceeded] == [50 * M]
    assert [t.min_volume for t in cal.thin_common] == [10 * M]
    assert not cal.table_ok

    warnings = cal.warnings()
    assert len(warnings) == 2
    assert "50-200 M$" in warnings[0] and "subir la tabla" in warnings[0]
    assert "0,0525 %" in warnings[0] and "0,04 %" in warnings[0]
    assert "10-50 M$" in warnings[1] and "siguiente nivel" in warnings[1]


def test_calibrate_without_snapshots_flags_nothing():
    cal = calibrate([])
    assert all(t.measured_cost is None and t.thin_share is None for t in cal.tiers)
    assert cal.table_ok and cal.warnings() == []


def test_calibrate_p75_interpolates_and_accepts_floats():
    snaps = [("S", 100 - h, 100 + h, 1_000.0, 300.0 * M) for h in (0.0, 0.01, 0.02, 0.03, 0.10)]
    cal = calibrate(snaps)
    # Medios diferenciales 0, 1, 2, 3 y 10 pb: p75 = 3 pb (posición 3 exacta).
    assert cal.tiers[0].measured_cost == pytest.approx(0.0003 + 0.0002)
    assert cal.tiers[0].exceeds_table

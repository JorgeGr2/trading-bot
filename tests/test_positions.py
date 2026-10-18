from __future__ import annotations

from decimal import Decimal

from copybot.analysis.positions import build_ledger
from tests.analysis_cases import AS_OF, SCENARIO, at, trade


def test_fifo_round_trips_by_hand():
    ledger = build_ledger(SCENARIO, AS_OF)
    trips = {t.mint: t for t in ledger.round_trips}

    a = trips["A"]
    assert a.closed_at == at(4, 10, 10)
    assert (a.cost, a.proceeds, a.pnl) == (Decimal(10), Decimal(9), Decimal(-1))
    assert a.roi == Decimal("-0.1")
    assert a.hold_seconds == 600
    assert a.seconds_to_first_sell == 300

    b = trips["B"]
    # FIFO: 50 del primer lote (coste 20) + 49,5 del segundo (coste 30 × 0,99 = 29,7).
    assert b.cost == Decimal("49.7")
    assert b.written_off == Decimal("0.3")
    assert b.pnl == Decimal("49.5")
    assert b.roi == Decimal("0.99")
    assert b.measurable

    c = trips["C"]
    assert c.closed and c.free_tokens and not c.measurable  # sin coste conocido

    assert not trips["E"].closed  # la venta posterior a as_of no existe para el análisis
    assert trips["E"].remaining_cost == Decimal(10)
    assert {t.mint for t in ledger.open_trips()} == {"E", "F", "G"}


def test_realizations_and_unknown_cost_sales():
    ledger = build_ledger(SCENARIO, AS_OF)
    assert [(r.cost, r.proceeds) for r in ledger.realizations] == [
        (Decimal(4), Decimal(6)),
        (Decimal(6), Decimal(3)),
        (Decimal("49.7"), Decimal("99.5")),
        (Decimal("0.3"), Decimal(0)),  # resto abandonado al cerrar B
    ]
    assert [v.usd for v in ledger.unmatched_sales] == [Decimal(8)]
    assert [v.usd for v in ledger.free_token_sales] == [Decimal(5)]
    assert ledger.counts["sell_without_position"] == 1


def test_open_cost_curve():
    ledger = build_ledger(SCENARIO, AS_OF)
    assert [v.usd for v in ledger.open_cost_curve] == [
        Decimal(x) for x in ("5", "9", "19", "15", "9", "29", "59", "9", "9", "9", "9", "19")
    ]


def test_transfer_out_moves_tokens_without_pnl():
    trades = [
        trade("a", 1, at(4), "buy", "X", "100", "10"),
        trade("b", 2, at(4, 1), "transfer_out", "X", "100"),
    ]
    ledger = build_ledger(trades, AS_OF)
    [trip] = ledger.round_trips
    assert trip.closed and trip.ended_by_transfer and not trip.measurable
    assert ledger.realizations == []
    assert ledger.open_cost_curve[-1].usd == 0


def test_sell_paid_in_sol_is_not_priced():
    trades = [
        trade("a", 1, at(4), "buy", "X", "100", "10"),
        trade("b", 2, at(4, 1), "sell", "X", "100", None),  # cobrada en SOL
    ]
    ledger = build_ledger(trades, AS_OF)
    [trip] = ledger.round_trips
    assert trip.closed and trip.unpriced_sold == Decimal(100)
    assert trip.priced_cost == 0 and not trip.measurable
    assert ledger.counts["non_usd_quote"] == 1


def test_same_slot_exit_is_detected():
    trades = [
        trade("a", 7, at(4), "buy", "X", "100", "10"),
        trade("b", 7, at(4), "sell", "X", "100", "11"),
    ]
    [trip] = build_ledger(trades, AS_OF).round_trips
    assert trip.same_slot_exit


def test_order_uses_slot_then_position_in_block():
    buy = trade("z-buy", 5, at(4), "buy", "X", "100", "10")
    sell = trade("a-sell", 5, at(4), "sell", "X", "100", "12")
    buy = buy.__class__(**{**buy.__dict__, "tx_index": 1})
    sell = sell.__class__(**{**sell.__dict__, "tx_index": 2})
    [trip] = build_ledger([sell, buy], AS_OF).round_trips
    assert trip.pnl == Decimal(2)

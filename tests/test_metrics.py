from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

import pytest

from copybot.analysis.metrics import AnalysisParams, Coverage, compute_window_metrics
from tests.analysis_cases import AS_OF, CREATED, SCENARIO, at, trade

PARAMS = AnalysisParams(_env_file=None)
FULL = Coverage(covered_from=at(1) - timedelta(days=10), covered_to=AS_OF)


def metrics(trades=SCENARIO, **kw):
    args = {
        "as_of": AS_OF,
        "window_days": 7,
        "params": PARAMS,
        "coverage": FULL,
        "token_created_at": CREATED,
    }
    args.update(kw)
    return compute_window_metrics(trades, **args)


def test_window_metrics_by_hand():
    m = metrics()
    assert m.coverage_complete
    assert m.events_total == 12  # T13 es posterior a as_of
    assert (m.buys, m.sells, m.transfers_in) == (5, 5, 1)
    assert m.tokens_traded == 6  # A, B, C, D, E, G (la compra de F es anterior)
    assert m.active_days == 6
    assert m.trades_per_day == Decimal(10) / 7
    assert m.buy_volume_usd == Decimal(74)
    assert m.sell_volume_usd == Decimal("121.5")
    # +2 − 3 + 49,8 − 0,3
    assert m.realized_pnl_usd == Decimal("48.5")
    assert m.realized_cost_usd == Decimal(60)
    # Operaciones medibles: A (−10 %) y B (+99 %). C no tiene coste conocido.
    assert (m.round_trips, m.wins, m.win_rate) == (2, 1, Decimal("0.5"))
    assert m.profit_factor == Decimal("49.5")
    assert m.mean_return == Decimal("0.445")
    assert m.median_return == Decimal("0.445")
    assert m.clipped_stdev_return == pytest.approx(Decimal("0.7707463914933368"))
    assert m.first_half_mean_return == Decimal("0.445")  # las dos cierran antes del día 6,5
    assert m.second_half_mean_return is None
    assert (m.round_trip_pnl_usd, m.pnl_without_best_usd) == (Decimal("48.5"), Decimal(-1))
    assert m.top1_profit_share == 1
    assert m.median_hold_seconds == 330  # (600 + 60) / 2
    assert m.median_seconds_to_first_sell == 180  # (300 + 60) / 2
    assert m.quick_exit_share == 0  # 60 s no es «menos de 60 s»
    # Curva de PnL: +2, −1 (caída de 3), +48,8, +48,5. Capital máximo abierto: 59 $.
    assert m.max_drawdown_usd == 3
    assert m.peak_open_cost_usd == 59
    assert m.max_drawdown_pct == Decimal(3) / 59
    assert (m.open_positions, m.open_cost_usd) == (3, Decimal(19))
    assert m.stale_open_cost_usd == 4  # G; F es anterior a la ventana; E es reciente
    assert m.conservative_pnl_usd == Decimal("44.5")
    assert m.unmatched_sell_usd == 8
    assert m.unmatched_sell_share == Decimal(8) / Decimal("121.5")
    assert m.free_tokens_sold_usd == 5
    # Sniping: A (30 s tras su creación) y B (4,5 días) comprobados; E y G sin fecha.
    assert (m.launch_checked, m.launch_unchecked, m.early_buys) == (2, 2, 1)
    assert m.early_buy_share == Decimal("0.5")
    assert m.median_token_age_seconds == Decimal(194415)  # (30 + 388800) / 2


def test_incomplete_coverage_is_reported():
    late = Coverage(covered_from=at(5), covered_to=AS_OF)
    stale = Coverage(covered_from=at(1), covered_to=AS_OF - timedelta(hours=3))
    assert not metrics(coverage=late).coverage_complete
    assert not metrics(coverage=stale).coverage_complete
    assert not metrics(coverage=None).coverage_complete


def test_json_is_serializable_and_exact():
    data = metrics().to_json()
    assert data["realized_pnl_usd"] == "48.5"
    assert data["as_of"] == "2026-10-10T00:00:00+00:00"
    assert data["second_half_mean_return"] is None


def _random_trades(seed: int):
    rng = random.Random(seed)  # noqa: S311 (datos de prueba)
    trades = []
    for i in range(80):
        moment = at(1) + timedelta(seconds=rng.randint(0, 14 * 86400))
        kind = rng.choice(["buy", "buy", "sell", "sell", "transfer_in", "transfer_out"])
        usd = str(rng.randint(1, 50)) if kind in ("buy", "sell") and rng.random() > 0.1 else None
        trades.append(
            trade(
                f"s{i}",
                int(moment.timestamp()),
                moment,
                kind,
                rng.choice("XYZ"),
                str(rng.randint(1, 1000)),
                usd,
            )
        )
    return trades


@pytest.mark.parametrize("seed", range(20))
def test_no_information_after_as_of_is_used(seed):
    """Añadir, quitar o cambiar eventos posteriores a as_of no cambia nada."""
    trades = _random_trades(seed)
    past = [t for t in trades if t.time <= AS_OF]
    future = [
        trade(f"f{i}", 10**9 + i, AS_OF + timedelta(seconds=i + 1), "sell", "X", "5", "999")
        for i in range(5)
    ]
    reference = metrics(past).to_json()
    assert metrics(trades).to_json() == reference
    assert metrics(trades + future).to_json() == reference
    assert metrics(list(reversed(trades))).to_json() == reference  # el orden de entrada da igual

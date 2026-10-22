"""Fidelidad: comparar las decisiones en vivo con la réplica del backtest."""

from __future__ import annotations

from copybot.config import RiskLimits
from copybot.crypto.engine import build_views
from copybot.crypto.fidelity import compare, replay_decisions
from copybot.crypto.strategy import STRATEGY_V1
from tests.test_crypto_engine import make_panel


def test_identical_decisions_pass_and_a_difference_is_red_until_explained():
    panel = make_panel(drift=lambda k: 0.01 if k == 1 else 0.0)
    days = panel.days[130:200]
    views = build_views(panel, days, STRATEGY_V1)
    replay = replay_decisions(panel, views, days, RiskLimits.model_construct())
    assert any(replay.values())
    live = {d: set(v) for d, v in replay.items()}
    assert compare(live, replay, days).ok
    day = next(d for d in days if replay.get(d))
    live[day] = set()  # en vivo no hizo lo que tocaba
    report = compare(live, replay, days)
    assert not report.ok and report.unexplained == [day]
    assert compare(live, replay, days, explained={day}).ok

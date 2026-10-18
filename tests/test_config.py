from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from copybot.config import (
    MAX_VIRTUAL_CAPITAL_USD,
    RiskLimits,
    Settings,
)


def test_defaults_are_the_agreed_limits():
    limits = RiskLimits()
    assert limits.virtual_capital_usd == Decimal("100")
    assert limits.max_position_usd == Decimal("10")
    assert limits.max_open_positions == 5
    assert limits.min_cash_reserve_pct == Decimal("20")
    assert limits.max_daily_loss_pct == Decimal("10")
    assert limits.max_drawdown_pct == Decimal("25")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("virtual_capital_usd", "100.01"),
        ("virtual_capital_usd", "0"),
        ("max_position_usd", "10.5"),
        ("max_open_positions", 6),
        ("min_cash_reserve_pct", "19.99"),
        ("max_daily_loss_pct", "11"),
        ("max_drawdown_pct", "30"),
    ],
)
def test_limits_cannot_be_relaxed(field, value):
    with pytest.raises(ValidationError):
        RiskLimits(**{field: value})


def test_limits_can_be_tightened():
    limits = RiskLimits(
        virtual_capital_usd="50",
        max_position_usd="5",
        max_open_positions=3,
        min_cash_reserve_pct="40",
        max_daily_loss_pct="5",
        max_drawdown_pct="15",
    )
    assert limits.max_position_usd == Decimal("5")


def test_limits_from_environment_are_validated(monkeypatch):
    monkeypatch.setenv("COPYBOT_RISK_VIRTUAL_CAPITAL_USD", "1000")
    with pytest.raises(ValidationError):
        RiskLimits()


def test_position_cannot_exceed_capital():
    with pytest.raises(ValidationError):
        RiskLimits(virtual_capital_usd="5", max_position_usd="10")


def test_only_simulation_mode_exists(monkeypatch):
    monkeypatch.setenv("COPYBOT_MODE", "live")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_budget_cap(monkeypatch):
    monkeypatch.setenv("COPYBOT_MONTHLY_BUDGET_USD", "60")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_secrets_are_not_printed():
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://u:supersecreto@h/db")
    assert "supersecreto" not in repr(settings)
    assert "supersecreto" not in str(settings.model_dump())


def test_invalid_relayer_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, fomo_relayer="no-es-una-direccion")


def test_capital_cap_constant():
    assert MAX_VIRTUAL_CAPITAL_USD == Decimal("100")

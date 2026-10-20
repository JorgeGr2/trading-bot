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


def test_helius_key_builds_endpoints_and_stays_hidden(monkeypatch):
    monkeypatch.setenv("COPYBOT_HELIUS_API_KEY", "clave-de-prueba-123")
    settings = Settings(_env_file=None)
    http, ws = settings.rpc_endpoints()
    assert http == "https://mainnet.helius-rpc.com/?api-key=clave-de-prueba-123"
    assert ws.startswith("wss://mainnet.helius-rpc.com/")
    assert "clave-de-prueba-123" not in repr(settings)


def test_public_rpc_is_the_default(monkeypatch):
    # Aislado del entorno: en el servidor (y en el entorno de desarrollo) puede haber
    # una clave de Helius definida.
    monkeypatch.delenv("COPYBOT_HELIUS_API_KEY", raising=False)
    http, ws = Settings(_env_file=None).rpc_endpoints()
    assert http == "https://api.mainnet-beta.solana.com"
    assert ws == "wss://api.mainnet-beta.solana.com"


def test_group_settings_are_read_from_dotenv(tmp_path, monkeypatch):
    from copybot.sim.params import EngineParams, SafetyParams

    for name in ("COPYBOT_RISK_MAX_OPEN_POSITIONS", "COPYBOT_SIM_LATENCY_MS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "COPYBOT_MODE=simulation\n"
        "COPYBOT_RISK_MAX_OPEN_POSITIONS=2\n"
        "COPYBOT_SAFETY_MIN_LIQUIDITY_USD=50000\n"
        "COPYBOT_SIM_LATENCY_MS=2500\n"
    )
    assert RiskLimits().max_open_positions == 2
    assert SafetyParams().min_liquidity_usd == 50_000
    assert EngineParams().latency_ms == 2500
    # Una errata en el nombre no se ignora en silencio.
    with (tmp_path / ".env").open("a") as fh:
        fh.write("COPYBOT_RISK_MAX_OPEN_POSITONS=1\n")
    with pytest.raises(ValueError, match="MAX_OPEN_POSITONS"):
        RiskLimits()
    # Y lo del .env tampoco puede relajar los topes.
    (tmp_path / ".env").write_text("COPYBOT_RISK_MAX_OPEN_POSITIONS=9\n")
    with pytest.raises(ValidationError):
        RiskLimits()


def test_unknown_copybot_variables_are_reported(tmp_path):
    from copybot.envcheck import unknown_settings

    env = {
        "COPYBOT_RISK_MAX_OPEN_POSITONS": "2",  # errata
        "COPYBOT_SIM_LATENCY_MS": "2000",
        "COPYBOT_HELIUS_API_KEY": "x",
        "COPYBOT_TEST_DATABASE_URL": "x",
        "OTRA_COSA": "1",
    }
    dotenv = tmp_path / ".env"
    dotenv.write_text("# comentario\nCOPYBOT_SAFETY_MIN_LIQUIDITY_USD=5000\nCOPYBOT_SIMM_X=1\n")
    assert unknown_settings(env, dotenv) == [
        "COPYBOT_RISK_MAX_OPEN_POSITONS",
        "COPYBOT_SIMM_X",
    ]

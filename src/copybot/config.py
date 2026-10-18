"""Configuración del sistema con límites de seguridad fijados en el código.

Los valores pueden venir del entorno (prefijo ``COPYBOT_``) o de ``.env``, pero
nunca pueden superar los topes definidos aquí. Si un valor los supera, la
configuración se rechaza y el sistema no arranca: es preferible no funcionar a
funcionar con un riesgo mayor del permitido.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Topes duros ---------------------------------------------------------------
# Cambiar cualquiera de estos valores exige modificar el código y pasar revisión.

MAX_VIRTUAL_CAPITAL_USD = Decimal("100")
MAX_POSITION_USD = Decimal("10")
MAX_OPEN_POSITIONS = 5
MIN_CASH_RESERVE_PCT = Decimal("20")
MAX_DAILY_LOSS_PCT = Decimal("10")
MAX_DRAWDOWN_PCT = Decimal("25")
MAX_MONTHLY_BUDGET_USD = Decimal("50")


class RiskLimits(BaseSettings):
    """Límites de riesgo. Solo pueden endurecerse respecto a los topes."""

    model_config = SettingsConfigDict(env_prefix="COPYBOT_RISK_", extra="forbid")

    virtual_capital_usd: Decimal = MAX_VIRTUAL_CAPITAL_USD
    max_position_usd: Decimal = MAX_POSITION_USD
    max_open_positions: int = MAX_OPEN_POSITIONS
    min_cash_reserve_pct: Decimal = MIN_CASH_RESERVE_PCT
    max_daily_loss_pct: Decimal = MAX_DAILY_LOSS_PCT
    max_drawdown_pct: Decimal = MAX_DRAWDOWN_PCT

    @model_validator(mode="after")
    def _within_hard_caps(self) -> RiskLimits:
        errors: list[str] = []
        if not (Decimal(0) < self.virtual_capital_usd <= MAX_VIRTUAL_CAPITAL_USD):
            errors.append(f"virtual_capital_usd debe estar en (0, {MAX_VIRTUAL_CAPITAL_USD}]")
        if not (Decimal(0) < self.max_position_usd <= MAX_POSITION_USD):
            errors.append(f"max_position_usd debe estar en (0, {MAX_POSITION_USD}]")
        if self.max_position_usd > self.virtual_capital_usd:
            errors.append("max_position_usd no puede superar el capital virtual")
        if not (1 <= self.max_open_positions <= MAX_OPEN_POSITIONS):
            errors.append(f"max_open_positions debe estar en [1, {MAX_OPEN_POSITIONS}]")
        # La reserva funciona al revés: solo puede aumentar.
        if not (MIN_CASH_RESERVE_PCT <= self.min_cash_reserve_pct < Decimal(100)):
            errors.append(f"min_cash_reserve_pct debe estar en [{MIN_CASH_RESERVE_PCT}, 100)")
        if not (Decimal(0) < self.max_daily_loss_pct <= MAX_DAILY_LOSS_PCT):
            errors.append(f"max_daily_loss_pct debe estar en (0, {MAX_DAILY_LOSS_PCT}]")
        if not (Decimal(0) < self.max_drawdown_pct <= MAX_DRAWDOWN_PCT):
            errors.append(f"max_drawdown_pct debe estar en (0, {MAX_DRAWDOWN_PCT}]")
        if errors:
            raise ValueError("; ".join(errors))
        return self


class Settings(BaseSettings):
    """Configuración general. Los secretos nunca se imprimen ni se registran."""

    model_config = SettingsConfigDict(
        env_prefix="COPYBOT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Único modo existente. Cualquier otro valor se rechaza.
    mode: Literal["simulation"] = "simulation"

    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://copybot:copybot@localhost:5432/copybot"
    )

    # Proveedores de datos
    solana_rpc_url: SecretStr = SecretStr("https://api.mainnet-beta.solana.com")
    solana_ws_url: SecretStr = SecretStr("wss://api.mainnet-beta.solana.com")
    # Si hay clave de Helius, tiene prioridad sobre las URL anteriores.
    helius_api_key: SecretStr | None = None
    rpc_max_requests_per_second: float = Field(default=1.0, gt=0, le=50)
    rpc_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    # Wallet de FOMO que firma y paga las operaciones de sus usuarios (verificado on-chain
    # en la Fase 2; ver docs/informes/fase-2.md).
    fomo_relayer: str = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"

    monthly_budget_usd: Decimal = Field(default=MAX_MONTHLY_BUDGET_USD, gt=0)

    # Si los datos son más antiguos que esto, no se toman decisiones de entrada.
    max_data_staleness_seconds: float = Field(default=30.0, gt=0, le=300)

    @field_validator("monthly_budget_usd")
    @classmethod
    def _budget_cap(cls, value: Decimal) -> Decimal:
        if value > MAX_MONTHLY_BUDGET_USD:
            raise ValueError(f"monthly_budget_usd no puede superar {MAX_MONTHLY_BUDGET_USD}")
        return value

    @field_validator("fomo_relayer")
    @classmethod
    def _valid_relayer(cls, value: str) -> str:
        from copybot.solana.addresses import validate_address

        return validate_address(value)

    def rpc_endpoints(self) -> tuple[str, str]:
        """URL HTTP y WebSocket del proveedor. Contienen la clave: no registrarlas."""
        if self.helius_api_key is not None:
            key = self.helius_api_key.get_secret_value()
            return (
                f"https://mainnet.helius-rpc.com/?api-key={key}",
                f"wss://mainnet.helius-rpc.com/?api-key={key}",
            )
        return self.solana_rpc_url.get_secret_value(), self.solana_ws_url.get_secret_value()

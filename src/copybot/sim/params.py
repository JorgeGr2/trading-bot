"""Parámetros del motor y de los filtros de seguridad (variables ``COPYBOT_SIM_*`` y
``COPYBOT_SAFETY_*``). Los límites de riesgo están en ``copybot.config.RiskLimits``."""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from copybot.config import MAX_POSITION_USD, GroupSettings


class EngineParams(GroupSettings):
    model_config = SettingsConfigDict(
        env_prefix="COPYBOT_SIM_", env_file=".env", env_file_encoding="utf-8", extra="forbid"
    )

    # Forma parte de la clave de idempotencia: cambiarla es una decisión nueva.
    decision_version: str = "1"
    # Una compra del trader más antigua que esto ya no se copia (la oportunidad pasó).
    max_signal_age_seconds: float = Field(default=60, gt=0, le=600)
    # Tiempo simulado entre decidir (t2) y ejecutarse la orden (t3): envío y confirmación.
    latency_ms: int = Field(default=1500, ge=0, le=60_000)
    # Si en t3 se reciben más de un 3 % menos tokens que en t2, la orden falla.
    slippage_tolerance_bps: int = Field(default=300, ge=0, le=5000)
    # Recorte supuesto por bots que se adelantan (MEV / sandwich).
    mev_haircut_bps: int = Field(default=50, ge=0, le=2000)
    base_fee_lamports: int = Field(default=5_000, ge=0)
    priority_fee_lamports: int = Field(default=100_000, ge=0)
    # Tamaño fijo de cada operación en el libro de señales (sin límite de capital). Por
    # defecto, el mismo que la cartera: así ambos libros comparten la ejecución.
    signal_book_size_usd: Decimal = Field(default=MAX_POSITION_USD, gt=0, le=MAX_POSITION_USD)
    stop_loss_pct: Decimal = Field(default=Decimal(50), gt=0, le=100)
    max_hold_hours: Decimal = Field(default=Decimal(48), gt=0, le=24 * 30)
    # Si tras una venta queda menos de esta fracción, se vende todo.
    exit_dust_fraction: Decimal = Field(default=Decimal("0.02"), ge=0, lt=1)
    revalue_seconds: int = Field(default=60, gt=0, le=3600)
    poll_seconds: float = Field(default=1.0, gt=0, le=60)


class SafetyParams(GroupSettings):
    """Filtros de seguridad de un token antes de comprarlo. Las autoridades de emisión y
    congelación, y las extensiones peligrosas de Token-2022, se rechazan siempre."""

    model_config = SettingsConfigDict(
        env_prefix="COPYBOT_SAFETY_", env_file=".env", env_file_encoding="utf-8", extra="forbid"
    )

    min_liquidity_usd: Decimal = Field(default=Decimal(20_000), ge=0)
    max_price_impact_pct: Decimal = Field(default=Decimal(3), gt=0, le=50)
    # Comprar y vender a la vez no puede costar más que esto (detecta tokens invendibles).
    max_roundtrip_loss_pct: Decimal = Field(default=Decimal(10), gt=0, le=100)
    max_transfer_fee_bps: int = Field(default=100, ge=0, le=10_000)
    # Rechazar tokens cuyo creador puede subir la comisión de transferencia.
    reject_mutable_transfer_fee: bool = True
    mint_check_cache_minutes: int = Field(default=30, gt=0, le=24 * 60)

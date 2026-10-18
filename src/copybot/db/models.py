"""Modelo de datos (PostgreSQL).

Las restricciones ``UNIQUE`` y las claves primarias naturales (la firma de la
transacción) hacen que procesar dos veces el mismo evento no tenga efecto: es la
base de la idempotencia del sistema.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ADDRESS = String(44)
SIGNATURE = String(90)
AMOUNT = Numeric(38, 18)


class Base(DeclarativeBase):
    pass


class TrackedWallet(Base):
    __tablename__ = "tracked_wallets"

    address: Mapped[str] = mapped_column(ADDRESS, primary_key=True)
    label: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(20))  # manual | discovery
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint("source IN ('manual', 'discovery')", name="source_valid"),)


class IngestCheckpoint(Base):
    """Última firma procesada sin huecos para cada dirección."""

    __tablename__ = "ingest_checkpoints"

    address: Mapped[str] = mapped_column(ADDRESS, primary_key=True)
    last_signature: Mapped[str] = mapped_column(SIGNATURE)
    last_slot: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RawTransaction(Base):
    __tablename__ = "raw_transactions"

    signature: Mapped[str] = mapped_column(SIGNATURE, primary_key=True)
    slot: Mapped[int] = mapped_column(BigInteger, index=True)
    block_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fee_payer: Mapped[str] = mapped_column(ADDRESS)
    via_fomo: Mapped[bool] = mapped_column(Boolean)
    succeeded: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(40))  # proveedor del que se obtuvo
    # Cómo se detectó por primera vez: en vivo (live) o al recuperar huecos (backfill).
    detected_via: Mapped[str] = mapped_column(String(10))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detection_latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("detected_via IN ('live', 'backfill')", name="detected_via_valid"),
    )


class WalletEvent(Base):
    __tablename__ = "wallet_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signature: Mapped[str] = mapped_column(
        SIGNATURE, ForeignKey("raw_transactions.signature", ondelete="RESTRICT")
    )
    wallet: Mapped[str] = mapped_column(ADDRESS)
    kind: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str] = mapped_column(String(10))
    mint: Mapped[str | None] = mapped_column(ADDRESS)
    token_delta: Mapped[Decimal] = mapped_column(AMOUNT)
    quote_symbol: Mapped[str | None] = mapped_column(String(10))
    quote_delta: Mapped[Decimal] = mapped_column(AMOUNT)
    wallet_is_signer: Mapped[bool] = mapped_column(Boolean)
    via_fomo: Mapped[bool] = mapped_column(Boolean)
    reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)
    slot: Mapped[int] = mapped_column(BigInteger)
    block_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detected_via: Mapped[str] = mapped_column(String(10))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    classifier_version: Mapped[str] = mapped_column(String(20))

    __table_args__ = (
        UniqueConstraint("signature", "wallet", name="uq_wallet_events_signature_wallet"),
        Index("ix_wallet_events_wallet_slot", "wallet", "slot"),
        CheckConstraint(
            "kind IN ('buy', 'sell', 'transfer_in', 'transfer_out', 'ambiguous')",
            name="kind_valid",
        ),
        CheckConstraint("confidence IN ('high', 'medium', 'low')", name="confidence_valid"),
    )


class ProviderUsage(Base):
    """Peticiones y créditos consumidos por proveedor, método y día (control de gasto)."""

    __tablename__ = "provider_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    method: Mapped[str] = mapped_column(String(60), primary_key=True)
    requests: Mapped[int] = mapped_column(BigInteger, default=0)
    credits: Mapped[int] = mapped_column(BigInteger, default=0)


class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeats"

    service: Mapped[str] = mapped_column(String(40), primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

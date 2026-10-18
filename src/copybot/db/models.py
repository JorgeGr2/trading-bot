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
    Integer,
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
    # Cómo se detectó por primera vez: en vivo (live), al recuperar huecos (backfill) o
    # al importar el historial de una wallet para analizarla (history).
    detected_via: Mapped[str] = mapped_column(String(10))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Momento en que se obtuvo la transacción completa (cuando ya se puede decidir).
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detection_latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    # Posición dentro del bloque (solo la conocemos en el historial de Helius).
    tx_index: Mapped[int | None] = mapped_column(Integer)
    # JSON completo de la transacción. En el historial no se guarda (~30 KB por
    # transacción y se puede volver a descargar); en vivo y en la recuperación, siempre.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))

    __table_args__ = (
        CheckConstraint(
            "detected_via IN ('live', 'backfill', 'history')", name="detected_via_valid"
        ),
        CheckConstraint("payload IS NOT NULL OR detected_via = 'history'", name="payload_required"),
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
    tx_index: Mapped[int | None] = mapped_column(Integer)
    detected_via: Mapped[str] = mapped_column(String(10))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    classifier_version: Mapped[str] = mapped_column(String(20))

    __table_args__ = (
        UniqueConstraint("signature", "wallet", name="uq_wallet_events_signature_wallet"),
        Index("ix_wallet_events_wallet_slot", "wallet", "slot"),
        CheckConstraint(
            "detected_via IN ('live', 'backfill', 'history')", name="event_detected_via_valid"
        ),
        CheckConstraint(
            "kind IN ('buy', 'sell', 'transfer_in', 'transfer_out', 'ambiguous')",
            name="kind_valid",
        ),
        CheckConstraint("confidence IN ('high', 'medium', 'low')", name="confidence_valid"),
    )


class Token(Base):
    """Datos de cada token que usa el analizador. La creación se obtiene con una
    consulta por token (la primera transacción del mint) y se guarda para siempre."""

    __tablename__ = "tokens"

    mint: Mapped[str] = mapped_column(ADDRESS, primary_key=True)
    created_slot: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    creation_signature: Mapped[str | None] = mapped_column(SIGNATURE)
    creation_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiscoveryWindow(Base):
    """Un tramo del historial del relayer de FOMO muestreado para descubrir traders."""

    __tablename__ = "discovery_windows"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Instante pedido (se descargan las transacciones a partir de aquí, en orden).
    requested_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_block_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_block_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transactions: Mapped[int] = mapped_column(Integer)
    trades: Mapped[int] = mapped_column(Integer)
    credits: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DiscoveryObservation(Base):
    """Una compraventa de un usuario de FOMO vista al muestrear el relayer.

    Es una muestra, no el historial completo de la wallet: sirve para elegir a quién
    analizar, nunca para calcular sus métricas.
    """

    __tablename__ = "discovery_observations"

    signature: Mapped[str] = mapped_column(SIGNATURE, primary_key=True)
    wallet: Mapped[str] = mapped_column(ADDRESS, primary_key=True)
    window_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("discovery_windows.id", ondelete="CASCADE")
    )
    block_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(20))
    mint: Mapped[str] = mapped_column(ADDRESS)
    usd_value: Mapped[Decimal | None] = mapped_column(AMOUNT)

    __table_args__ = (
        Index("ix_discovery_observations_wallet", "wallet"),
        CheckConstraint("kind IN ('buy', 'sell')", name="observation_kind_valid"),
    )


class CandidateWallet(Base):
    """Wallet candidata a analizar, de dónde viene y cuánto opera.

    ``relayer``: vista al muestrear el relayer de FOMO. ``leaderboard``: añadida a mano
    desde la leaderboard de FOMO (su PnL allí no se usa: solo sirve de pista).
    ``manual``: añadida a mano por otro motivo.
    """

    __tablename__ = "candidate_wallets"

    wallet: Mapped[str] = mapped_column(ADDRESS, primary_key=True)
    source: Mapped[str] = mapped_column(String(20))
    label: Mapped[str | None] = mapped_column(String(100))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Filtro de actividad: transacciones con éxito en los últimos ``activity_days`` días.
    screened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activity_days: Mapped[int | None] = mapped_column(Integer)
    recent_transactions: Mapped[int | None] = mapped_column(Integer)
    # Se alcanzó el máximo de la consulta: hay al menos ``recent_transactions``.
    activity_capped: Mapped[bool | None] = mapped_column(Boolean)

    __table_args__ = (
        CheckConstraint(
            "source IN ('relayer', 'leaderboard', 'manual')", name="candidate_source_valid"
        ),
        Index("ix_candidate_wallets_source", "source"),
    )


class HistoryImport(Base):
    """Periodo del historial de una wallet que está importado **completo**.

    El analizador solo calcula métricas dentro de ``[covered_from, covered_to]``.
    """

    __tablename__ = "history_imports"

    wallet: Mapped[str] = mapped_column(ADDRESS, primary_key=True)
    covered_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    covered_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # El periodo pedido era más largo, pero se alcanzó el máximo de páginas.
    truncated: Mapped[bool] = mapped_column(Boolean)
    transactions: Mapped[int] = mapped_column(Integer)
    credits: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (CheckConstraint("covered_from <= covered_to", name="coverage_ordered"),)


class WalletMetricSnapshot(Base):
    """Métricas de una wallet calculadas solo con eventos hasta ``as_of``.

    ``params_hash`` identifica la versión del analizador y sus parámetros: el mismo
    ``as_of`` con los mismos parámetros da siempre el mismo resultado.
    """

    __tablename__ = "wallet_metric_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(ADDRESS)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_days: Mapped[int] = mapped_column(Integer)
    analyzer_version: Mapped[str] = mapped_column(String(20))
    params_hash: Mapped[str] = mapped_column(String(64))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "wallet", "as_of", "window_days", "params_hash", name="uq_metric_snapshot"
        ),
        CheckConstraint("window_days > 0", name="window_positive"),
    )


class WalletScore(Base):
    """Puntuación explicada de una wallet a fecha ``as_of`` (versionada)."""

    __tablename__ = "wallet_scores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(ADDRESS)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scoring_version: Mapped[str] = mapped_column(String(20))
    params_hash: Mapped[str] = mapped_column(String(64))
    eligible: Mapped[bool] = mapped_column(Boolean)
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    components: Mapped[dict[str, Any]] = mapped_column(JSONB)
    flags: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    explanation: Mapped[list[str]] = mapped_column(JSONB)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("wallet", "as_of", "params_hash", name="uq_wallet_score"),
        Index("ix_wallet_scores_as_of", "as_of"),
        CheckConstraint("eligible = (score IS NOT NULL)", name="score_iff_eligible"),
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="score_range"),
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

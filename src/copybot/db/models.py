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
    text,
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


class TokenPrice(Base):
    """Precio y liquidez de un token observados en un momento (Jupiter Price API).

    El analizador solo usa precios observados antes de su ``as_of``.
    """

    __tablename__ = "token_prices"

    mint: Mapped[str] = mapped_column(ADDRESS, primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    usd_price: Mapped[Decimal | None] = mapped_column(AMOUNT)  # None: sin precio
    liquidity_usd: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    source: Mapped[str] = mapped_column(String(20))


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


# --- Simulación (Fase 4) ---------------------------------------------------------

SYSTEM_STATES = ("RECOVERING", "RUNNING", "PAUSED", "HALTED_RISK", "READ_ONLY", "STOPPED")
BOOKS = ("portfolio", "signals")


class ControlState(Base):
    """Estado global del sistema (una sola fila). Fuera de RUNNING no se abren posiciones.

    La comprobación se hace dentro de la misma transacción que abre la posición.
    """

    __tablename__ = "control_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(300), default="")
    updated_by: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("id = 1", name="control_state_single_row"),
        CheckConstraint(f"state IN {SYSTEM_STATES}", name="control_state_valid"),
    )


class Signal(Base):
    """Una compraventa de un trader seguido que el motor debe estudiar (una por evento)."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("wallet_events.id", ondelete="RESTRICT"), unique=True
    )
    wallet: Mapped[str] = mapped_column(ADDRESS)
    mint: Mapped[str] = mapped_column(ADDRESS)
    side: Mapped[str] = mapped_column(String(4))
    trader_token_amount: Mapped[Decimal] = mapped_column(AMOUNT)
    trader_usd: Mapped[Decimal | None] = mapped_column(AMOUNT)
    # Fracción de su posición que vende el trader (solo ventas).
    trader_sell_fraction: Mapped[Decimal | None] = mapped_column(AMOUNT)
    block_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_via: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("side IN ('buy', 'sell')", name="signal_side_valid"),
        CheckConstraint("trader_token_amount > 0", name="signal_amount_positive"),
    )


class SimOrder(Base):
    """Orden simulada. ``idempotency_key`` es única: la misma decisión nunca se repite."""

    __tablename__ = "sim_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    book: Mapped[str] = mapped_column(String(10))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    signal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="RESTRICT")
    )
    position_id: Mapped[int | None] = mapped_column(BigInteger)
    side: Mapped[str] = mapped_column(String(4))
    purpose: Mapped[str] = mapped_column(String(20))  # entry | copy_exit | stop_loss | max_hold…
    mint: Mapped[str] = mapped_column(ADDRESS)
    wallet: Mapped[str] = mapped_column(ADDRESS)  # trader copiado
    requested_usd: Mapped[Decimal | None] = mapped_column(AMOUNT)
    requested_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    reserved_usd: Mapped[Decimal] = mapped_column(AMOUNT, default=Decimal(0))
    status: Mapped[str] = mapped_column(String(10))
    status_reason: Mapped[str] = mapped_column(String(300), default="")
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"book IN {BOOKS}", name="order_book_valid"),
        CheckConstraint("side IN ('buy', 'sell')", name="order_side_valid"),
        CheckConstraint(
            "status IN ('pending', 'filled', 'failed', 'rejected')", name="order_status_valid"
        ),
        CheckConstraint("reserved_usd >= 0", name="order_reserved_non_negative"),
        Index("ix_sim_orders_status", "status"),
    )


class SimFill(Base):
    """Ejecución simulada de una orden (como mucho una por orden)."""

    __tablename__ = "sim_fills"

    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sim_orders.id", ondelete="RESTRICT"), primary_key=True
    )
    token_amount: Mapped[Decimal] = mapped_column(AMOUNT)
    usd_amount: Mapped[Decimal] = mapped_column(AMOUNT)  # pagado (compra) o cobrado (venta)
    fees_usd: Mapped[Decimal] = mapped_column(AMOUNT)
    price_usd: Mapped[Decimal] = mapped_column(AMOUNT)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("token_amount > 0", name="fill_amount_positive"),
        CheckConstraint("usd_amount >= 0 AND fees_usd >= 0", name="fill_usd_non_negative"),
    )


class SimPosition(Base):
    """Posición simulada de un libro en un token, copiando a un trader."""

    __tablename__ = "sim_positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    book: Mapped[str] = mapped_column(String(10))
    mint: Mapped[str] = mapped_column(ADDRESS)
    wallet: Mapped[str] = mapped_column(ADDRESS)
    status: Mapped[str] = mapped_column(String(10))
    amount: Mapped[Decimal] = mapped_column(AMOUNT)
    cost_usd: Mapped[Decimal] = mapped_column(AMOUNT)  # coste de lo que queda
    invested_usd: Mapped[Decimal] = mapped_column(AMOUNT)  # todo lo pagado, comisiones incluidas
    proceeds_usd: Mapped[Decimal] = mapped_column(AMOUNT, default=Decimal(0))
    realized_pnl_usd: Mapped[Decimal] = mapped_column(AMOUNT, default=Decimal(0))
    rent_lamports: Mapped[int] = mapped_column(BigInteger, default=0)  # recuperable al cerrar
    rent_usd: Mapped[Decimal] = mapped_column(AMOUNT, default=Decimal(0))  # pagado al abrir
    # Copia teórica: lo que habría pasado comprando y vendiendo al precio del trader.
    theoretical_entry_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    # Tokens que tendría una copia sin costes al precio del trader (None: sin precio).
    theoretical_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    theoretical_proceeds_usd: Mapped[Decimal] = mapped_column(AMOUNT, default=Decimal(0))
    last_value_usd: Mapped[Decimal | None] = mapped_column(AMOUNT)
    last_valued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unsellable: Mapped[bool] = mapped_column(Boolean, default=False)
    # Hay que vender (el trader vendió, o salta un stop) y aún no se ha conseguido.
    exit_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    exit_purpose: Mapped[str | None] = mapped_column(String(20))
    last_exit_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entry_order_id: Mapped[int] = mapped_column(BigInteger)

    __table_args__ = (
        CheckConstraint(f"book IN {BOOKS}", name="position_book_valid"),
        CheckConstraint("status IN ('open', 'closed')", name="position_status_valid"),
        CheckConstraint("amount >= 0", name="position_amount_non_negative"),
        CheckConstraint("cost_usd >= 0", name="position_cost_non_negative"),
        CheckConstraint("status = 'open' OR amount = 0", name="closed_position_is_empty"),
        Index(
            "uq_open_position",
            "book",
            "mint",
            "wallet",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )


class BookCash(Base):
    """Efectivo de un libro. La base de datos impide que sea negativo."""

    __tablename__ = "book_cash"

    book: Mapped[str] = mapped_column(String(10), primary_key=True)
    initial_usd: Mapped[Decimal] = mapped_column(AMOUNT)
    cash_usd: Mapped[Decimal] = mapped_column(AMOUNT)  # disponible
    reserved_usd: Mapped[Decimal] = mapped_column(AMOUNT, default=Decimal(0))  # órdenes en curso
    peak_equity_usd: Mapped[Decimal] = mapped_column(AMOUNT)
    day: Mapped[date] = mapped_column(Date)
    day_start_equity_usd: Mapped[Decimal] = mapped_column(AMOUNT)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"book IN {BOOKS}", name="cash_book_valid"),
        CheckConstraint("cash_usd >= 0", name="cash_non_negative"),
        CheckConstraint("reserved_usd >= 0", name="reserved_non_negative"),
    )


class LedgerEntry(Base):
    """Diario de doble entrada: los apuntes de cada operación suman cero."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    book: Mapped[str] = mapped_column(String(10))
    order_id: Mapped[int | None] = mapped_column(BigInteger)
    position_id: Mapped[int | None] = mapped_column(BigInteger)
    account: Mapped[str] = mapped_column(String(20))  # cash | reserved | position | fees | pnl
    delta_usd: Mapped[Decimal] = mapped_column(AMOUNT)
    memo: Mapped[str] = mapped_column(String(200), default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "account IN ('cash', 'reserved', 'position', 'fees', 'pnl', 'capital')",
            name="ledger_account_valid",
        ),
        Index("ix_ledger_order", "order_id"),
    )


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    book: Mapped[str] = mapped_column(String(10))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cash_usd: Mapped[Decimal] = mapped_column(AMOUNT)
    positions_value_usd: Mapped[Decimal] = mapped_column(AMOUNT)
    equity_usd: Mapped[Decimal] = mapped_column(AMOUNT)
    open_positions: Mapped[int] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (Index("ix_portfolio_snapshots_book_at", "book", "at"),)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    book: Mapped[str | None] = mapped_column(String(10))
    kind: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class TokenSafetyCheck(Base):
    """Resultado de los filtros de seguridad de un token en un momento."""

    __tablename__ = "token_safety_checks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mint: Mapped[str] = mapped_column(ADDRESS)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    passed: Mapped[bool] = mapped_column(Boolean)
    reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (Index("ix_token_safety_mint_at", "mint", "checked_at"),)


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


# --- Fase 5: backtester ------------------------------------------------------------------


class DelayedPrice(Base):
    """Precio al que se operaba un token unos segundos después de un instante dado,
    sacado de las operaciones reales de otros (para copiar con retraso en el backtest).

    Es una caché: el backtest se repite sin volver a gastar créditos y da siempre lo mismo.
    """

    __tablename__ = "backtest_delayed_prices"

    mint: Mapped[str] = mapped_column(ADDRESS, primary_key=True)
    at: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # segundos Unix
    delay_from: Mapped[int] = mapped_column(Integer, primary_key=True)  # segundos después
    delay_to: Mapped[int] = mapped_column(Integer, primary_key=True)
    price_usd: Mapped[Decimal | None] = mapped_column(AMOUNT)  # None: no hubo operaciones
    swaps: Mapped[int] = mapped_column(Integer)  # operaciones usadas
    window_end: Mapped[int] = mapped_column(Integer)  # hasta dónde hubo que mirar (s)
    sol_usd: Mapped[Decimal | None] = mapped_column(AMOUNT)  # precio de SOL usado
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PriceBar(Base):
    """Vela de precio (Kraken, GeckoTerminal…) para las estrategias de referencia."""

    __tablename__ = "backtest_price_bars"

    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    asset: Mapped[str] = mapped_column(String(60), primary_key=True)
    interval: Mapped[str] = mapped_column(String(5), primary_key=True)  # 1m, 1h, 1d
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(AMOUNT)
    high: Mapped[Decimal] = mapped_column(AMOUNT)
    low: Mapped[Decimal] = mapped_column(AMOUNT)
    close: Mapped[Decimal] = mapped_column(AMOUNT)
    volume: Mapped[Decimal | None] = mapped_column(AMOUNT)

    __table_args__ = (CheckConstraint("low <= high AND low > 0", name="bar_prices_valid"),)

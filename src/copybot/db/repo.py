"""Operaciones de base de datos idempotentes usadas por la ingesta y el análisis."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, Row, case, cast, distinct, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.db.models import (
    CandidateWallet,
    DiscoveryObservation,
    DiscoveryWindow,
    HistoryImport,
    IngestCheckpoint,
    ProviderUsage,
    RawTransaction,
    ServiceHeartbeat,
    Token,
    TrackedWallet,
    WalletMetricSnapshot,
    WalletScore,
)
from copybot.db.models import (
    WalletEvent as WalletEventRow,
)
from copybot.solana.addresses import validate_address
from copybot.solana.classifier import TxSummary, WalletEvent

# 2: los restos de un token intermedio de la ruta ya no convierten la compra en ambigua;
#    solo se descuenta la renta de las cuentas propias de la wallet, y un movimiento
#    pequeño de SOL junto a un pago en USDC/USDT se trata como coste de cuentas.
CLASSIFIER_VERSION = "2"


def _ts(epoch: int | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, tz=UTC) if epoch is not None else None


def _event_row(
    ev: WalletEvent, *, detected_via: str, detected_at: datetime, tx_index: int | None = None
) -> dict[str, Any]:
    return {
        "signature": ev.signature,
        "wallet": ev.wallet,
        "kind": ev.kind.value,
        "confidence": ev.confidence.value,
        "mint": ev.mint,
        "token_delta": ev.token_delta,
        "quote_symbol": ev.quote_symbol,
        "quote_delta": ev.quote_delta,
        "wallet_is_signer": ev.wallet_is_signer,
        "via_fomo": ev.via_fomo,
        "reasons": list(ev.reasons),
        "slot": ev.slot,
        "block_time": _ts(ev.block_time),
        "tx_index": tx_index,
        "detected_via": detected_via,
        "detected_at": detected_at,
        "classifier_version": CLASSIFIER_VERSION,
    }


@dataclass(frozen=True)
class HistoryItem:
    """Una transacción del historial de una wallet, ya clasificada."""

    summary: TxSummary
    tx_index: int | None
    events: list[WalletEvent]


@dataclass(frozen=True)
class Observation:
    signature: str
    wallet: str
    block_time: datetime | None
    kind: str
    mint: str
    usd_value: Decimal | None


@dataclass(frozen=True)
class Candidate:
    wallet: str
    windows: int
    trades: int
    buys: int
    sells: int
    volume_usd: Decimal
    last_seen: datetime | None


class Repository:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessionmaker

    # --- Wallets ----------------------------------------------------------------

    async def add_wallet(self, address: str, *, label: str | None = None, source: str) -> bool:
        address = validate_address(address)
        async with self._sessions.begin() as s:
            result = await s.execute(
                insert(TrackedWallet)
                .values(address=address, label=label, source=source, active=True)
                .on_conflict_do_nothing(index_elements=["address"])
                .returning(TrackedWallet.address)
            )
            return result.scalar_one_or_none() is not None

    async def active_wallets(self) -> list[str]:
        async with self._sessions() as s:
            rows = await s.scalars(
                select(TrackedWallet.address)
                .where(TrackedWallet.active.is_(True))
                .order_by(TrackedWallet.address)
            )
            return list(rows)

    # --- Checkpoints ------------------------------------------------------------

    async def get_checkpoint(self, address: str) -> IngestCheckpoint | None:
        async with self._sessions() as s:
            return await s.get(IngestCheckpoint, address)

    async def set_checkpoint(self, address: str, signature: str, slot: int) -> None:
        """Avanza el checkpoint; nunca lo hace retroceder."""
        async with self._sessions.begin() as s:
            stmt = insert(IngestCheckpoint).values(
                address=address, last_signature=signature, last_slot=slot
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["address"],
                set_={
                    "last_signature": signature,
                    "last_slot": slot,
                    "updated_at": datetime.now(UTC),
                },
                where=IngestCheckpoint.last_slot <= slot,
            )
            await s.execute(stmt)

    # --- Transacciones y eventos ------------------------------------------------

    async def has_transaction(self, signature: str) -> bool:
        """¿Está ya guardada con su JSON completo? Las filas del historial (sin JSON) no
        cuentan: el monitor las vuelve a procesar para añadir los eventos de las wallets
        que sigue."""
        async with self._sessions() as s:
            found = await s.scalar(
                select(RawTransaction.signature).where(
                    RawTransaction.signature == signature, RawTransaction.payload.is_not(None)
                )
            )
            return found is not None

    async def store_transaction(
        self,
        tx: dict[str, Any],
        summary: TxSummary,
        events: Iterable[WalletEvent],
        *,
        source: str,
        detected_via: str,
        first_seen_at: datetime,
        fetched_at: datetime | None = None,
    ) -> bool:
        """Guarda la transacción y sus eventos de forma atómica.

        Devuelve ``False`` si la transacción ya existía con su JSON (en ese caso no se
        toca nada: la primera detección es la que cuenta). Si solo existía como fila del
        historial, se completa su JSON y se añaden los eventos que falten, sin cambiar los
        que ya había.
        """
        latency_ms = None
        if summary.block_time is not None and detected_via == "live":
            latency_ms = int((first_seen_at.timestamp() - summary.block_time) * 1000)
        async with self._sessions.begin() as s:
            stmt = insert(RawTransaction).values(
                signature=summary.signature,
                slot=summary.slot,
                block_time=_ts(summary.block_time),
                fee_payer=summary.fee_payer,
                via_fomo=summary.via_fomo,
                succeeded=summary.succeeded,
                source=source,
                detected_via=detected_via,
                first_seen_at=first_seen_at,
                fetched_at=fetched_at or first_seen_at,
                detection_latency_ms=latency_ms,
                payload=tx,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["signature"],
                set_={"payload": stmt.excluded.payload},
                where=RawTransaction.payload.is_(None),
            ).returning(RawTransaction.signature)
            if (await s.execute(stmt)).scalar_one_or_none() is None:
                return False
            rows = [
                _event_row(ev, detected_via=detected_via, detected_at=first_seen_at)
                for ev in events
            ]
            if rows:
                await s.execute(
                    insert(WalletEventRow)
                    .values(rows)
                    .on_conflict_do_nothing(constraint="uq_wallet_events_signature_wallet")
                )
            return True

    async def store_history_batch(
        self, items: Sequence[HistoryItem], *, source: str, imported_at: datetime
    ) -> tuple[int, int]:
        """Guarda una página del historial de una wallet (sin el JSON completo).

        Idempotente: lo que ya exista no se toca. A diferencia de la ingesta en vivo, los
        eventos se añaden aunque la transacción ya estuviera guardada (por ejemplo, una
        transferencia entre dos wallets analizadas). Devuelve (transacciones nuevas,
        eventos nuevos).
        """
        if not items:
            return 0, 0
        raw_rows = [
            {
                "signature": it.summary.signature,
                "slot": it.summary.slot,
                "block_time": _ts(it.summary.block_time),
                "fee_payer": it.summary.fee_payer,
                "via_fomo": it.summary.via_fomo,
                "succeeded": it.summary.succeeded,
                "source": source,
                "detected_via": "history",
                "first_seen_at": imported_at,
                "fetched_at": imported_at,
                "detection_latency_ms": None,
                "tx_index": it.tx_index,
                "payload": None,
            }
            for it in items
        ]
        event_rows = [
            _event_row(ev, detected_via="history", detected_at=imported_at, tx_index=it.tx_index)
            for it in items
            for ev in it.events
        ]
        async with self._sessions.begin() as s:
            new_raw = 0
            for chunk in _chunks(raw_rows, 500):
                result = await s.execute(
                    insert(RawTransaction)
                    .values(chunk)
                    .on_conflict_do_nothing(index_elements=["signature"])
                    .returning(RawTransaction.signature)
                )
                new_raw += len(result.all())
            new_events = 0
            for chunk in _chunks(event_rows, 500):
                result = await s.execute(
                    insert(WalletEventRow)
                    .values(chunk)
                    .on_conflict_do_nothing(constraint="uq_wallet_events_signature_wallet")
                    .returning(WalletEventRow.id)
                )
                new_events += len(result.all())
        return new_raw, new_events

    async def get_history_import(self, wallet: str) -> HistoryImport | None:
        async with self._sessions() as s:
            return await s.get(HistoryImport, wallet)

    async def save_history_import(
        self,
        wallet: str,
        *,
        covered_from: datetime,
        covered_to: datetime,
        truncated: bool,
        transactions: int,
        credits: int,
    ) -> None:
        async with self._sessions.begin() as s:
            stmt = insert(HistoryImport).values(
                wallet=wallet,
                covered_from=covered_from,
                covered_to=covered_to,
                truncated=truncated,
                transactions=transactions,
                credits=credits,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["wallet"],
                set_={
                    "covered_from": stmt.excluded.covered_from,
                    "covered_to": stmt.excluded.covered_to,
                    "truncated": stmt.excluded.truncated,
                    "transactions": HistoryImport.transactions + stmt.excluded.transactions,
                    "credits": HistoryImport.credits + stmt.excluded.credits,
                    "updated_at": datetime.now(UTC),
                },
            )
            await s.execute(stmt)

    async def imported_wallets(self) -> list[HistoryImport]:
        async with self._sessions() as s:
            return list(await s.scalars(select(HistoryImport).order_by(HistoryImport.wallet)))

    async def wallet_events_until(self, wallet: str, until: datetime) -> list[WalletEventRow]:
        """Eventos de la wallet con ``block_time`` hasta ``until`` (incluido), en orden."""
        async with self._sessions() as s:
            rows = await s.scalars(
                select(WalletEventRow)
                .where(WalletEventRow.wallet == wallet, WalletEventRow.block_time <= until)
                .order_by(WalletEventRow.slot, WalletEventRow.tx_index, WalletEventRow.id)
            )
            return list(rows)

    # --- Descubrimiento ----------------------------------------------------------

    async def save_discovery_window(
        self,
        *,
        requested_start: datetime,
        first_block_time: datetime | None,
        last_block_time: datetime | None,
        transactions: int,
        credits: int,
        observations: Sequence[Observation],
    ) -> tuple[int, int]:
        """Guarda una ventana muestreada y sus observaciones. Devuelve (id, nuevas)."""
        async with self._sessions.begin() as s:
            window_id = await s.scalar(
                insert(DiscoveryWindow)
                .values(
                    requested_start=requested_start,
                    first_block_time=first_block_time,
                    last_block_time=last_block_time,
                    transactions=transactions,
                    trades=len(observations),
                    credits=credits,
                )
                .returning(DiscoveryWindow.id)
            )
            new = 0
            rows = [
                {
                    "signature": o.signature,
                    "wallet": o.wallet,
                    "window_id": window_id,
                    "block_time": o.block_time,
                    "kind": o.kind,
                    "mint": o.mint,
                    "usd_value": o.usd_value,
                }
                for o in observations
            ]
            for chunk in _chunks(rows, 500):
                result = await s.execute(
                    insert(DiscoveryObservation)
                    .values(chunk)
                    .on_conflict_do_nothing(index_elements=["signature", "wallet"])
                    .returning(DiscoveryObservation.wallet)
                )
                new += len(result.all())
            return int(window_id), new

    async def discovery_candidates(
        self, *, limit: int, min_trades: int = 2, exclude_imported: bool = True
    ) -> list[Candidate]:
        """Wallets vistas al muestrear el relayer, de más a menos recurrentes."""
        obs = DiscoveryObservation
        windows = func.count(distinct(obs.window_id))
        trades = func.count()
        stmt = (
            select(
                obs.wallet,
                windows.label("windows"),
                trades.label("trades"),
                func.count().filter(obs.kind == "buy").label("buys"),
                func.count().filter(obs.kind == "sell").label("sells"),
                func.coalesce(func.sum(obs.usd_value), 0).label("volume"),
                func.max(obs.block_time).label("last_seen"),
            )
            .group_by(obs.wallet)
            .having(trades >= min_trades)
            .order_by(windows.desc(), trades.desc(), obs.wallet)
            .limit(limit)
        )
        if exclude_imported:
            stmt = stmt.where(obs.wallet.not_in(select(HistoryImport.wallet)))
        async with self._sessions() as s:
            rows: Sequence[Row[Any]] = (await s.execute(stmt)).all()
        return [
            Candidate(
                r.wallet, r.windows, r.trades, r.buys, r.sells, Decimal(r.volume), r.last_seen
            )
            for r in rows
        ]

    async def add_candidate(self, wallet: str, *, source: str, label: str | None = None) -> bool:
        """Registra una candidata. Si ya existía, conserva el origen más específico
        (la leaderboard o el alta manual valen más que el muestreo) y la etiqueta."""
        wallet = validate_address(wallet)
        async with self._sessions.begin() as s:
            stmt = insert(CandidateWallet).values(wallet=wallet, source=source, label=label)
            stmt = stmt.on_conflict_do_update(
                index_elements=["wallet"],
                set_={
                    "label": func.coalesce(stmt.excluded.label, CandidateWallet.label),
                    "source": case(
                        (CandidateWallet.source == "relayer", stmt.excluded.source),
                        else_=CandidateWallet.source,
                    ),
                },
            ).returning(CandidateWallet.wallet, (literal_column("xmax") == 0).label("new"))
            row = (await s.execute(stmt)).one()
            return bool(row.new)

    async def wallets_to_screen(self, *, limit: int, max_windows: int | None = None) -> list[str]:
        """Wallets vistas en el relayer, aún sin filtro de actividad ni historial.

        Orden pseudoaleatorio fijo (por la dirección), para no favorecer a nadie.
        """
        obs = DiscoveryObservation
        stmt = (
            select(obs.wallet)
            .group_by(obs.wallet)
            .where(
                obs.wallet.not_in(select(HistoryImport.wallet)),
                obs.wallet.not_in(
                    select(CandidateWallet.wallet).where(CandidateWallet.screened_at.is_not(None))
                ),
            )
            .order_by(func.md5(obs.wallet))
            .limit(limit)
        )
        if max_windows is not None:
            stmt = stmt.having(func.count(distinct(obs.window_id)) <= max_windows)
        async with self._sessions() as s:
            return list(await s.scalars(stmt))

    async def save_screening(
        self,
        wallet: str,
        *,
        days: int,
        transactions: int,
        capped: bool,
        screened_at: datetime,
        source: str = "relayer",
    ) -> None:
        async with self._sessions.begin() as s:
            stmt = insert(CandidateWallet).values(
                wallet=wallet,
                source=source,
                screened_at=screened_at,
                activity_days=days,
                recent_transactions=transactions,
                activity_capped=capped,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["wallet"],
                set_={
                    "screened_at": stmt.excluded.screened_at,
                    "activity_days": stmt.excluded.activity_days,
                    "recent_transactions": stmt.excluded.recent_transactions,
                    "activity_capped": stmt.excluded.activity_capped,
                },
            )
            await s.execute(stmt)

    async def candidates_by_activity(
        self, *, min_per_day: Decimal, max_per_day: Decimal, limit: int, seed: int = 0
    ) -> list[str]:
        """Candidatas con el filtro de actividad dentro de la banda, aún sin historial.

        Se eligen al azar (con semilla) dentro de la banda, no por volumen ni por
        frecuencia: así la muestra no se inclina hacia nadie.
        """
        c = CandidateWallet
        per_day = cast(c.recent_transactions, Numeric) / func.nullif(c.activity_days, 0)
        stmt = (
            select(c.wallet)
            .where(
                c.screened_at.is_not(None),
                c.activity_capped.is_(False),
                per_day >= min_per_day,
                per_day <= max_per_day,
                c.wallet.not_in(select(HistoryImport.wallet)),
            )
            .order_by(func.md5(func.concat(c.wallet, str(seed))))
            .limit(limit)
        )
        async with self._sessions() as s:
            return list(await s.scalars(stmt))

    async def candidates_from(self, source: str, *, include_imported: bool = False) -> list[str]:
        stmt = select(CandidateWallet.wallet).where(CandidateWallet.source == source)
        if not include_imported:
            stmt = stmt.where(CandidateWallet.wallet.not_in(select(HistoryImport.wallet)))
        async with self._sessions() as s:
            return list(await s.scalars(stmt.order_by(CandidateWallet.wallet)))

    async def candidate_info(self, wallets: Iterable[str]) -> dict[str, CandidateWallet]:
        wallets = list(wallets)
        if not wallets:
            return {}
        async with self._sessions() as s:
            rows = await s.scalars(
                select(CandidateWallet).where(CandidateWallet.wallet.in_(wallets))
            )
            return {r.wallet: r for r in rows}

    # --- Tokens -------------------------------------------------------------------

    async def token_creation_times(self, mints: Iterable[str]) -> dict[str, datetime | None]:
        """Fecha de creación de los tokens ya consultados (``None``: no se encontró)."""
        mints = list(set(mints))
        if not mints:
            return {}
        async with self._sessions() as s:
            rows = await s.execute(
                select(Token.mint, Token.created_at).where(
                    Token.mint.in_(mints), Token.creation_checked_at.is_not(None)
                )
            )
            return {mint: created for mint, created in rows.all()}

    async def save_token_creation(
        self,
        mint: str,
        *,
        created_slot: int | None,
        created_at: datetime | None,
        signature: str | None,
        checked_at: datetime,
    ) -> None:
        async with self._sessions.begin() as s:
            stmt = insert(Token).values(
                mint=mint,
                created_slot=created_slot,
                created_at=created_at,
                creation_signature=signature,
                creation_checked_at=checked_at,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["mint"],
                set_={
                    "created_slot": stmt.excluded.created_slot,
                    "created_at": stmt.excluded.created_at,
                    "creation_signature": stmt.excluded.creation_signature,
                    "creation_checked_at": stmt.excluded.creation_checked_at,
                },
            )
            await s.execute(stmt)

    # --- Análisis -------------------------------------------------------------------

    async def save_metric_snapshot(
        self,
        *,
        wallet: str,
        as_of: datetime,
        window_days: int,
        analyzer_version: str,
        params_hash: str,
        metrics: dict[str, Any],
        computed_at: datetime,
    ) -> bool:
        """Los snapshots son inmutables: si ya existe uno igual (misma wallet, as_of,
        ventana y parámetros), se conserva el primero y se devuelve ``False``."""
        async with self._sessions.begin() as s:
            result = await s.execute(
                insert(WalletMetricSnapshot)
                .values(
                    wallet=wallet,
                    as_of=as_of,
                    window_days=window_days,
                    analyzer_version=analyzer_version,
                    params_hash=params_hash,
                    metrics=metrics,
                    computed_at=computed_at,
                )
                .on_conflict_do_nothing(constraint="uq_metric_snapshot")
                .returning(WalletMetricSnapshot.id)
            )
            return result.scalar_one_or_none() is not None

    async def save_score(
        self,
        *,
        wallet: str,
        as_of: datetime,
        scoring_version: str,
        params_hash: str,
        eligible: bool,
        score: Decimal | None,
        components: dict[str, Any],
        flags: list[dict[str, Any]],
        explanation: list[str],
        computed_at: datetime,
    ) -> bool:
        async with self._sessions.begin() as s:
            result = await s.execute(
                insert(WalletScore)
                .values(
                    wallet=wallet,
                    as_of=as_of,
                    scoring_version=scoring_version,
                    params_hash=params_hash,
                    eligible=eligible,
                    score=score,
                    components=components,
                    flags=flags,
                    explanation=explanation,
                    computed_at=computed_at,
                )
                .on_conflict_do_nothing(constraint="uq_wallet_score")
                .returning(WalletScore.id)
            )
            return result.scalar_one_or_none() is not None

    async def latest_scores(self, *, params_hash: str | None = None) -> list[WalletScore]:
        """La puntuación más reciente (por ``as_of``) de cada wallet."""
        latest = select(WalletScore.wallet, func.max(WalletScore.as_of).label("as_of")).group_by(
            WalletScore.wallet
        )
        if params_hash is not None:
            latest = latest.where(WalletScore.params_hash == params_hash)
        sub = latest.subquery()
        stmt = select(WalletScore).join(
            sub, (WalletScore.wallet == sub.c.wallet) & (WalletScore.as_of == sub.c.as_of)
        )
        if params_hash is not None:
            stmt = stmt.where(WalletScore.params_hash == params_hash)
        stmt = stmt.order_by(
            WalletScore.eligible.desc(), WalletScore.score.desc().nulls_last(), WalletScore.wallet
        )
        async with self._sessions() as s:
            return list(await s.scalars(stmt))

    async def metric_snapshot(
        self, wallet: str, as_of: datetime, window_days: int, params_hash: str
    ) -> WalletMetricSnapshot | None:
        async with self._sessions() as s:
            return await s.scalar(
                select(WalletMetricSnapshot).where(
                    WalletMetricSnapshot.wallet == wallet,
                    WalletMetricSnapshot.as_of == as_of,
                    WalletMetricSnapshot.window_days == window_days,
                    WalletMetricSnapshot.params_hash == params_hash,
                )
            )

    async def events_for_wallet(self, wallet: str) -> list[WalletEventRow]:
        async with self._sessions() as s:
            rows = await s.scalars(
                select(WalletEventRow)
                .where(WalletEventRow.wallet == wallet)
                .order_by(WalletEventRow.slot, WalletEventRow.id)
            )
            return list(rows)

    # --- Operación ----------------------------------------------------------------

    async def record_usage(self, provider: str, method: str, requests: int, credits: int) -> None:
        async with self._sessions.begin() as s:
            stmt = insert(ProviderUsage).values(
                day=datetime.now(UTC).date(),
                provider=provider,
                method=method,
                requests=requests,
                credits=credits,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["day", "provider", "method"],
                set_={
                    "requests": ProviderUsage.requests + stmt.excluded.requests,
                    "credits": ProviderUsage.credits + stmt.excluded.credits,
                },
            )
            await s.execute(stmt)

    async def monthly_credits(self, provider: str, now: datetime) -> int:
        """Créditos consumidos este mes natural (UTC) con ``provider``."""
        month_start = now.astimezone(UTC).date().replace(day=1)
        async with self._sessions() as s:
            total = await s.scalar(
                select(func.coalesce(func.sum(ProviderUsage.credits), 0)).where(
                    ProviderUsage.provider == provider, ProviderUsage.day >= month_start
                )
            )
            return int(total or 0)

    async def heartbeat(self, service: str, details: dict[str, Any]) -> None:
        async with self._sessions.begin() as s:
            stmt = insert(ServiceHeartbeat).values(
                service=service, beat_at=datetime.now(UTC), details=details
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["service"],
                set_={"beat_at": stmt.excluded.beat_at, "details": stmt.excluded.details},
            )
            await s.execute(stmt)


def _chunks(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]

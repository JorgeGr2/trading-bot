"""Operaciones de base de datos idempotentes usadas por la ingesta."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.db.models import (
    IngestCheckpoint,
    ProviderUsage,
    RawTransaction,
    ServiceHeartbeat,
    TrackedWallet,
)
from copybot.db.models import (
    WalletEvent as WalletEventRow,
)
from copybot.solana.addresses import validate_address
from copybot.solana.classifier import TxSummary, WalletEvent

CLASSIFIER_VERSION = "1"


def _ts(epoch: int | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, tz=UTC) if epoch is not None else None


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
        async with self._sessions() as s:
            found = await s.scalar(
                select(RawTransaction.signature).where(RawTransaction.signature == signature)
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
    ) -> bool:
        """Guarda la transacción y sus eventos de forma atómica.

        Devuelve ``False`` si la transacción ya existía (en ese caso no se toca nada:
        la primera detección es la que cuenta).
        """
        latency_ms = None
        if summary.block_time is not None and detected_via == "live":
            latency_ms = int((first_seen_at.timestamp() - summary.block_time) * 1000)
        async with self._sessions.begin() as s:
            inserted = await s.execute(
                insert(RawTransaction)
                .values(
                    signature=summary.signature,
                    slot=summary.slot,
                    block_time=_ts(summary.block_time),
                    fee_payer=summary.fee_payer,
                    via_fomo=summary.via_fomo,
                    succeeded=summary.succeeded,
                    source=source,
                    detected_via=detected_via,
                    first_seen_at=first_seen_at,
                    detection_latency_ms=latency_ms,
                    payload=tx,
                )
                .on_conflict_do_nothing(index_elements=["signature"])
                .returning(RawTransaction.signature)
            )
            if inserted.scalar_one_or_none() is None:
                return False
            rows = [
                {
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
                    "detected_via": detected_via,
                    "detected_at": first_seen_at,
                    "classifier_version": CLASSIFIER_VERSION,
                }
                for ev in events
            ]
            if rows:
                await s.execute(
                    insert(WalletEventRow)
                    .values(rows)
                    .on_conflict_do_nothing(constraint="uq_wallet_events_signature_wallet")
                )
            return True

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

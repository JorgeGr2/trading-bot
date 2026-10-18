"""Importación de historiales (contra PostgreSQL real)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from copybot.db.models import RawTransaction, WalletEvent
from copybot.db.repo import HistoryItem
from copybot.db.session import make_sessionmaker
from copybot.ingest.history import HistoryImporter
from copybot.solana.classifier import classify, summarize
from copybot.solana.rpc import RpcError
from tests.fakes import RELAYER, FakeHistoryRpc, fomo_like_swap

pytestmark = pytest.mark.postgres

W = "7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm"
RELAYERS = frozenset({RELAYER})
NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def swap(i: int, when: datetime, buy: bool = True):
    return fomo_like_swap(
        f"sig{i:03d}",
        1000 + i,
        W,
        token_raw_delta=1_000_000 if buy else -1_000_000,
        usdc_raw_delta=-10_000_000 if buy else 11_000_000,
        block_time=int(when.timestamp()),
    )


def history_rpc(n: int = 5) -> FakeHistoryRpc:
    rpc = FakeHistoryRpc()
    for i in range(n):
        rpc.add(W, swap(i, NOW - timedelta(days=n - i), buy=i % 2 == 0), tx_index=i)
    return rpc


def importer(rpc, repo, clock, **kw):
    return HistoryImporter(
        rpc, repo, relayers=RELAYERS, source="helius", page_limit=2, clock=clock, **kw
    )


async def rows(engine, model):
    async with make_sessionmaker(engine)() as s:
        return list(await s.scalars(select(model)))


async def test_full_import_paginates_and_records_coverage(repo, engine):
    rpc = history_rpc()
    result = await importer(rpc, repo, Clock(NOW)).import_wallet(W, days=37)
    assert (result.pages, result.transactions, result.new_events) == (3, 5, 5)
    assert not result.truncated
    assert result.credits == 300
    to_time = NOW - timedelta(seconds=60)
    imported = await repo.get_history_import(W)
    assert (imported.covered_from, imported.covered_to) == (to_time - timedelta(days=37), to_time)
    raw = await rows(engine, RawTransaction)
    assert all(r.payload is None and r.detected_via == "history" for r in raw)
    events = await rows(engine, WalletEvent)
    assert sorted(e.tx_index for e in events) == [0, 1, 2, 3, 4]
    assert {e.detected_via for e in events} == {"history"}
    assert {e.kind for e in events} == {"buy", "sell"}


async def test_second_import_only_fetches_new_transactions(repo, engine):
    rpc = history_rpc()
    clock = Clock(NOW)
    await importer(rpc, repo, clock).import_wallet(W, days=37)
    first_to = (await repo.get_history_import(W)).covered_to
    rpc.add(W, swap(10, NOW + timedelta(minutes=30)), tx_index=10)
    clock.now = NOW + timedelta(hours=2)
    rpc.calls.clear()
    result = await importer(rpc, repo, clock).import_wallet(W, days=37)
    assert rpc.calls[0]["gte"] == int(first_to.timestamp())
    assert (result.transactions, result.new_events) == (1, 1)
    imported = await repo.get_history_import(W)
    assert imported.covered_from == NOW - timedelta(seconds=60) - timedelta(days=37)
    assert imported.covered_to == clock.now - timedelta(seconds=60)
    assert len(await rows(engine, WalletEvent)) == 6


async def test_truncated_import_keeps_the_most_recent_part(repo):
    rpc = history_rpc()
    result = await importer(rpc, repo, Clock(NOW), max_pages=1).import_wallet(W, days=37)
    assert result.truncated and result.transactions == 2
    # Página en orden descendente: las dos más recientes (hace 1 y 2 días).
    oldest = NOW - timedelta(days=2)
    assert result.covered_from == oldest + timedelta(seconds=1)
    assert (await repo.get_history_import(W)).truncated


async def test_interrupted_import_saves_no_coverage_and_resumes_without_duplicates(repo, engine):
    rpc = history_rpc()
    rpc.fail_after = 1
    with pytest.raises(RpcError):
        await importer(rpc, repo, Clock(NOW)).import_wallet(W, days=37)
    assert await repo.get_history_import(W) is None  # nada se da por cubierto
    assert len(await rows(engine, WalletEvent)) == 2  # la primera página sí quedó
    rpc.fail_after = None
    await importer(rpc, repo, Clock(NOW)).import_wallet(W, days=37)
    assert len(await rows(engine, WalletEvent)) == 5
    assert await repo.get_history_import(W) is not None


async def test_history_events_are_added_even_if_transaction_exists(repo):
    """Una transacción ya guardada por el historial de otra wallet recibe los eventos
    de la segunda wallet (p. ej. una transferencia entre dos wallets analizadas)."""
    tx = swap(1, NOW)
    summary = summarize(tx, RELAYERS)
    [event] = classify(tx, {W}, RELAYERS)
    other = event.__class__(**{**event.__dict__, "wallet": "OtherWallet" + "1" * 33})
    assert await repo.store_history_batch(
        [HistoryItem(summary, 3, [event])], source="helius", imported_at=NOW
    ) == (1, 1)
    assert await repo.store_history_batch(
        [HistoryItem(summary, 3, [event, other])], source="helius", imported_at=NOW
    ) == (0, 1)


async def test_live_monitor_completes_history_rows(repo, engine):
    tx = swap(1, NOW)
    summary = summarize(tx, RELAYERS)
    events = classify(tx, {W}, RELAYERS)
    await repo.store_history_batch(
        [HistoryItem(summary, 0, events)], source="helius", imported_at=NOW
    )
    # Sin JSON, el monitor no la da por procesada…
    assert not await repo.has_transaction(summary.signature)
    # …y al guardarla completa el JSON sin duplicar eventos ni cambiar su origen.
    assert await repo.store_transaction(
        tx, summary, events, source="helius", detected_via="backfill", first_seen_at=NOW
    )
    assert await repo.has_transaction(summary.signature)
    [raw] = await rows(engine, RawTransaction)
    assert raw.payload is not None and raw.detected_via == "history"
    [event] = await rows(engine, WalletEvent)
    assert event.detected_via == "history"
    # Una segunda vez ya no tiene efecto.
    assert not await repo.store_transaction(
        tx, summary, events, source="helius", detected_via="live", first_seen_at=NOW
    )

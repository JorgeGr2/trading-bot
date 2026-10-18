"""Monitor de wallets contra PostgreSQL real y un RPC en memoria (datos sintéticos)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from copybot.ingest.wallets import WalletMonitor
from copybot.solana.ws import Notification, Reconnected
from tests.fakes import RELAYER, FakeRpc, fomo_like_swap

pytestmark = pytest.mark.postgres

W = "7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm"
OTHER = "Gvg5vebyNUYgDP6U5jJLS3wAgbkhgJ19xHVpBLHtmGSy"


async def no_sleep(_: float) -> None:
    return None


def make_monitor(rpc: FakeRpc, repo, **kwargs) -> WalletMonitor:
    return WalletMonitor(
        rpc, repo, relayers=frozenset({RELAYER}), source="fake", sleep=no_sleep, **kwargs
    )


def add_buy(rpc: FakeRpc, sig: str, slot: int, wallet: str = W) -> str:
    return rpc.add(
        fomo_like_swap(sig, slot, wallet, token_raw_delta=5_000_000, usdc_raw_delta=-1_000_000),
        [wallet, RELAYER],
    )


def live(sig: str, received: datetime, failed: bool = False) -> Notification:
    return Notification(address=W, signature=sig, slot=None, failed=failed, received_at=received)


async def test_first_run_starts_from_now_without_backfill(repo):
    rpc = FakeRpc()
    for i in range(3):
        add_buy(rpc, f"old{i}", 100 + i)
    await repo.add_wallet(W, source="manual")
    monitor = make_monitor(rpc, repo)

    assert await monitor.reconcile(W) == 0
    checkpoint = await repo.get_checkpoint(W)
    assert checkpoint.last_signature == "old2"
    assert rpc.count("getTransaction") == 0


async def test_reconcile_processes_in_chronological_order(repo):
    rpc = FakeRpc()
    add_buy(rpc, "old", 100)
    await repo.add_wallet(W, source="manual")
    monitor = make_monitor(rpc, repo)
    await monitor.reconcile(W)

    for i, slot in enumerate((101, 102, 103)):
        add_buy(rpc, f"new{i}", slot)
    assert await monitor.reconcile(W) == 3

    events = await repo.events_for_wallet(W)
    assert [e.signature for e in events] == ["new0", "new1", "new2"]
    assert all(e.detected_via == "backfill" for e in events)
    assert all(e.kind == "buy" and e.confidence == "high" for e in events)
    assert events[0].token_delta == Decimal("5")
    assert events[0].quote_delta == Decimal("-1")
    assert (await repo.get_checkpoint(W)).last_signature == "new2"


async def test_live_event_records_latency_and_is_not_duplicated(repo):
    rpc = FakeRpc()
    add_buy(rpc, "old", 100)
    await repo.add_wallet(W, source="manual")
    monitor = make_monitor(rpc, repo)
    await monitor.reconcile(W)

    sig = add_buy(rpc, "a", 200)
    block_time = rpc.transactions[sig]["blockTime"]
    received = datetime.fromtimestamp(block_time + 1.25, tz=UTC)
    assert await monitor.handle_live(live(sig, received))
    # Notificación repetida (p. ej. tras reconectar): no tiene efecto.
    assert not await monitor.handle_live(live(sig, received))
    # La recuperación posterior tampoco la duplica ni cambia su origen.
    assert await monitor.reconcile(W) == 0

    [event] = await repo.events_for_wallet(W)
    assert event.detected_via == "live"
    assert monitor.stats["duplicates"] >= 2


async def test_failed_transactions_are_ignored(repo):
    rpc = FakeRpc()
    add_buy(rpc, "old", 100)
    await repo.add_wallet(W, source="manual")
    monitor = make_monitor(rpc, repo)
    await monitor.reconcile(W)
    rpc.add(
        fomo_like_swap("bad", 101, W, token_raw_delta=1, usdc_raw_delta=-1, err={"x": 1}),
        [W],
    )
    now = datetime.now(UTC)
    assert not await monitor.handle_live(live("bad", now, failed=True))
    assert await monitor.reconcile(W) == 0
    assert rpc.count("getTransaction") == 0
    assert (await repo.get_checkpoint(W)).last_signature == "bad"


async def test_gap_during_disconnection_is_recovered(repo):
    """Se pierde la conexión: las operaciones de ese intervalo se recuperan al volver."""
    rpc = FakeRpc()
    add_buy(rpc, "old", 100)
    await repo.add_wallet(W, source="manual")
    monitor = make_monitor(rpc, repo)
    t0 = datetime.now(UTC)

    a = add_buy(rpc, "a", 101)

    async def stream():
        yield Reconnected(0)
        yield live(a, t0)
        # Conexión caída: "b" y "c" ocurren sin notificación.
        add_buy(rpc, "b", 102)
        add_buy(rpc, "c", 103)
        yield Reconnected(1)
        d = add_buy(rpc, "d", 104)
        yield live(d, t0)

    await monitor.run(stream())
    events = {e.signature: e.detected_via for e in await repo.events_for_wallet(W)}
    assert events == {"a": "live", "b": "backfill", "c": "backfill", "d": "live"}


async def test_restart_resumes_from_checkpoint(repo):
    rpc = FakeRpc()
    add_buy(rpc, "old", 100)
    await repo.add_wallet(W, source="manual")
    await make_monitor(rpc, repo).reconcile(W)
    add_buy(rpc, "a", 101)
    await make_monitor(rpc, repo).reconcile(W)

    # El proceso se reinicia: una instancia nueva solo procesa lo que falta.
    add_buy(rpc, "b", 102)
    rpc.calls.clear()
    assert await make_monitor(rpc, repo).reconcile(W) == 1
    assert rpc.count("getTransaction") == 1


async def test_unavailable_transaction_does_not_advance_checkpoint(repo):
    rpc = FakeRpc()
    add_buy(rpc, "old", 100)
    await repo.add_wallet(W, source="manual")
    monitor = make_monitor(rpc, repo, fetch_attempts=2)
    await monitor.reconcile(W)

    add_buy(rpc, "a", 101)
    b = add_buy(rpc, "b", 102)
    rpc.unavailable.add(b)
    assert await monitor.reconcile_all() == 0
    assert (await repo.get_checkpoint(W)).last_signature == "old"
    assert [e.signature for e in await repo.events_for_wallet(W)] == ["a"]

    rpc.unavailable.clear()
    assert await monitor.reconcile_all() == 1
    assert (await repo.get_checkpoint(W)).last_signature == "b"


async def test_long_gap_is_paginated(repo):
    rpc = FakeRpc()
    add_buy(rpc, "old", 100)
    await repo.add_wallet(W, source="manual")
    monitor = make_monitor(rpc, repo, page_limit=3, max_pages=10)
    await monitor.reconcile(W)
    for i in range(8):
        add_buy(rpc, f"n{i}", 101 + i)
    assert await monitor.reconcile(W) == 8
    assert monitor.stats["gaps"] == 0


async def test_gap_larger_than_limit_is_reported(repo):
    rpc = FakeRpc()
    add_buy(rpc, "old", 100)
    await repo.add_wallet(W, source="manual")
    monitor = make_monitor(rpc, repo, page_limit=2, max_pages=2)
    await monitor.reconcile(W)
    for i in range(7):
        add_buy(rpc, f"n{i}", 101 + i)
    assert await monitor.reconcile(W) == 4
    assert monitor.stats["gaps"] == 1
    assert (await repo.get_checkpoint(W)).last_signature == "n6"


async def test_only_tracked_wallets_get_events(repo):
    rpc = FakeRpc()
    add_buy(rpc, "old", 100)
    await repo.add_wallet(W, source="manual")
    monitor = make_monitor(rpc, repo)
    await monitor.reconcile(W)
    # Transacción de otra wallet que menciona a W de algún modo.
    rpc.add(
        fomo_like_swap("x", 101, OTHER, token_raw_delta=1_000_000, usdc_raw_delta=-500_000),
        [W],
    )
    assert await monitor.reconcile(W) == 1
    assert await repo.events_for_wallet(W) == []
    assert await repo.events_for_wallet(OTHER) == []
    assert await repo.has_transaction("x")


async def test_provider_failure_does_not_stop_the_monitor(repo):
    """El proveedor rechaza peticiones (429) durante un rato: el monitor sigue vivo y
    recupera después lo que no pudo descargar."""
    rpc = FakeRpc()
    add_buy(rpc, "old", 100)
    await repo.add_wallet(W, source="manual")
    monitor = make_monitor(rpc, repo)
    t0 = datetime.now(UTC)

    async def stream():
        yield Reconnected(0)  # primer arranque: el punto de partida es "old"
        a = add_buy(rpc, "a", 101)
        rpc.failing.add(a)
        yield live(a, t0)  # falla: 429
        b = add_buy(rpc, "b", 102)
        yield live(b, t0)  # sigue funcionando
        rpc.failing.clear()
        yield Reconnected(1)  # la recuperación recoge "a"

    await monitor.run(stream())
    events = {e.signature: e.detected_via for e in await repo.events_for_wallet(W)}
    assert events == {"a": "backfill", "b": "live"}
    assert monitor.stats["rpc_errors"] == 1


async def test_reconcile_survives_provider_errors_per_wallet(repo):
    rpc = FakeRpc()
    add_buy(rpc, "old", 100)
    add_buy(rpc, "oldo", 100, wallet=OTHER)
    await repo.add_wallet(W, source="manual")
    await repo.add_wallet(OTHER, source="manual")
    monitor = make_monitor(rpc, repo)
    await monitor.reconcile_all()
    bad = add_buy(rpc, "bad", 101)
    add_buy(rpc, "good", 101, wallet=OTHER)
    rpc.failing.add(bad)
    assert await monitor.reconcile_all() == 1  # la otra wallet sí avanza
    assert (await repo.get_checkpoint(W)).last_signature == "old"
    assert (await repo.get_checkpoint(OTHER)).last_signature == "good"

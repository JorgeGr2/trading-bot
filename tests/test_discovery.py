"""Muestreo del relayer, candidatos y fechas de creación de tokens (PostgreSQL real)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from copybot.ingest.discovery import RelayerSampler, TokenCreationLookup
from tests.fakes import MEME, RELAYER, FakeHistoryRpc, fomo_like_swap

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
A = "7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm"
B = "Gvg5vebyNUYgDP6U5jJLS3wAgbkhgJ19xHVpBLHtmGSy"


def relayer_rpc() -> FakeHistoryRpc:
    rpc = FakeHistoryRpc()
    trades = [(A, 4), (A, 3), (B, 2), (A, 1)]  # en orden cronológico (slots crecientes)
    for i, (wallet, hours_ago) in enumerate(trades):
        when = NOW - timedelta(hours=hours_ago)
        rpc.add(
            RELAYER,
            fomo_like_swap(
                f"r{i}",
                100 + i,
                wallet,
                token_raw_delta=5_000_000,
                usdc_raw_delta=-2_500_000,
                block_time=int(when.timestamp()),
            ),
        )
    return rpc


def sampler(rpc, repo, per_window=2):
    return RelayerSampler(
        rpc,
        repo,
        relayer=RELAYER,
        relayers=frozenset({RELAYER}),
        per_window=per_window,
        clock=lambda: NOW,
    )


async def test_windows_are_reproducible():
    s = sampler(FakeHistoryRpc(), None)
    first = s.window_starts(5, days=7, seed=42)
    assert first == s.window_starts(5, days=7, seed=42)
    assert first != s.window_starts(5, days=7, seed=43)
    assert all(NOW - timedelta(days=7, minutes=10) <= t <= NOW for t in first)


async def test_sampling_builds_candidates_without_double_counting(repo):
    rpc = relayer_rpc()
    s = sampler(rpc, repo)
    w1 = await s.sample_window(NOW - timedelta(hours=5))  # A (4 h) y A (3 h)
    w2 = await s.sample_window(NOW - timedelta(hours=2, minutes=30))  # B (2 h) y A (1 h)
    again = await s.sample_window(NOW - timedelta(hours=5))  # las mismas que w1
    assert (w1.trades, w2.trades, again.new_observations) == (2, 2, 0)
    assert w1.credits == 100
    candidates = await repo.discovery_candidates(limit=10, min_trades=2)
    assert [(c.wallet, c.windows, c.trades, c.buys) for c in candidates] == [(A, 2, 3, 3)]
    assert candidates[0].volume_usd == 7.5
    everyone = await repo.discovery_candidates(limit=10, min_trades=1)
    assert [c.wallet for c in everyone] == [A, B]


async def test_imported_wallets_are_not_candidates_again(repo):
    s = sampler(relayer_rpc(), repo, per_window=10)
    await s.sample_window(NOW - timedelta(days=1))
    await repo.save_history_import(
        A,
        covered_from=NOW - timedelta(days=37),
        covered_to=NOW,
        truncated=False,
        transactions=4,
        credits=100,
    )
    assert [c.wallet for c in await repo.discovery_candidates(limit=10, min_trades=1)] == [B]


async def test_token_creation_lookup_is_cached_and_bounded(repo):
    rpc = FakeHistoryRpc()
    created = NOW - timedelta(days=3)
    mints = [MEME, "M2" + "1" * 42, "M3" + "1" * 42]
    for mint in mints[:2]:
        for k, when in enumerate((created + timedelta(minutes=5), created)):
            rpc.add(
                mint,
                fomo_like_swap(
                    f"{mint[:4]}{k}",
                    10 + (1 - k),
                    A,
                    token_raw_delta=1,
                    usdc_raw_delta=-1,
                    block_time=int(when.timestamp()),
                ),
            )
    lookup = TokenCreationLookup(rpc, repo, clock=lambda: NOW)
    assert await lookup.ensure(mints, max_lookups=2) == 2
    assert await lookup.ensure(mints, max_lookups=2) == 1
    assert await lookup.ensure(mints, max_lookups=2) == 0  # nunca se repite una consulta
    times = await repo.token_creation_times(mints)
    assert times[MEME] == created
    assert times[mints[2]] is None  # consultado, sin transacciones
    assert rpc.calls[0]["order"] == "asc"

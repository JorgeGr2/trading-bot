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


# --- Wallets poco activas y alta manual -------------------------------------------


class SignaturesRpc:
    def __init__(self, by_wallet):
        self.by_wallet = by_wallet

    async def get_signatures_for_address(self, address, *, limit=1000, **_):
        return self.by_wallet.get(address, [])[:limit]


def _sigs(n: int, every_hours: float, failed_every: int = 0):
    return [
        {
            "signature": f"s{i}",
            "blockTime": int((NOW - timedelta(hours=every_hours * i)).timestamp()),
            "err": {"x": 1} if failed_every and i % failed_every == 0 else None,
        }
        for i in range(n)
    ]


async def test_activity_screening_counts_recent_successful_transactions(repo):
    from copybot.ingest.discovery import ActivityScreener

    rpc = SignaturesRpc(
        {
            # 1 al día durante 100 días: hoy y los 37 días anteriores (límite incluido) → 38
            A: _sigs(100, every_hours=24),
            B: _sigs(1000, every_hours=0.1),  # 1.000 en 4 días: la consulta se queda corta
        }
    )
    screener = ActivityScreener(rpc, repo, days=37, clock=lambda: NOW)
    low = await screener.screen(A)
    high = await screener.screen(B)
    assert (low.transactions, low.capped) == (38, False)
    assert high.capped
    failed = await ActivityScreener(
        SignaturesRpc({A: _sigs(10, 24, failed_every=2)}), repo, clock=lambda: NOW
    ).screen(A)
    assert failed.transactions == 5  # las fallidas no cuentan


async def test_activity_band_selection(repo):
    from decimal import Decimal

    for wallet, txs, capped in [(A, 37, False), (B, 600, False), (MEME, 1000, True)]:
        await repo.save_screening(wallet, days=37, transactions=txs, capped=capped, screened_at=NOW)
    pick = await repo.candidates_by_activity(
        min_per_day=Decimal("0.7"), max_per_day=Decimal(15), limit=10
    )
    assert pick == [A]  # 1/día; B hace 16,2/día; MEME supera el máximo de la consulta
    await repo.save_history_import(
        A,
        covered_from=NOW - timedelta(days=37),
        covered_to=NOW,
        truncated=False,
        transactions=37,
        credits=100,
    )
    assert (
        await repo.candidates_by_activity(
            min_per_day=Decimal("0.7"), max_per_day=Decimal(15), limit=10
        )
        == []
    )


async def test_screening_queue_skips_screened_and_imported(repo):
    s = sampler(relayer_rpc(), repo, per_window=10)
    await s.sample_window(NOW - timedelta(days=1))
    assert sorted(await repo.wallets_to_screen(limit=10)) == sorted([A, B])
    await repo.save_screening(A, days=37, transactions=3, capped=False, screened_at=NOW)
    assert await repo.wallets_to_screen(limit=10) == [B]
    assert await repo.wallets_to_screen(limit=10, max_windows=0) == []


async def test_manual_candidates_keep_the_most_specific_source(repo):
    assert await repo.add_candidate(A, source="leaderboard", label="@uno, puesto 1")
    assert not await repo.add_candidate(A, source="manual")  # ya existía
    await repo.save_screening(A, days=37, transactions=3, capped=False, screened_at=NOW)
    info = (await repo.candidate_info([A]))[A]
    assert (info.source, info.label, info.recent_transactions) == (
        "leaderboard",
        "@uno, puesto 1",
        3,
    )
    await repo.save_screening(B, days=37, transactions=3, capped=False, screened_at=NOW)
    await repo.add_candidate(B, source="manual")
    assert (await repo.candidate_info([B]))[B].source == "manual"  # «relayer» se mejora
    assert await repo.candidates_from("leaderboard") == [A]

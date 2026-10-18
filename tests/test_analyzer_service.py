"""Analizador de extremo a extremo: historial importado → métricas → puntuación guardada."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from copybot.analysis.metrics import AnalysisParams
from copybot.analysis.scoring import ScoringParams
from copybot.analysis.service import TraderAnalyzer
from copybot.db.models import WalletMetricSnapshot, WalletScore
from copybot.db.repo import HistoryItem
from copybot.db.session import make_sessionmaker
from copybot.ingest.history import HistoryImporter
from copybot.solana.classifier import classify, summarize
from tests.fakes import RELAYER, FakeHistoryRpc, fomo_like_swap

pytestmark = pytest.mark.postgres

W = "7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm"
RELAYERS = frozenset({RELAYER})
NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
AS_OF = NOW - timedelta(hours=1)


def mint(i: int) -> str:
    return f"Mint{i:04d}".ljust(40, "1") + "pump"


def round_trips(n: int) -> list[dict]:
    """``n`` operaciones: compra 10 $ y, 10 minutos después, vende por 11 $ (+10 %)."""
    txs = []
    for i in range(n):
        opened = NOW - timedelta(days=28) + timedelta(hours=20 * i)
        for k, (delta_token, delta_usdc, when) in enumerate(
            [
                (1_000_000, -10_000_000, opened),
                (-1_000_000, 11_000_000, opened + timedelta(minutes=10)),
            ]
        ):
            txs.append(
                fomo_like_swap(
                    f"t{i:03d}{k}",
                    10_000 + 2 * i + k,
                    W,
                    token_raw_delta=delta_token,
                    usdc_raw_delta=delta_usdc,
                    mint=mint(i),
                    block_time=int(when.timestamp()),
                )
            )
    return txs


async def prepared(repo, n: int = 25) -> TraderAnalyzer:
    rpc = FakeHistoryRpc()
    for i, tx in enumerate(round_trips(n)):
        rpc.add(W, tx, tx_index=i)
    await HistoryImporter(
        rpc, repo, relayers=RELAYERS, source="helius", clock=lambda: NOW
    ).import_wallet(W, days=37)
    return TraderAnalyzer(
        repo,
        analysis=AnalysisParams(_env_file=None),
        scoring=ScoringParams(_env_file=None),
        clock=lambda: NOW,
    )


async def test_end_to_end_analysis_is_saved_and_explained(repo, engine):
    analyzer = await prepared(repo)
    result = await analyzer.analyze(W, AS_OF)
    m = result.main
    assert m.coverage_complete
    assert m.round_trips == 25 and m.wins == 25
    assert m.mean_return == Decimal("0.1")
    assert m.median_seconds_to_first_sell == 600
    assert result.recent.window_days == 7
    assert result.score.eligible and result.saved
    # Sin desviación (todas +10 %): ventaja = (0,10 − 0,03) / 0,10 = 0,7
    assert result.score.components["ventaja"]["valor"] == Decimal("0.7")
    assert "sniping_sin_comprobar" in {f.code for f in result.score.flags}
    async with make_sessionmaker(engine)() as s:
        assert await s.scalar(select(func.count()).select_from(WalletMetricSnapshot)) == 2
        stored = await s.scalar(select(WalletScore))
    assert stored.score == result.score.score
    assert stored.params_hash == analyzer.score_hash
    assert stored.explanation[0].startswith("Puntuación ")
    [latest] = await repo.latest_scores()
    assert latest.wallet == W


async def test_snapshots_are_immutable_and_ignore_later_events(repo):
    analyzer = await prepared(repo)
    first = await analyzer.analyze(W, AS_OF)
    # Llega más historial, posterior a as_of: no debe cambiar el análisis a esa fecha.
    late = fomo_like_swap(
        "late0",
        99_999,
        W,
        token_raw_delta=-1,
        usdc_raw_delta=10**9,
        mint=mint(0),
        block_time=int((AS_OF + timedelta(minutes=5)).timestamp()),
    )
    await repo.store_history_batch(
        [HistoryItem(summarize(late, RELAYERS), 0, classify(late, {W}, RELAYERS))],
        source="helius",
        imported_at=NOW,
    )
    again = await analyzer.analyze(W, AS_OF)
    assert not again.saved  # ya existía: se conserva el primero
    assert again.main.to_json() == first.main.to_json()
    assert again.score.explanation() == first.score.explanation()
    stored = await repo.metric_snapshot(W, AS_OF, 30, analyzer.metrics_hash)
    assert stored.metrics == first.main.to_json()


async def test_small_sample_is_excluded_with_reason(repo):
    analyzer = await prepared(repo, n=5)
    result = await analyzer.analyze(W, AS_OF)
    assert not result.score.eligible
    assert any("muestra insuficiente" in r for r in result.score.exclusions)


async def test_wallet_without_imported_history_is_not_scored(repo):
    analyzer = TraderAnalyzer(
        repo, analysis=AnalysisParams(_env_file=None), scoring=ScoringParams(_env_file=None)
    )
    result = await analyzer.analyze(W, AS_OF, save=False)
    assert not result.score.eligible
    assert not result.main.coverage_complete

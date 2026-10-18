"""Une la base de datos con el núcleo puro del analizador y guarda los resultados."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from copybot.analysis.metrics import (
    ANALYZER_VERSION,
    AnalysisParams,
    Coverage,
    WindowMetrics,
    compute_window_metrics,
)
from copybot.analysis.positions import USD_QUOTES, Trade, build_ledger
from copybot.analysis.scoring import (
    SCORING_VERSION,
    ScoreResult,
    ScoringParams,
    metrics_params_hash,
    score_params_hash,
    score_wallet,
)
from copybot.db.models import WalletEvent as WalletEventRow
from copybot.db.repo import Repository


def to_trade(row: WalletEventRow) -> Trade | None:
    """Convierte un evento guardado en la entrada del analizador."""
    if row.block_time is None:
        return None
    usd = abs(row.quote_delta) if row.quote_symbol in USD_QUOTES else None
    return Trade(
        signature=row.signature,
        slot=row.slot,
        tx_index=row.tx_index if row.tx_index is not None else 0,
        time=row.block_time,
        kind=row.kind,
        mint=row.mint,
        amount=abs(Decimal(row.token_delta)),
        usd=Decimal(usd) if usd is not None else None,
        confidence=row.confidence,
    )


@dataclass(frozen=True)
class WalletAnalysis:
    wallet: str
    as_of: datetime
    main: WindowMetrics
    recent: WindowMetrics
    score: ScoreResult
    saved: bool


class TraderAnalyzer:
    def __init__(
        self,
        repo: Repository,
        *,
        analysis: AnalysisParams,
        scoring: ScoringParams,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repo
        self._analysis = analysis
        self._scoring = scoring
        self._clock = clock
        self.metrics_hash = metrics_params_hash(analysis)
        self.score_hash = score_params_hash(analysis, scoring)

    @property
    def scoring(self) -> ScoringParams:
        return self._scoring

    async def load(self, wallet: str, as_of: datetime) -> tuple[list[Trade], Coverage | None]:
        rows = await self._repo.wallet_events_until(wallet, as_of)
        trades = [t for r in rows if (t := to_trade(r)) is not None]
        imported = await self._repo.get_history_import(wallet)
        coverage = (
            Coverage(imported.covered_from, imported.covered_to, imported.truncated)
            if imported is not None
            else None
        )
        return trades, coverage

    async def opened_mints(self, wallet: str, as_of: datetime) -> list[str]:
        """Tokens de las operaciones abiertas en la ventana principal (para el sniping)."""
        trades, _ = await self.load(wallet, as_of)
        start = as_of.timestamp() - self._scoring.window_days * 86400
        ledger = build_ledger(trades, as_of, self._analysis.close_tolerance)
        return sorted(
            {t.mint for t in ledger.round_trips if t.buys and t.opened_at.timestamp() > start}
        )

    async def analyze(self, wallet: str, as_of: datetime, *, save: bool = True) -> WalletAnalysis:
        trades, coverage = await self.load(wallet, as_of)
        created = await self._repo.token_creation_times({t.mint for t in trades if t.mint})
        ledger = build_ledger(trades, as_of, self._analysis.close_tolerance)
        windows = {}
        for days in sorted({self._scoring.window_days, self._scoring.recent_window_days}):
            windows[days] = compute_window_metrics(
                trades,
                as_of=as_of,
                window_days=days,
                params=self._analysis,
                coverage=coverage,
                token_created_at=created,
                ledger=ledger,
            )
        main = windows[self._scoring.window_days]
        recent = windows[self._scoring.recent_window_days]
        result = score_wallet(main, recent, self._scoring)
        saved = False
        if save:
            now = self._clock()
            for days, metrics in windows.items():
                await self._repo.save_metric_snapshot(
                    wallet=wallet,
                    as_of=as_of,
                    window_days=days,
                    analyzer_version=ANALYZER_VERSION,
                    params_hash=self.metrics_hash,
                    metrics=metrics.to_json(),
                    computed_at=now,
                )
            saved = await self._repo.save_score(
                wallet=wallet,
                as_of=as_of,
                scoring_version=SCORING_VERSION,
                params_hash=self.score_hash,
                eligible=result.eligible,
                score=result.score,
                components=result.components_json(),
                flags=[f.to_json() for f in result.flags],
                explanation=result.explanation(),
                computed_at=now,
            )
        return WalletAnalysis(wallet, as_of, main, recent, result, saved)

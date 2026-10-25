"""Análisis de un día: universo, puntuaciones, régimen y datos informativos de cada moneda.

Se guarda en ``crypto_analyses`` y ``crypto_analysis_rows``. Un análisis calculado en vivo
(la noche de ese día) no se reescribe nunca: es la prueba de lo que el bot dijo en su
momento. Los recalculados después se marcan como tales y no cuentan en «¿Acertó?».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.crypto import explain
from copybot.crypto.panel import Panel
from copybot.crypto.strategy import (
    STRATEGY_V1,
    STRATEGY_VERSION,
    DayView,
    StrategyParams,
    day_view,
)
from copybot.db.models import CryptoAnalysis, CryptoAnalysisRow

MIN_UNIVERSE = 20


@dataclass(frozen=True)
class CoinInfo:
    symbol: str
    base: str
    included: bool
    exclusion_reason: str | None
    rank: int | None
    score: float | None
    trend_share: float | None
    ret_7d: float | None
    ret_30d: float | None
    ret_90d: float | None
    vol_daily: float | None
    median_qv: float | None
    liquidity_rank: int | None
    best_day_share: float | None
    p5_week: float | None
    risk_level: str | None
    entry_eligible: bool
    momentum_pct: float | None
    above_sma: dict[int, bool]
    vs_btc_30d: float | None
    warnings: list[str]


@dataclass(frozen=True)
class Analysis:
    day: date
    view: DayView
    coins: dict[str, CoinInfo]
    pairs: int  # pares con vela ese día
    status: str  # ok, datos_incompletos

    @property
    def favorable(self) -> bool | None:
        return None if self.view.regime is None else self.view.regime.favorable

    @property
    def ranked(self) -> list[CoinInfo]:
        return sorted(
            (c for c in self.coins.values() if c.rank is not None), key=lambda c: c.rank or 0
        )


def analyze(panel: Panel, day: date, params: StrategyParams = STRATEGY_V1) -> Analysis:
    view = day_view(panel, day, params)
    members = {m.symbol: m for m in view.universe.members}
    ranked = view.by_symbol
    m30 = [s.momentum[30] for s in view.ranked if 30 in s.momentum]
    btc = ranked.get(params.market_symbol)
    favorable = view.regime is not None and view.regime.favorable
    coins = {}
    for symbol, series in panel.series.items():
        i = series.at(day)
        if i is None:
            continue
        closes = series.close[: i + 1]
        sig = ranked.get(symbol)
        member = members.get(symbol)
        episode = (day - series.days[series.episode_start(i)]).days + 1
        vol = sig.vol if sig else None
        share = explain.best_day_share(closes)
        p5 = explain.p5_week(closes)
        qv = member.median_quote_volume if member else None
        warnings = []
        warn = explain.single_day_warning(share)
        if warn:
            warnings.append(warn)
        coins[symbol] = CoinInfo(
            symbol=symbol,
            base=series.base,
            included=member is not None,
            exclusion_reason=view.universe.excluded.get(symbol),
            rank=sig.rank if sig else None,
            score=sig.score if sig else None,
            trend_share=sig.trend_share if sig else None,
            ret_7d=explain.ret(closes, 7),
            ret_30d=explain.ret(closes, 30),
            ret_90d=explain.ret(closes, 90),
            vol_daily=vol,
            median_qv=qv,
            liquidity_rank=member.liquidity_rank if member else None,
            best_day_share=share,
            p5_week=p5,
            risk_level=explain.risk_level(vol, episode, qv, share) if sig else None,
            entry_eligible=bool(
                sig
                and favorable
                and sig.rank <= params.entry_rank
                and sig.trend_share + 1e-9 >= params.entry_trend
            ),
            momentum_pct=(
                explain.momentum_percentile(sig.momentum[30], m30) if sig and m30 else None
            ),
            above_sma=dict(sig.above_sma) if sig else {},
            vs_btc_30d=(sig.momentum[30] - btc.momentum[30]) if sig and btc else None,
            warnings=warnings,
        )
    complete = view.regime is not None and len(view.universe.members) >= MIN_UNIVERSE
    return Analysis(day, view, coins, len(coins), "ok" if complete else "datos_incompletos")


def _dec(x: float | None, places: int = 10) -> Decimal | None:
    return None if x is None else Decimal(str(round(x, places)))


async def store(
    sessions: async_sessionmaker[AsyncSession],
    a: Analysis,
    *,
    live: bool,
    confidence: str | None = None,
    commit: str | None = None,
    report_md: str | None = None,
    params: StrategyParams = STRATEGY_V1,
) -> int:
    """Guarda el análisis. Si ya hay uno **en vivo** y completo de ese día, no se toca y se
    devuelve su id. Uno recalculado, o uno en vivo con datos incompletos (no decidió nada),
    se sustituye en el sitio: conserva su id, al que apuntan los días de la cartera."""
    reg = a.view.regime
    values = {
        "params_hash": params.params_hash(),
        "data_until": datetime(a.day.year, a.day.month, a.day.day, tzinfo=UTC) + timedelta(days=1),
        "computed_live": live,
        "regime": {}
        if reg is None
        else {"btc": reg.close, "sma100": reg.sma, "favorable": reg.favorable},
        "universe_size": len(a.view.universe.members),
        "status": a.status,
        "confidence": confidence,
        "code_commit": commit,
        "report_md": report_md,
        "report_json": None,
    }
    async with sessions.begin() as s:
        existing = await s.scalar(
            select(CryptoAnalysis).where(
                CryptoAnalysis.day == a.day, CryptoAnalysis.strategy_version == STRATEGY_VERSION
            )
        )
        if existing is not None and existing.computed_live and existing.status == "ok":
            return existing.id
        if existing is None:
            row = CryptoAnalysis(day=a.day, strategy_version=STRATEGY_VERSION, **values)
            s.add(row)
            await s.flush()
        else:
            row = existing
            for key, value in values.items():
                setattr(row, key, value)
            await s.execute(
                delete(CryptoAnalysisRow).where(CryptoAnalysisRow.analysis_id == row.id)
            )
        for c in a.coins.values():
            s.add(
                CryptoAnalysisRow(
                    analysis_id=row.id,
                    symbol=c.symbol,
                    included=c.included,
                    exclusion_reason=c.exclusion_reason,
                    liquidity_rank=c.liquidity_rank,
                    median_qv_30d=_dec(c.median_qv, 2),
                    rank=c.rank,
                    score=_dec(c.score, 6),
                    components=None
                    if c.rank is None
                    else {
                        "tendencia": c.trend_share,
                        "medias": {str(k): v for k, v in c.above_sma.items()},
                    },
                    ret_7d=_dec(c.ret_7d),
                    ret_30d=_dec(c.ret_30d),
                    ret_90d=_dec(c.ret_90d),
                    vol_daily=_dec(c.vol_daily),
                    best_day_share=_dec(c.best_day_share),
                    p5_week=_dec(c.p5_week),
                    risk_level=c.risk_level,
                    entry_eligible=c.entry_eligible,
                    warnings=c.warnings or None,
                )
            )
        return row.id


async def set_report(
    sessions: async_sessionmaker[AsyncSession], analysis_id: int, report_md: str, report_json
) -> None:
    async with sessions.begin() as s:
        row = await s.get(CryptoAnalysis, analysis_id)
        if row is not None and (row.report_md is None or not row.computed_live):
            row.report_md, row.report_json = report_md, report_json

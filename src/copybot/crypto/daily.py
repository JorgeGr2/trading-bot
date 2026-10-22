"""El paso diario del bot: descargar → analizar → cartera de prueba → informe.

Se ejecuta poco después de las 00:00 UTC, con la vela del día anterior ya cerrada. Si no
hay datos completos, ese día no se decide nada (solo se venden pares que dejaron de
cotizar). Los días que la máquina estuvo apagada no se rellenan con precios pasados.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.config import RiskLimits
from copybot.crypto import explain, report
from copybot.crypto.analysis import Analysis, analyze, set_report, store
from copybot.crypto.binance import BinanceMarket, BookTicker
from copybot.crypto.panel import load_panel
from copybot.crypto.paper import (
    PORTFOLIO,
    Filters,
    StepResult,
    active_runs,
    paper_step,
    start_run,
)
from copybot.crypto.registry import code_commit, prereg_check
from copybot.crypto.strategy import STRATEGY_VERSION
from copybot.crypto.sync import EXCHANGE, sync_all
from copybot.db.models import (
    CryptoAnalysis,
    CryptoAnalysisRow,
    CryptoBacktestRun,
    CryptoPaperBook,
    CryptoPaperPosition,
    CryptoPaperRun,
    CryptoPaperSnapshot,
    MarketSymbol,
    ServiceHeartbeat,
)

HISTORY_DAYS = 500  # velas que hacen falta para el análisis (medias, 365 días de «semana mala»)
SERVICE = "crypto_daily"
Say = Callable[[str], None]


@dataclass
class DailyResult:
    day: date
    analysis_id: int
    steps: list[StepResult]
    text: str


async def _filters(sessions: async_sessionmaker[AsyncSession]) -> dict[str, Filters]:
    async with sessions() as s:
        rows = await s.scalars(select(MarketSymbol).where(MarketSymbol.exchange == EXCHANGE))
        return {r.symbol: Filters(r.step_size, r.min_notional) for r in rows}


async def latest_backtest(sessions: async_sessionmaker[AsyncSession]) -> report.BacktestInfo:
    async with sessions() as s:
        row = await s.scalar(
            select(CryptoBacktestRun)
            .where(
                CryptoBacktestRun.strategy_version == STRATEGY_VERSION,
                CryptoBacktestRun.role == "principal",
            )
            .order_by(CryptoBacktestRun.id.desc())
            .limit(1)
        )
    if row is None:
        return report.BacktestInfo()
    m = row.metrics or {}
    ci = m.get("margen")
    return report.BacktestInfo(
        run_day=row.created_at.date(),
        excess_annual=m.get("exceso_anual"),
        ci=tuple(ci) if ci else None,
        null_percentile=m.get("nulo_percentil"),
        light=m.get("luz"),
        rows=[tuple(r) for r in m.get("periodos", [])],
    )


async def _paper_info(
    sessions: async_sessionmaker[AsyncSession], run_id: int, day: date, step: StepResult
) -> report.PaperInfo:
    async with sessions() as s:
        book = await s.get(CryptoPaperBook, (run_id, PORTFOLIO))
        snap = await s.get(CryptoPaperSnapshot, (run_id, PORTFOLIO, day))
        positions = list(
            await s.scalars(
                select(CryptoPaperPosition).where(
                    CryptoPaperPosition.run_id == run_id,
                    CryptoPaperPosition.book == PORTFOLIO,
                    CryptoPaperPosition.status == "open",
                )
            )
        )
        closed_today = {
            p.symbol: p
            for p in await s.scalars(
                select(CryptoPaperPosition).where(
                    CryptoPaperPosition.run_id == run_id,
                    CryptoPaperPosition.book == PORTFOLIO,
                    CryptoPaperPosition.closed_day == day,
                )
            )
        }
        run = await s.get(CryptoPaperRun, run_id)
    sells, buys, rejected = [], [], []
    for symbol, side, purpose, status, why in step.orders:
        if purpose in ("senal", "horizonte_7d"):
            continue
        base = symbol[:-4]
        if status != "filled":
            rejected.append((base, why))
        elif side == "sell":
            p = closed_today.get(symbol)
            pnl = None if p is None or p.realized_pnl_usd is None else float(p.realized_pnl_usd)
            pct = None if pnl is None or not p.cost_usd else pnl / float(p.cost_usd)
            sells.append((base, why.split(" · ")[0], pnl, pct))
        else:
            cost = next((float(p.cost_usd) for p in positions if p.symbol == symbol), 0.0)
            buys.append((base, cost))
    equity = float(snap.equity_usd) if snap else None
    peak = float(book.peak_equity_usd) if book else None
    return report.PaperInfo(
        holdings=[p.symbol[:-4] for p in positions],
        sells=sells,
        buys=buys,
        rejected=rejected,
        equity=equity,
        btc_equity=float(snap.btc_benchmark_usd) if snap and snap.btc_benchmark_usd else None,
        cash=float(book.cash_usd) if book and book.cash_usd is not None else None,
        day_loss=None
        if snap is None or not snap.equity_usd
        else max(0.0, 1 - float(snap.worst_equity_usd) / float(snap.equity_usd)),
        drawdown=None if not (equity and peak) else max(0.0, 1 - equity / peak),
        positions={p.symbol: float(p.last_value_usd or p.cost_usd) for p in positions},
        status=run.status if run else "RUNNING",
    )


async def _history(
    sessions: async_sessionmaker[AsyncSession], a: Analysis, panel
) -> report.HistoryInfo:
    """«¿Acertó?»: solo análisis emitidos en vivo, con la rentabilidad de sus elegidas de la
    apertura siguiente a la vela de hoy (7 días)."""
    week_ago = a.day - timedelta(days=7)
    info = report.HistoryInfo()
    async with sessions() as s:
        past = list(
            await s.scalars(
                select(CryptoAnalysis).where(
                    CryptoAnalysis.computed_live.is_(True),
                    CryptoAnalysis.strategy_version == STRATEGY_VERSION,
                    CryptoAnalysis.day <= week_ago,
                )
            )
        )
        rows = {}
        for an in past:
            rows[an.day] = list(
                await s.scalars(
                    select(CryptoAnalysisRow)
                    .where(CryptoAnalysisRow.analysis_id == an.id, CryptoAnalysisRow.rank <= 5)
                    .order_by(CryptoAnalysisRow.rank)
                )
            )

    def change(symbol: str, start: date, end: date) -> float | None:
        series = panel.series.get(symbol)
        if series is None:
            return None
        i, j = series.at(start + timedelta(days=1)), series.index(end)
        if i is None or j is None or j <= i:
            return None
        return series.close[j] / series.open[i] - 1

    for day, picks in sorted(rows.items()):
        btc = change("BTCUSDT", day, day + timedelta(days=7))
        top = picks[0] if picks else None
        if top is not None and btc is not None:
            r = change(top.symbol, day, day + timedelta(days=7))
            if r is not None:
                info.top1_total += 1
                info.top1_beats += int(r > btc)
        if day == week_ago:
            info.week_ago_btc = btc
            for p in picks:
                r = change(p.symbol, day, a.day)
                if r is not None:
                    info.week_ago.append((p.symbol[:-4], r))
    return info


async def _heartbeat(sessions: async_sessionmaker[AsyncSession], details: dict) -> None:
    async with sessions.begin() as s:
        stmt = insert(ServiceHeartbeat).values(
            service=SERVICE, beat_at=datetime.now(UTC), details=details
        )
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=["service"],
                set_={"beat_at": stmt.excluded.beat_at, "details": details},
            )
        )


async def run_daily(
    sessions: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    sync: bool = True,
    say: Say = lambda _m: None,
) -> DailyResult:
    now = now or datetime.now(UTC)
    day = now.date() - timedelta(days=1)  # última vela diaria cerrada
    limits = RiskLimits.model_construct()
    async with BinanceMarket() as market:
        if sync:
            say("Descargando velas nuevas de Binance…")
            await sync_all(sessions, market)
        panel = await load_panel(sessions, start=day - timedelta(days=HISTORY_DAYS), end=day)
        say(f"Analizando el {day:%d-%m-%Y}…")
        a = analyze(panel, day)
        books = await market.book_tickers()
    quotes: dict[str, BookTicker] = {b.symbol: b for b in books}
    filters = await _filters(sessions)
    bt = await latest_backtest(sessions)
    level = explain.confidence(bt.excess_annual is not None, bt.excess_annual, bt.light, None)
    analysis_id = await store(sessions, a, live=True, confidence=level, commit=code_commit())
    if not prereg_check() and not any(r.kind == "continua" for r in await active_runs(sessions)):
        btc = quotes.get("BTCUSDT")
        await start_run(
            sessions,
            kind="continua",
            start_day=day,
            limits=limits,
            btc_price=btc.ask if btc else None,
        )
        say("Arranca la cartera de prueba continua (100 $ ficticios).")
    steps = []
    if a.status == "ok":
        for run in await active_runs(sessions):
            steps.append(
                await paper_step(
                    sessions, run.id, a.view, quotes, filters, now=now, analysis_id=analysis_id
                )
            )
    continua = next((r for r in await active_runs(sessions) if r.kind == "continua"), None)
    paper = None
    if continua is not None:
        step = next(
            (st for st in steps if st.run_id == continua.id),
            StepResult(continua.id, day, "sin_paso"),
        )
        paper = await _paper_info(sessions, continua.id, day, step)
    history = await _history(sessions, a, panel)
    sem_line = (
        f"SEMÁFORO «¿ES RENTABLE?»: GRIS (falta muestra). Backtest: "
        f"{(bt.light or 'sin datos').upper()} · En vivo: "
        + (
            f"día {(day - continua.start_day).days + 1} de 28."
            if continua is not None
            else "sin cartera continua (falta el preregistro)."
        )
    )
    text = report.render(
        a,
        generated_at=now,
        paper=paper,
        history=history,
        backtest=bt if bt.excess_annual is not None else None,
        semaforo_line=sem_line,
        confidence=level,
    )
    await set_report(sessions, analysis_id, text, report.to_json(a))
    await _heartbeat(sessions, {"dia": day.isoformat(), "estado": a.status})
    return DailyResult(day, analysis_id, steps, text)


async def run_forever(
    sessions: async_sessionmaker[AsyncSession], *, at_minute: int = 10, say: Say = print
) -> None:
    """Servicio: el paso diario a las 00:10 UTC; si falla, se reintenta cada 10 minutos
    hasta las 02:00."""
    while True:
        now = datetime.now(UTC)
        target = now.replace(hour=0, minute=at_minute, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        deadline = target.replace(hour=2, minute=0)
        while datetime.now(UTC) < deadline:
            try:
                result = await run_daily(sessions, say=say)
                say(result.text)
                break
            except Exception as exc:  # el servicio no debe caerse
                say(f"Fallo del paso diario ({type(exc).__name__}: {exc}); reintento en 10 min.")
                await asyncio.sleep(600)

"""El paso diario del bot: descargar → analizar → cartera de prueba → informe.

Se ejecuta poco después de las 00:00 UTC, con la vela del día anterior ya cerrada.

* Con datos incompletos se reintenta cada 10 minutos hasta las 02:00 UTC sin guardar nada
  «en vivo»; el último intento guarda el análisis incompleto y el día queda «sin datos»
  para las carteras: ese día no se decide nada (los pares que dejaron de cotizar se venden
  en el paso siguiente).
* Pasadas las 02:00 UTC el paso llega tarde: el análisis se guarda como recalculado y el
  día queda «perdido» para las carteras, salvo que se pida expresamente (``tarde``). Los
  días que la máquina estuvo apagada no se rellenan con precios pasados.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.config import RiskLimits
from copybot.crypto import explain, report
from copybot.crypto.analysis import Analysis, analyze, set_report, store
from copybot.crypto.binance import BinanceMarket, BookTicker
from copybot.crypto.panel import load_panel
from copybot.crypto.paper import (
    ACTIVE,
    LIVE_BLOCK,
    PORTFOLIO,
    Filters,
    StepResult,
    active_runs,
    paper_step,
    record_day,
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
    CryptoPaperDay,
    CryptoPaperPosition,
    CryptoPaperRun,
    CryptoPaperSnapshot,
    MarketSymbol,
    ServiceHeartbeat,
)

HISTORY_DAYS = 500  # velas que hacen falta para el análisis (medias, 365 días de «semana mala»)
SERVICE = "crypto_daily"
RETRY_UNTIL = time(2, 0)  # hasta las 02:00 UTC se reintenta; después el paso llega tarde
RETRY_EVERY = timedelta(minutes=10)
LAST_TRY = timedelta(minutes=5)  # el último intento empieza como tarde 5 minutos antes
NO_STEP = {  # por qué un día no hubo paso de la cartera (``crypto_paper_days.status``)
    "perdido": "Hoy no opera: el paso llegó después de las 02:00 UTC y no se compra ni se "
    "vende con precios de otro momento. El día queda como perdido.",
    "sin_datos": "Hoy no opera: los datos del día seguían incompletos a las 02:00 UTC.",
}
Say = Callable[[str], None]


@dataclass
class DailyResult:
    day: date
    analysis_id: int | None
    steps: list[StepResult]
    text: str
    # ok; reintentar (datos incompletos antes de las 02:00: no se guardó nada en vivo);
    # sin_datos (incompletos en el último intento); tarde (después de las 02:00).
    status: str = "ok"


def deadline_for(day: date) -> datetime:
    """Hasta cuándo se puede dar el paso del día ``day`` (su vela cierra a las 00:00 UTC
    del día siguiente; a partir de las 02:00 llega tarde)."""
    return datetime.combine(day + timedelta(days=1), RETRY_UNTIL, tzinfo=UTC)


async def _filters(sessions: async_sessionmaker[AsyncSession]) -> dict[str, Filters]:
    async with sessions() as s:
        rows = await s.scalars(select(MarketSymbol).where(MarketSymbol.exchange == EXCHANGE))
        return {r.symbol: Filters(r.step_size, r.min_notional, r.status, r.spot) for r in rows}


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
        day_row = await s.get(CryptoPaperDay, (run_id, day))
        before = select(CryptoPaperSnapshot).where(
            CryptoPaperSnapshot.run_id == run_id,
            CryptoPaperSnapshot.book == PORTFOLIO,
            CryptoPaperSnapshot.day < day,
        )
        prev_snap = await s.scalar(before.order_by(CryptoPaperSnapshot.day.desc()).limit(1))
        peak_before = await s.scalar(
            select(func.max(CryptoPaperSnapshot.equity_usd)).where(
                CryptoPaperSnapshot.run_id == run_id,
                CryptoPaperSnapshot.book == PORTFOLIO,
                CryptoPaperSnapshot.day < day,
            )
        )
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
    # Las mismas medidas que las paradas (apartado 6.4): el peor valor del día, W(t),
    # frente al patrimonio al cierre anterior, E(t−1), y frente al máximo hasta t−1.
    initial = float(run.initial_usd) if run else None
    worst = float(snap.worst_equity_usd) if snap else None
    prev = float(prev_snap.equity_usd) if prev_snap else initial
    peak = initial if peak_before is None or initial is None else max(initial, float(peak_before))
    return report.PaperInfo(
        holdings=[p.symbol[:-4] for p in positions],
        sells=sells,
        buys=buys,
        rejected=rejected,
        equity=equity,
        btc_equity=float(snap.btc_benchmark_usd) if snap and snap.btc_benchmark_usd else None,
        cash=float(book.cash_usd) if book and book.cash_usd is not None else None,
        day_loss=None if worst is None or not prev else max(0.0, 1 - worst / prev),
        drawdown=None if worst is None or not peak else max(0.0, 1 - worst / peak),
        positions={p.symbol: float(p.last_value_usd or p.cost_usd) for p in positions},
        status=run.status if run else "RUNNING",
        blocked=[
            (symbol[:-4], why.removeprefix(f"{LIVE_BLOCK}: ")) for symbol, why in step.blocked
        ],
        note=None if day_row is None else NO_STEP.get(day_row.status),
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


async def _start_continua(
    sessions: async_sessionmaker[AsyncSession],
    day: date,
    quotes: dict[str, BookTicker],
    limits: RiskLimits,
    say: Say,
) -> None:
    """La continua arranca sola una única vez por versión, al registrarla, y no se
    reinicia nunca: si ya hay una de esta versión, en cualquier estado (también parada),
    no se crea otra."""
    if prereg_check():
        return
    async with sessions() as s:
        exists = await s.scalar(
            select(CryptoPaperRun.id)
            .where(
                CryptoPaperRun.kind == "continua",
                CryptoPaperRun.strategy_version == STRATEGY_VERSION,
            )
            .limit(1)
        )
    if exists is not None:
        return
    btc = quotes.get("BTCUSDT")
    await start_run(
        sessions, kind="continua", start_day=day, limits=limits, btc_price=btc.ask if btc else None
    )
    say("Arranca la cartera de prueba continua (100 $ ficticios).")


async def run_daily(
    sessions: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    sync: bool = True,
    say: Say = lambda _m: None,
    late_ok: bool = False,
    final: bool = False,
) -> DailyResult:
    """Paso diario del día anterior a ``now``.

    * ``final``: último intento antes de las 02:00. Con datos incompletos, si no es el
      último intento no se guarda nada en vivo (``reintentar``); si lo es, se guarda el
      análisis incompleto y el día queda «sin datos» para las carteras.
    * Pasadas las 02:00 UTC el paso llega tarde: no se opera (día «perdido») salvo con
      ``late_ok`` (``copybot crypto daily --tarde``); el análisis se guarda como recalculado.
    """
    now = now or datetime.now(UTC)
    day = now.date() - timedelta(days=1)  # última vela diaria cerrada
    late = now > deadline_for(day)
    limits = RiskLimits.model_construct()
    async with BinanceMarket() as market:
        if sync:
            say("Descargando velas nuevas de Binance…")
            await sync_all(sessions, market)
        panel = await load_panel(sessions, start=day - timedelta(days=HISTORY_DAYS), end=day)
        say(f"Analizando el {day:%d-%m-%Y}…")
        a = analyze(panel, day)
        books = await market.book_tickers()
    if a.status != "ok" and not final and not late:
        text = (
            f"Datos incompletos del {day:%d-%m-%Y} ({len(a.view.universe.members)} monedas "
            "evaluables o sin vela de BTC): no se guarda nada en vivo; se reintenta hasta las "
            "02:00 UTC."
        )
        await _heartbeat(sessions, {"dia": day.isoformat(), "estado": "reintentar"})
        return DailyResult(day, None, [], text, "reintentar")
    quotes: dict[str, BookTicker] = {b.symbol: b for b in books}
    filters = await _filters(sessions)
    bt = await latest_backtest(sessions)
    level = explain.confidence(bt.excess_annual is not None, bt.excess_annual, bt.light, None)
    analysis_id = await store(sessions, a, live=not late, confidence=level, commit=code_commit())
    await _start_continua(sessions, day, quotes, limits, say)
    steps: list[StepResult] = []
    if (late and not late_ok) or a.status != "ok":
        status = "tarde" if late and not late_ok else "sin_datos"
        no_step = "perdido" if status == "tarde" else "sin_datos"
        for run in await active_runs(sessions):
            await record_day(
                sessions,
                run.id,
                day,
                no_step,
                now=now,
                note=NO_STEP[no_step],
                analysis_id=analysis_id,
            )
        if status == "tarde":
            say("Paso fuera de plazo: no se opera (con --tarde se opera igual).")
    else:
        status = "ok"
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
    await _heartbeat(sessions, {"dia": day.isoformat(), "estado": status})
    return DailyResult(day, analysis_id, steps, text, status)


async def day_done(sessions: async_sessionmaker[AsyncSession], day: date) -> bool:
    """El paso de ``day`` ya está hecho: hay análisis en vivo y todas las carteras activas
    tienen ese día apuntado."""
    async with sessions() as s:
        live = await s.scalar(
            select(CryptoAnalysis.id).where(
                CryptoAnalysis.day == day,
                CryptoAnalysis.strategy_version == STRATEGY_VERSION,
                CryptoAnalysis.computed_live.is_(True),
            )
        )
        if live is None:
            return False
        for run in await s.scalars(select(CryptoPaperRun).where(CryptoPaperRun.status.in_(ACTIVE))):
            if await s.get(CryptoPaperDay, (run.id, day)) is None:
                return False
    return True


async def run_forever(
    sessions: async_sessionmaker[AsyncSession],
    *,
    at_minute: int = 10,
    say: Say = print,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    daily: Callable[..., Awaitable[DailyResult]] = run_daily,
    done: Callable[[date], Awaitable[bool]] | None = None,
) -> None:
    """Servicio: el paso diario a las 00:10 UTC. Si falla o los datos están incompletos,
    se reintenta cada 10 minutos hasta las 02:00; el último intento deja el día «sin
    datos». Si arranca (o se reinicia) entre las 00:10 y las 02:00 con el paso de hoy sin
    hacer, lo da en ese momento."""
    is_done = done or (lambda day: day_done(sessions, day))
    while True:
        now = clock()
        start = now.replace(hour=0, minute=at_minute, second=0, microsecond=0)
        day = now.date() - timedelta(days=1)
        deadline = deadline_for(day)
        if not start <= now < deadline or await is_done(day):
            target = start if now < start else start + timedelta(days=1)
            await sleep((target - now).total_seconds())
            continue
        await _attempts(sessions, deadline, say=say, clock=clock, sleep=sleep, daily=daily)
        left = (deadline - clock()).total_seconds()
        if left > 0:  # hasta el día siguiente no hay nada más que hacer
            await sleep(left)


async def _attempts(
    sessions: async_sessionmaker[AsyncSession],
    deadline: datetime,
    *,
    say: Say,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    daily: Callable[..., Awaitable[DailyResult]],
) -> None:
    """Intentos del paso de hoy, cada 10 minutos, hasta uno completo o el último."""
    while True:
        now = clock()
        final = now + RETRY_EVERY > deadline - LAST_TRY
        try:
            result = await daily(sessions, now=now, say=say, final=final)
        except Exception as exc:  # el servicio no debe caerse
            say(f"Fallo del paso diario ({type(exc).__name__}: {exc}).")
        else:
            say(result.text)
            if result.status != "reintentar":
                return
        if final:
            return
        wait = min(RETRY_EVERY, deadline - LAST_TRY - clock())
        say(f"Reintento en {max(wait.total_seconds(), 0) / 60:.0f} min.")
        await sleep(max(wait.total_seconds(), 0.0))

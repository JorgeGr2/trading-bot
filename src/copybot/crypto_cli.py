"""Órdenes del bot de cripto (Binance, solo simulación).

copybot crypto sync [--since 2017-07-01]   descarga pares y velas diarias (lo que falte)
copybot crypto costs calibrate | show      fotos del libro (bid/ask) y comparación con la tabla
copybot crypto prereg check                el código coincide con el preregistro
copybot crypto backtest [--until D] [--seed 7] [--null 1000] [--out F]
                                           backtest de la regla v1 (siempre se registra)
copybot crypto analyze [--day D] [--out F] análisis de un día pasado (recalculado)
copybot crypto today                       último informe diario guardado
copybot crypto coin SÍMBOLO [--day D]      por qué una moneda está o no, y en qué puesto
copybot crypto paper start [--days N] | status | pause | resume | stop [--kind K | --id N]
                                           (parar la continua exige --kind continua o --id)
copybot crypto daily [--tarde]             una vez: descargar → analizar → cartera → informe
                                           (después de las 02:00 UTC solo opera con --tarde)
copybot crypto run                         servicio: el paso diario a las 00:10 UTC
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from copybot.backtest import stats
from copybot.config import RiskLimits, Settings
from copybot.crypto import analysis, btreport, daily, paper, report, semaforo
from copybot.crypto.backtest import deflated, evidence, run_backtest
from copybot.crypto.binance import BinanceMarket
from copybot.crypto.calibration import calibration_report, calibration_snapshot
from copybot.crypto.panel import load_panel
from copybot.crypto.registry import (
    code_commit,
    config_of,
    k_count,
    prereg_check,
    record_run,
)
from copybot.crypto.strategy import STRATEGY_V1, STRATEGY_VERSION, VARIANTS
from copybot.crypto.sync import HISTORY_START, sync_all
from copybot.db.models import CryptoAnalysis, CryptoBacktestRun, CryptoPaperSnapshot
from copybot.db.session import make_engine, make_sessionmaker


def add_parsers(sub: argparse._SubParsersAction) -> None:
    crypto = sub.add_parser("crypto", help="bot de cripto en Binance (simulado)")
    csub = crypto.add_subparsers(dest="action", required=True)
    an = csub.add_parser("analyze", help="análisis de un día pasado (sin red)")
    an.add_argument("--day", type=date.fromisoformat, help="día (por defecto, ayer)")
    an.add_argument("--out", help="guardar el informe en este fichero")
    csub.add_parser("today", help="último informe diario guardado")
    coin = csub.add_parser("coin", help="situación de una moneda")
    coin.add_argument("symbol")
    coin.add_argument("--day", type=date.fromisoformat)
    paper = csub.add_parser("paper", help="cartera de prueba (simulada)")
    paper.add_argument("paper_action", choices=["start", "status", "pause", "resume", "stop"])
    paper.add_argument("--days", type=int, default=28, help="duración de la prueba")
    paper.add_argument(
        "--kind",
        choices=["continua", "prueba"],
        help="a qué cartera (por defecto, la prueba activa; parar la continua exige pedirlo)",
    )
    paper.add_argument("--id", type=int, dest="run_id", help="número de la cartera")
    dly = csub.add_parser("daily", help="paso diario completo, una vez")
    dly.add_argument(
        "--tarde",
        action="store_true",
        help="operar aunque sean más de las 02:00 UTC (si no, el día queda como perdido)",
    )
    csub.add_parser("run", help="servicio: paso diario a las 00:10 UTC")
    pre = csub.add_parser("prereg", help="comprobar el preregistro de la regla")
    pre.add_argument("prereg_action", choices=["check"])
    bt = csub.add_parser("backtest", help="backtest de la regla v1 (se registra siempre)")
    bt.add_argument("--until", type=date.fromisoformat, help="último día (por defecto, ayer)")
    bt.add_argument("--seed", type=int, default=7)
    bt.add_argument("--null", type=int, default=1000, help="carteras al azar del nulo")
    bt.add_argument("--out", help="guardar el informe en este fichero")
    cost = csub.add_parser("costs", help="calibrar los costes con el libro de Binance")
    cost.add_argument("costs_action", choices=["calibrate", "show"])
    sync = csub.add_parser("sync", help="descargar pares y velas diarias de Binance")
    sync.add_argument(
        "--since",
        type=lambda v: datetime.fromisoformat(v).replace(tzinfo=UTC),
        default=HISTORY_START,
        help="desde qué día (solo para pares sin historial todavía)",
    )


async def run(args: argparse.Namespace, settings: Settings) -> int:
    engine = make_engine(settings.database_url.get_secret_value())
    sessions = make_sessionmaker(engine)
    try:
        if args.action == "sync":
            return await _sync(args, sessions)
        if args.action == "costs":
            return await _costs(args, sessions)
        if args.action == "prereg":
            problems = prereg_check()
            for p in problems:
                print(f"✗ {p}")
            print("El código coincide con el preregistro." if not problems else "")
            return 0 if not problems else 1
        if args.action == "backtest":
            return await _backtest(args, sessions)
        if args.action in ("analyze", "coin"):
            return await _analyze(args, sessions)
        if args.action == "today":
            return await _today(sessions)
        if args.action == "paper":
            return await _paper(args, sessions)
        if args.action == "daily":
            result = await daily.run_daily(sessions, say=_say, late_ok=args.tarde)
            print(result.text)
            return 0 if result.status in ("ok", "sin_datos") else 1
        if args.action == "run":
            await daily.run_forever(sessions, say=_say)
            return 0
    finally:
        await engine.dispose()
    return 2


async def _costs(args: argparse.Namespace, sessions) -> int:
    if args.costs_action == "calibrate":
        async with BinanceMarket() as market:
            taken, n = await calibration_snapshot(sessions, market)
        print(f"Foto del libro guardada ({n} pares, {taken:%Y-%m-%d %H:%M} UTC).")
    cal = await calibration_report(sessions)
    print(cal)
    return 0


async def _sync(args: argparse.Namespace, sessions) -> int:
    def progress(done: int, total: int, symbol: str) -> None:
        if done % 25 == 0 or done == total:
            print(f"  {done}/{total} pares ({symbol})", file=sys.stderr, flush=True)

    async with BinanceMarket() as market:
        result = await sync_all(sessions, market, since=args.since, progress=progress)
    print(
        f"Pares en Binance: {result.symbols} ({result.new_symbols} nuevos). Velas diarias "
        f"nuevas: {result.bars:,} en {result.updated_symbols} pares. Peticiones: "
        f"{result.requests}."
    )
    return 0


async def _backtest(args: argparse.Namespace, sessions) -> int:
    problems = prereg_check()
    if problems:
        print("No se ejecuta: el código no coincide con el preregistro.", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1
    end = args.until or (datetime.now(UTC).date() - timedelta(days=1))
    limits = RiskLimits.model_construct()  # topes del código, sin el entorno ni .env

    def say(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    say("Cargando velas…")
    panel = await load_panel(sessions, end=end)
    r = run_backtest(
        panel, end=end, limits=limits, seed=args.seed, null_seeds=args.null, progress=say
    )
    period = (r.start, r.end)
    common = {"period": period, "data_hash": r.data_hash, "seed": args.seed, "n_days": len(r.days)}
    ex = r.excess_a
    principal_id = await record_run(
        sessions,
        cfg=config_of(STRATEGY_V1, curva="A"),
        role="principal",
        sr_daily=ex.sr_daily,
        skew=ex.skew,
        kurt=ex.kurt,
        metrics={"exceso_anual": ex.annual, "final": r.curve_a.final},
        **common,
    )
    for key, params in VARIANTS.items():
        e = r.excess_variants[key]
        await record_run(
            sessions,
            cfg=config_of(params, curva="A"),
            role="variante",
            sr_daily=e.sr_daily,
            skew=e.skew,
            kurt=e.kurt,
            metrics={"exceso_anual": e.annual, "final": r.variants[key].final},
            **common,
        )
    for key, run in r.stress.items():
        e = r.excess_stress[key]
        await record_run(
            sessions,
            cfg=config_of(STRATEGY_V1, curva="A", estres=key),
            role="estres",
            sr_daily=e.sr_daily,
            skew=e.skew,
            kurt=e.kurt,
            metrics={"exceso_anual": e.annual, "final": run.final},
            **common,
        )
    k = await k_count(sessions)
    dsr = deflated(r, k)
    trl = (
        stats.min_trl(ex.sr_daily, ex.skew, ex.kurt, 1.96)
        if ex.sr_daily and ex.sr_daily > 0 and ex.skew is not None and ex.kurt is not None
        else None
    )
    sem = semaforo.evaluate(
        evidence(r, dsr=dsr, prereg_ok=not prereg_check(), panel_gaps=panel.global_gaps()),
        None,
        version=f"{STRATEGY_VERSION}:{STRATEGY_V1.params_hash()[:12]}",
        huella=r.data_hash[:16],
    )
    text = btreport.render(
        r,
        k=k,
        dsr=dsr,
        min_trl=trl,
        commit=code_commit(),
        semaforo_md=btreport.semaforo_section(sem),
    )
    periods = [
        (
            name,
            p["regla"]["anual"],
            p["btc"]["anual"],
            p["btc_filtro"]["anual"],
            p["cesta10"]["anual"],
        )
        for name, p in r.subperiods.items()
        if name in ("inicio-2020", "2021-2022", "2023-hoy")
    ]
    async with sessions.begin() as s:
        row = await s.get(CryptoBacktestRun, principal_id)
        if row is not None:
            row.report_md = text
            row.metrics = {
                **row.metrics,
                "margen": [ex.ci_low, ex.ci_high],
                "nulo_percentil": r.null_percentile,
                "luz": sem.luces.backtest,
                "periodos": periods,
                "dsr": dsr,
                "k": k,
            }
    if args.out:
        await asyncio.to_thread(Path(args.out).write_text, text, encoding="utf-8")
        say(f"Informe guardado en {args.out}")
    print(text)
    return 0


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def _analyze(args: argparse.Namespace, sessions) -> int:
    day = args.day or (datetime.now(UTC).date() - timedelta(days=1))
    panel = await load_panel(sessions, start=day - timedelta(days=daily.HISTORY_DAYS), end=day)
    a = analysis.analyze(panel, day)
    if args.action == "coin":
        print(report.coin_text(a, args.symbol.upper()))
        return 0
    await analysis.store(sessions, a, live=False, commit=code_commit())
    text = report.render(a, generated_at=datetime.now(UTC))
    if args.out:
        await asyncio.to_thread(Path(args.out).write_text, text, encoding="utf-8")
    print(text)
    return 0


async def _today(sessions) -> int:
    async with sessions() as s:
        row = await s.scalar(
            select(CryptoAnalysis)
            .where(CryptoAnalysis.report_md.is_not(None))
            .order_by(CryptoAnalysis.day.desc())
            .limit(1)
        )
    if row is None:
        print("Aún no hay ningún informe diario: ejecuta «copybot crypto daily».")
        return 1
    print(row.report_md)
    return 0


async def _paper(args: argparse.Namespace, sessions) -> int:
    runs = await paper.active_runs(sessions)
    if args.paper_action == "start":
        if prereg_check():
            print("No se puede empezar: la regla aún no está preregistrada.", file=sys.stderr)
            return 1
        day = datetime.now(UTC).date() - timedelta(days=1)
        run_id = await paper.start_run(
            sessions,
            kind="prueba",
            start_day=day,
            limits=RiskLimits.model_construct(),
            days=args.days,
        )
        print(
            f"Prueba n.º {run_id} de {args.days} días con 100 $ ficticios. Empieza en el "
            "próximo paso diario."
            + (
                " Con menos de 28 días el semáforo no puede pasar de gris."
                if args.days < 28
                else ""
            )
        )
        return 0
    if args.paper_action == "status":
        if not runs:
            print("No hay ninguna cartera de prueba activa.")
        async with sessions() as s:
            for r in runs:
                snap = await s.scalar(
                    select(CryptoPaperSnapshot)
                    .where(
                        CryptoPaperSnapshot.run_id == r.id, CryptoPaperSnapshot.book == "portfolio"
                    )
                    .order_by(CryptoPaperSnapshot.day.desc())
                    .limit(1)
                )
                value = "sin pasos todavía" if snap is None else f"{snap.equity_usd:.2f} $"
                print(f"n.º {r.id} ({r.kind}) desde {r.start_day}: {r.status}; valor {value}")
        return 0
    target, problem = paper_target(args.paper_action, runs, kind=args.kind, run_id=args.run_id)
    if target is None:
        print(problem, file=sys.stderr)
        return 1
    try:
        if args.paper_action == "pause":
            await paper.set_status(sessions, target.id, "PAUSED", "pausada a mano")
        elif args.paper_action == "resume":
            await paper.resume(sessions, target.id)
        else:
            await paper.set_status(sessions, target.id, "STOPPED", "parada a mano")
    except paper.PaperError as exc:
        print(f"Cartera n.º {target.id}: {exc}", file=sys.stderr)
        return 1
    print(f"Cartera n.º {target.id} ({target.kind}): {args.paper_action} hecho.")
    if args.paper_action == "stop" and target.kind == "continua":
        print(
            "La cartera continua no vuelve a arrancar sola: la luz en vivo de esta versión se "
            "queda sin días nuevos."
        )
    return 0


def paper_target(action: str, runs: list, *, kind: str | None = None, run_id: int | None = None):
    """(cartera activa a la que se aplica ``action``, o ``None`` y por qué). Sin ``kind`` ni
    ``run_id`` se elige la prueba activa (o, salvo para parar, la continua): parar la
    continua, que no se reinicia nunca, hay que pedirlo expresamente."""
    if run_id is not None:
        target = next((r for r in runs if r.id == run_id), None)
        return target, None if target else f"No hay ninguna cartera activa con el n.º {run_id}."
    if kind is not None:
        target = next((r for r in runs if r.kind == kind), None)
        return target, None if target else f"No hay ninguna cartera «{kind}» activa."
    test = next((r for r in runs if r.kind == "prueba"), None)
    continua = next((r for r in runs if r.kind == "continua"), None)
    if test is not None:
        return test, None
    if continua is not None and action == "stop":
        return None, (
            "No hay ninguna prueba activa. Para parar la cartera continua (no se reinicia "
            "nunca) hay que pedirlo: «copybot crypto paper stop --kind continua» o --id "
            f"{continua.id}."
        )
    return continua, None if continua else "No hay ninguna cartera activa."

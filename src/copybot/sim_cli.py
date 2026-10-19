"""Órdenes de la simulación.

copybot sim run [--minutes N] [--no-monitor]   arrancar el motor (y el monitor)
copybot sim status                             estado y resumen
copybot sim report                             informe completo
copybot sim pause | resume | stop              control manual
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import UTC, datetime

import structlog

from copybot.config import RiskLimits, Settings
from copybot.db.repo import Repository
from copybot.db.session import make_engine, make_sessionmaker
from copybot.ingest.prices import JupiterPrices
from copybot.monitor_runner import make_rpc, run_monitor
from copybot.sim.engine import Engine
from copybot.sim.executor import SimExecutor
from copybot.sim.market import LiveMarket
from copybot.sim.params import EngineParams, SafetyParams
from copybot.sim.quotes import JupiterQuotes
from copybot.sim.report import build_report, render
from copybot.sim.risk import halt_reasons
from copybot.sim.store import PORTFOLIO, SimStore

log = structlog.get_logger(__name__)


def add_parsers(sub: argparse._SubParsersAction) -> None:
    sim = sub.add_parser("sim", help="simulación de copy-trading (sin dinero real)")
    ssub = sim.add_subparsers(dest="action", required=True)
    run = ssub.add_parser("run", help="arrancar el motor de simulación")
    run.add_argument("--minutes", type=float, help="parar solo tras N minutos (prueba)")
    run.add_argument(
        "--no-monitor",
        action="store_true",
        help="no arrancar el monitor aquí (ya corre «copybot monitor» aparte)",
    )
    ssub.add_parser("status", help="estado y resumen")
    ssub.add_parser("report", help="informe completo")
    ssub.add_parser("pause", help="no abrir posiciones nuevas (las salidas siguen)")
    ssub.add_parser("resume", help="volver a RUNNING tras una pausa o una parada por riesgo")
    ssub.add_parser("stop", help="parar el motor")


async def run(args: argparse.Namespace, settings: Settings) -> int:
    engine = make_engine(settings.database_url.get_secret_value())
    sessions = make_sessionmaker(engine)
    store = SimStore(sessions)
    try:
        if args.action == "run":
            return await _run(args, settings, sessions, store)
        if args.action in ("status", "report"):
            report = await build_report(sessions, datetime.now(UTC))
            print(render(report, full=args.action == "report"))
            return 0
        return await _control(args.action, store)
    finally:
        await engine.dispose()


async def _control(action: str, store: SimStore) -> int:
    try:
        current = (await store.state()).state
    except RuntimeError:
        print("La simulación no se ha arrancado nunca: usa «copybot sim run».")
        return 1
    if action == "pause":
        ok = await store.set_state(
            "PAUSED", "pausa manual", "cli", only_from=["RUNNING", "READ_ONLY", "RECOVERING"]
        )
    elif action == "stop":
        ok = await store.set_state("STOPPED", "parada manual", "cli")
    else:
        # Sea cual sea el estado (también desde una pausa), no se reanuda con los
        # límites de pérdida superados.
        reasons = halt_reasons(await store.book_state(PORTFOLIO), RiskLimits())
        if reasons:
            print("No se reanuda: los límites de riesgo siguen superados.")
            for _, detail in reasons:
                print(f"  - {detail}")
            return 1
        ok = await store.set_state(
            "RUNNING", "reanudación manual", "cli", only_from=["PAUSED", "HALTED_RISK"]
        )
    state = (await store.state()).state
    print(f"Estado: {state}" if ok else f"Sin cambios: el estado es {current}.")
    return 0 if ok else 1


async def _run(args: argparse.Namespace, settings: Settings, sessions, store: SimStore) -> int:
    repo = Repository(sessions)
    if not await repo.active_wallets():
        print("No hay wallets que seguir: añade alguna con «copybot wallets add <dirección>».")
        return 2
    rpc, provider = make_rpc(settings, repo)
    params = EngineParams()
    quotes = JupiterQuotes()
    prices = JupiterPrices(seconds_between_requests=1.0)
    executor = SimExecutor(
        quotes, latency_ms=params.latency_ms, slippage_tolerance_bps=params.slippage_tolerance_bps
    )
    sim = Engine(
        store,
        LiveMarket(prices, rpc),
        quotes,
        executor,
        params=params,
        safety=SafetyParams(),
        limits=RiskLimits(),
        monitor_stale_seconds=max(settings.max_data_staleness_seconds, 30.0),
    )
    engine_task = asyncio.create_task(sim.run(), name="engine")
    tasks = [engine_task]
    if not args.no_monitor:
        tasks.append(
            asyncio.create_task(run_monitor(settings, repo, rpc, provider), name="monitor")
        )
    try:
        done, _ = await asyncio.wait(
            tasks,
            timeout=None if args.minutes is None else args.minutes * 60,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task.exception() is not None:
                raise task.exception()  # type: ignore[misc]
    finally:
        # Parada ordenada: el motor termina la vuelta en curso (sin dejar órdenes a medias)
        # y deja el estado en STOPPED; solo si no lo consigue a tiempo se cancela.
        sim.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(engine_task), timeout=60)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await quotes.aclose()
        await prices.aclose()
        await rpc.aclose()
    print(render(await build_report(sessions, datetime.now(UTC)), full=False))
    return 0

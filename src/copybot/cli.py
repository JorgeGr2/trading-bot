"""Línea de órdenes.

copybot wallets add <dirección> [--label TEXTO]
copybot wallets list
copybot monitor
copybot sim run | status | report | pause | resume | stop
copybot crypto sync | …   (bot de cripto en Binance: ver ``copybot.crypto_cli``)
copybot panel [--host 127.0.0.1] [--port 8000]   panel web local (solo lectura)

Descubrimiento y análisis de traders: ver ``copybot.traders_cli``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

from copybot import backtest_cli, crypto_cli, panel_cli, sim_cli, traders_cli
from copybot import logging as copylog
from copybot.config import Settings
from copybot.db.repo import Repository
from copybot.db.session import make_engine, make_sessionmaker
from copybot.envcheck import unknown_settings
from copybot.monitor_runner import make_rpc, run_monitor

log = structlog.get_logger(__name__)


async def _wallets(args: argparse.Namespace, settings: Settings) -> int:
    engine = make_engine(settings.database_url.get_secret_value())
    repo = Repository(make_sessionmaker(engine))
    try:
        if args.action == "add":
            added = await repo.add_wallet(args.address, label=args.label, source="manual")
            print("Añadida." if added else "Ya estaba en la lista.")
        else:
            for address in await repo.active_wallets():
                print(address)
    finally:
        await engine.dispose()
    return 0


async def _monitor(settings: Settings) -> int:
    engine = make_engine(settings.database_url.get_secret_value())
    repo = Repository(make_sessionmaker(engine))
    rpc, provider = make_rpc(settings, repo)
    try:
        return await run_monitor(settings, repo, rpc, provider)
    finally:
        await rpc.aclose()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="copybot")
    sub = parser.add_subparsers(dest="command", required=True)
    wallets = sub.add_parser("wallets", help="gestionar las wallets seguidas")
    wsub = wallets.add_subparsers(dest="action", required=True)
    add = wsub.add_parser("add")
    add.add_argument("address")
    add.add_argument("--label")
    wsub.add_parser("list")
    sub.add_parser("monitor", help="seguir las wallets en vivo")
    traders_cli.add_parsers(sub)
    sim_cli.add_parsers(sub)
    backtest_cli.add_parsers(sub)
    crypto_cli.add_parsers(sub)
    panel_cli.add_parsers(sub)
    args = parser.parse_args(argv)

    unknown = unknown_settings()
    if unknown:
        print(
            "Variables de configuración desconocidas (¿una errata?): " + ", ".join(unknown),
            file=sys.stderr,
        )
        return 2
    settings = Settings()
    if args.command == "wallets":
        copylog.configure()
        return asyncio.run(_wallets(args, settings))
    if args.command == "monitor":
        copylog.configure()
        return asyncio.run(_monitor(settings))
    if args.command == "backtest":
        copylog.configure("WARNING")
        return asyncio.run(backtest_cli.run(args, settings))
    if args.command == "crypto":
        copylog.configure("WARNING")
        return asyncio.run(crypto_cli.run(args, settings))
    if args.command == "panel":
        copylog.configure("WARNING")
        return panel_cli.run(args, settings)
    if args.command == "sim":
        copylog.configure("INFO" if args.action == "run" else "WARNING")
        return asyncio.run(sim_cli.run(args, settings))
    # El análisis escribe un informe legible; los logs, solo si hay avisos.
    copylog.configure("WARNING")
    return asyncio.run(traders_cli.run(args, settings))


if __name__ == "__main__":
    raise SystemExit(main())

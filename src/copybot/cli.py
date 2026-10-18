"""Línea de órdenes.

copybot wallets add <dirección> [--label TEXTO]
copybot wallets list
copybot monitor
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

import structlog

from copybot import logging as copylog
from copybot.config import Settings
from copybot.db.repo import Repository
from copybot.db.session import make_engine, make_sessionmaker
from copybot.ingest.wallets import WalletMonitor
from copybot.solana.rpc import SolanaRpc
from copybot.solana.ws import LogsSubscriber

log = structlog.get_logger(__name__)


def _provider_name(url: str) -> str:
    return "helius" if "helius" in url else "solana-public" if "solana.com" in url else "rpc"


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
    rpc_url, ws_url = settings.rpc_endpoints()
    provider = _provider_name(rpc_url)
    engine = make_engine(settings.database_url.get_secret_value())
    repo = Repository(make_sessionmaker(engine))

    async def on_usage(method: str, requests: int, credits: int) -> None:
        await repo.record_usage(provider, method, requests, credits=credits)

    rpc = SolanaRpc(
        rpc_url,
        rate_per_second=settings.rpc_max_requests_per_second,
        timeout_seconds=settings.rpc_timeout_seconds,
        on_usage=on_usage,
    )
    monitor = WalletMonitor(rpc, repo, relayers=frozenset({settings.fomo_relayer}), source=provider)
    wallets = await repo.active_wallets()
    if not wallets:
        log.error("no_wallets", hint="añade wallets con: copybot wallets add <dirección>")
        return 2
    log.info("monitor_start", wallets=len(wallets), provider=provider)

    async def heartbeat() -> None:
        while True:
            await repo.heartbeat(
                "monitor",
                {
                    "stats": monitor.stats,
                    "last_live_at": monitor.last_live_at.isoformat()
                    if monitor.last_live_at
                    else None,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
            await asyncio.sleep(30)

    subscriber = LogsSubscriber(ws_url, wallets)
    beat = asyncio.create_task(heartbeat())
    try:
        await monitor.run(subscriber.stream())
    finally:
        beat.cancel()
        await rpc.aclose()
        await engine.dispose()
    return 0


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
    args = parser.parse_args(argv)

    settings = Settings()
    copylog.configure()
    if args.command == "wallets":
        return asyncio.run(_wallets(args, settings))
    return asyncio.run(_monitor(settings))


if __name__ == "__main__":
    raise SystemExit(main())

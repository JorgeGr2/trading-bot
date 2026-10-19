"""Arranque del monitor de wallets (lo usan ``copybot monitor`` y ``copybot sim run``)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog

from copybot.config import Settings
from copybot.db.repo import Repository
from copybot.ingest.wallets import WalletMonitor
from copybot.solana.rpc import SolanaRpc
from copybot.solana.ws import LogsSubscriber

log = structlog.get_logger(__name__)


def provider_name(url: str) -> str:
    return "helius" if "helius" in url else "solana-public" if "solana.com" in url else "rpc"


def make_rpc(settings: Settings, repo: Repository) -> tuple[SolanaRpc, str]:
    rpc_url, _ = settings.rpc_endpoints()
    provider = provider_name(rpc_url)

    async def on_usage(method: str, requests: int, credits: int) -> None:
        await repo.record_usage(provider, method, requests, credits=credits)

    rpc = SolanaRpc(
        rpc_url,
        rate_per_second=settings.rpc_max_requests_per_second,
        timeout_seconds=settings.rpc_timeout_seconds,
        on_usage=on_usage,
    )
    return rpc, provider


async def run_monitor(settings: Settings, repo: Repository, rpc: SolanaRpc, provider: str) -> int:
    """Sigue en vivo las wallets activas hasta que se cancele. 2 si no hay ninguna."""
    _, ws_url = settings.rpc_endpoints()
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
    return 0

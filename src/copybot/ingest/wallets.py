"""Monitor de wallets: detección en vivo y recuperación de huecos.

Reglas:

* La recuperación (``reconcile``) recorre el historial de cada wallet desde su
  checkpoint y es la única que avanza el checkpoint, así que nunca deja huecos.
* Las transacciones detectadas en vivo se guardan con ``detected_via='live'`` y su
  latencia; las recuperadas, con ``detected_via='backfill'``. Las fases siguientes
  no deben abrir posiciones a partir de eventos recuperados (la oportunidad ya pasó).
* Guardar dos veces la misma transacción no tiene efecto (idempotencia).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from copybot.db.repo import Repository
from copybot.solana.classifier import MalformedTransactionError, classify, summarize
from copybot.solana.rpc import RpcError
from copybot.solana.ws import LiveItem, Notification, Reconnected

log = structlog.get_logger(__name__)


class ReadOnlyRpc(Protocol):
    async def get_signatures_for_address(
        self,
        address: str,
        *,
        before: str | None = None,
        until: str | None = None,
        limit: int = 1000,
        commitment: str = "confirmed",
    ) -> list[dict[str, Any]]: ...

    async def get_transaction(
        self, signature: str, commitment: str = "confirmed"
    ) -> dict[str, Any] | None: ...


class TransactionUnavailableError(RuntimeError):
    """El nodo todavía no devuelve la transacción."""


class WalletMonitor:
    def __init__(
        self,
        rpc: ReadOnlyRpc,
        repo: Repository,
        *,
        relayers: frozenset[str],
        source: str,
        initial_backfill: int = 0,
        page_limit: int = 100,
        max_pages: int = 20,
        fetch_attempts: int = 5,
        fetch_retry_seconds: float = 1.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._rpc = rpc
        self._repo = repo
        self._relayers = relayers
        self._source = source
        self._initial_backfill = initial_backfill
        self._page_limit = page_limit
        self._max_pages = max_pages
        self._fetch_attempts = fetch_attempts
        self._fetch_retry = fetch_retry_seconds
        self._clock = clock
        self._sleep = sleep
        self.last_live_at: datetime | None = None
        self.stats: dict[str, int] = {
            "live": 0,
            "backfill": 0,
            "duplicates": 0,
            "gaps": 0,
            "rpc_errors": 0,
        }

    async def _fetch(self, signature: str) -> dict[str, Any]:
        for attempt in range(self._fetch_attempts):
            tx = await self._rpc.get_transaction(signature)
            if tx is not None:
                return tx
            await self._sleep(self._fetch_retry * (attempt + 1))
        raise TransactionUnavailableError(signature)

    async def _ingest(self, signature: str, detected_via: str, seen_at: datetime) -> bool:
        if await self._repo.has_transaction(signature):
            self.stats["duplicates"] += 1
            return False
        tx = await self._fetch(signature)
        fetched_at = self._clock()
        try:
            summary = summarize(tx, self._relayers)
            wallets = set(await self._repo.active_wallets())
            events = classify(tx, wallets, self._relayers)
        except MalformedTransactionError:
            log.error("malformed_transaction", signature=signature)
            return False
        stored = await self._repo.store_transaction(
            tx,
            summary,
            events,
            source=self._source,
            detected_via=detected_via,
            first_seen_at=seen_at,
            fetched_at=fetched_at,
        )
        if stored:
            self.stats[detected_via] += 1
        else:
            self.stats["duplicates"] += 1
        return stored

    async def handle_live(self, item: Notification) -> bool:
        self.last_live_at = item.received_at
        if item.failed:
            return False
        return await self._ingest(item.signature, "live", item.received_at)

    async def reconcile(self, wallet: str) -> int:
        """Procesa todo lo ocurrido desde el checkpoint, en orden cronológico."""
        checkpoint = await self._repo.get_checkpoint(wallet)
        if checkpoint is None:
            newest = await self._rpc.get_signatures_for_address(
                wallet, limit=max(1, self._initial_backfill)
            )
            if not newest:
                return 0
            if self._initial_backfill == 0:
                await self._repo.set_checkpoint(wallet, newest[0]["signature"], newest[0]["slot"])
                return 0
            signatures = newest
        else:
            signatures = []
            before: str | None = None
            for _ in range(self._max_pages):
                page = await self._rpc.get_signatures_for_address(
                    wallet, before=before, until=checkpoint.last_signature, limit=self._page_limit
                )
                signatures += page
                if len(page) < self._page_limit:
                    break
                before = page[-1]["signature"]
            else:
                # Hay más historial del que estamos dispuestos a recorrer: el hueco
                # más antiguo se pierde y se avisa.
                self.stats["gaps"] += 1
                log.error("reconcile_gap_truncated", wallet=wallet, fetched=len(signatures))

        processed = 0
        for entry in reversed(signatures):
            if entry.get("err") is None and await self._ingest(
                entry["signature"], "backfill", self._clock()
            ):
                processed += 1
        if signatures:
            await self._repo.set_checkpoint(
                wallet, signatures[0]["signature"], signatures[0]["slot"]
            )
        return processed

    async def reconcile_all(self) -> int:
        """Recupera todas las wallets. Un fallo del proveedor en una wallet no detiene
        las demás ni avanza su checkpoint: se reintentará en la próxima pasada."""
        total = 0
        for wallet in await self._repo.active_wallets():
            try:
                total += await self.reconcile(wallet)
            except (TransactionUnavailableError, RpcError) as exc:
                self.stats["rpc_errors"] += 1
                log.warning("reconcile_incomplete", wallet=wallet, error=str(exc))
        return total

    async def run(self, live: Any, reconcile_every_seconds: float = 300.0) -> None:
        """Bucle principal: recupera huecos al (re)conectar y periódicamente.

        Los fallos del proveedor nunca detienen el bucle. Una transacción que no se
        pudo descargar en vivo la recogerá la siguiente recuperación (como
        ``backfill``, porque para entonces la oportunidad ya habrá pasado).
        """

        async def periodic() -> None:
            while True:
                await asyncio.sleep(reconcile_every_seconds)
                try:
                    await self.reconcile_all()
                except Exception:
                    log.exception("periodic_reconcile_failed")

        # Los avisos se leen en una tarea aparte, sin esperar a que se procese el
        # anterior: así la hora de llegada que se anota es la real, aunque descargar
        # una transacción tarde (p. ej. si el proveedor limita las peticiones).
        queue: asyncio.Queue[LiveItem | None] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for live_item in live:
                    queue.put_nowait(live_item)
            finally:
                queue.put_nowait(None)

        tasks = [asyncio.create_task(periodic()), asyncio.create_task(pump())]
        try:
            while (item := await queue.get()) is not None:
                if isinstance(item, Reconnected):
                    await self.reconcile_all()
                    continue
                try:
                    await self.handle_live(item)
                except (TransactionUnavailableError, RpcError) as exc:
                    self.stats["rpc_errors"] += 1
                    log.warning("live_tx_failed", signature=item.signature, error=str(exc))
            await tasks[1]  # propaga un posible error del lector
        finally:
            for task in tasks:
                task.cancel()

"""Importación del historial de una wallet para analizarla.

Usa ``getTransactionsForAddress`` de Helius (hasta 1.000 transacciones completas por
petición), de la más reciente a la más antigua. Reglas:

* El periodo cubierto ``[covered_from, covered_to)`` solo se guarda al terminar, así
  que una importación interrumpida no deja un periodo «completo» que no lo esté. Lo
  ya guardado no se duplica al repetirla.
* Si se alcanza el máximo de páginas, se conserva lo más reciente y el periodo cubierto
  empieza en la transacción más antigua descargada (``truncated``).
* Las importaciones siguientes solo piden lo nuevo desde ``covered_to``.
* Se dejan fuera los últimos segundos (``safety_lag_seconds``): una transacción muy
  reciente puede no estar todavía en el índice del proveedor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

from copybot.db.repo import HistoryItem, Repository
from copybot.solana.classifier import MalformedTransactionError, classify, summarize
from copybot.solana.rpc import TransactionPage

log = structlog.get_logger(__name__)


class HistoryRpc(Protocol):
    credits: Any  # Counter de créditos por método

    async def get_transactions_for_address(
        self,
        address: str,
        *,
        full: bool,
        limit: int = ...,
        sort_order: str = ...,
        block_time_gte: int | None = ...,
        block_time_lt: int | None = ...,
        pagination_token: str | None = ...,
        associated_accounts: str = ...,
        only_succeeded: bool = ...,
    ) -> TransactionPage: ...


@dataclass(frozen=True)
class ImportResult:
    wallet: str
    pages: int
    transactions: int
    new_transactions: int
    new_events: int
    malformed: int
    truncated: bool
    covered_from: datetime
    covered_to: datetime
    credits: int


class HistoryImporter:
    def __init__(
        self,
        rpc: HistoryRpc,
        repo: Repository,
        *,
        relayers: frozenset[str],
        source: str,
        max_pages: int = 10,
        page_limit: int = 1000,
        safety_lag_seconds: int = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._rpc = rpc
        self._repo = repo
        self._relayers = relayers
        self._source = source
        self._max_pages = max_pages
        self._page_limit = page_limit
        self._lag = safety_lag_seconds
        self._clock = clock

    async def import_wallet(self, wallet: str, *, days: int) -> ImportResult:
        now = self._clock()
        to_time = (now - timedelta(seconds=self._lag)).replace(microsecond=0)
        target_from = to_time - timedelta(days=days)
        existing = await self._repo.get_history_import(wallet)
        incremental = (
            existing is not None
            and existing.covered_from <= target_from
            and existing.covered_to >= target_from
            and not existing.truncated
        )
        fetch_from = existing.covered_to if incremental and existing else target_from
        credits_before = sum(self._rpc.credits.values())

        pages = transactions = new_tx = new_events = malformed = 0
        oldest: int | None = None
        token: str | None = None
        truncated = False
        while True:
            if pages == self._max_pages:
                truncated = True
                break
            page = await self._rpc.get_transactions_for_address(
                wallet,
                full=True,
                limit=self._page_limit,
                sort_order="desc",
                block_time_gte=int(fetch_from.timestamp()),
                block_time_lt=int(to_time.timestamp()),
                pagination_token=token,
            )
            pages += 1
            items: list[HistoryItem] = []
            for raw in page.items:
                tx_index = raw.get("transactionIndex")
                try:
                    summary = summarize(raw, self._relayers)
                    events = classify(raw, {wallet}, self._relayers)
                except MalformedTransactionError:
                    malformed += 1
                    log.error("malformed_history_transaction", wallet=wallet)
                    continue
                items.append(HistoryItem(summary, tx_index, events))
                if summary.block_time is not None:
                    oldest = (
                        summary.block_time if oldest is None else min(oldest, summary.block_time)
                    )
            transactions += len(page.items)
            a, b = await self._repo.store_history_batch(items, source=self._source, imported_at=now)
            new_tx += a
            new_events += b
            token = page.pagination_token
            if token is None:
                break

        if truncated and oldest is not None:
            # Lo descargado es completo desde el segundo siguiente a la transacción más
            # antigua (en ese mismo segundo puede haber otras en la página no pedida).
            covered_from = datetime.fromtimestamp(oldest + 1, tz=UTC)
            if incremental:
                # Hay un hueco entre lo anterior y lo nuevo: el periodo continuo empieza aquí.
                log.warning("history_gap_after_truncation", wallet=wallet)
        elif incremental and existing is not None:
            covered_from = existing.covered_from
        else:
            covered_from = target_from
        covered_from = min(covered_from, to_time)
        credits = sum(self._rpc.credits.values()) - credits_before
        await self._repo.save_history_import(
            wallet,
            covered_from=covered_from,
            covered_to=to_time,
            truncated=truncated,
            transactions=transactions,
            credits=credits,
        )
        return ImportResult(
            wallet=wallet,
            pages=pages,
            transactions=transactions,
            new_transactions=new_tx,
            new_events=new_events,
            malformed=malformed,
            truncated=truncated,
            covered_from=covered_from,
            covered_to=to_time,
            credits=credits,
        )

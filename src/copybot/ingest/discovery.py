"""Descubrimiento de traders de FOMO a partir del relayer, por muestreo.

Descargar todo lo que firma el relayer (unas 9 transacciones por segundo) no cabe en el
presupuesto. En su lugar se toman **ventanas** de transacciones consecutivas que
empiezan en instantes al azar de los últimos días. Las wallets que aparecen en varias
ventanas son traders recurrentes, y son las candidatas a analizar.

Sesgo conocido: el muestreo favorece a quien opera mucho. Eso no es lo mismo que operar
bien; la puntuación lo corrige después con sus filtros (frecuencia, salidas rápidas…).
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import structlog

from copybot.analysis.positions import USD_QUOTES
from copybot.db.repo import Observation, Repository
from copybot.ingest.history import HistoryRpc
from copybot.solana.classifier import EventKind, MalformedTransactionError, classify

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WindowResult:
    requested_start: datetime
    transactions: int
    trades: int
    new_observations: int
    credits: int


class RelayerSampler:
    def __init__(
        self,
        rpc: HistoryRpc,
        repo: Repository,
        *,
        relayer: str,
        relayers: frozenset[str],
        per_window: int = 1000,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._rpc = rpc
        self._repo = repo
        self._relayer = relayer
        self._relayers = relayers
        self._per_window = per_window
        self._clock = clock

    def window_starts(self, windows: int, days: float, seed: int) -> list[datetime]:
        """Instantes de inicio, reproducibles con la misma semilla y la misma hora."""
        rng = random.Random(seed)  # noqa: S311 (muestreo, no criptografía)
        latest = self._clock() - timedelta(minutes=10)
        span = timedelta(days=days).total_seconds()
        return sorted(
            (latest - timedelta(seconds=rng.random() * span)).replace(microsecond=0)
            for _ in range(windows)
        )

    async def sample_window(self, start: datetime) -> WindowResult:
        before = sum(self._rpc.credits.values())
        page = await self._rpc.get_transactions_for_address(
            self._relayer,
            full=True,
            limit=self._per_window,
            sort_order="asc",
            block_time_gte=int(start.timestamp()),
        )
        observations: list[Observation] = []
        times: list[int] = []
        for raw in page.items:
            if raw.get("blockTime") is not None:
                times.append(int(raw["blockTime"]))
            try:
                events = classify(raw, None, self._relayers)
            except MalformedTransactionError:
                log.error("malformed_relayer_transaction")
                continue
            for ev in events:
                if not ev.usable_as_signal or ev.mint is None:
                    continue
                usd = abs(ev.quote_delta) if ev.quote_symbol in USD_QUOTES else None
                observations.append(
                    Observation(
                        signature=ev.signature,
                        wallet=ev.wallet,
                        block_time=datetime.fromtimestamp(ev.block_time, tz=UTC)
                        if ev.block_time is not None
                        else None,
                        kind="buy" if ev.kind is EventKind.BUY else "sell",
                        mint=ev.mint,
                        usd_value=Decimal(usd) if usd is not None else None,
                    )
                )
        credits = sum(self._rpc.credits.values()) - before
        _, new = await self._repo.save_discovery_window(
            requested_start=start,
            first_block_time=datetime.fromtimestamp(min(times), tz=UTC) if times else None,
            last_block_time=datetime.fromtimestamp(max(times), tz=UTC) if times else None,
            transactions=len(page.items),
            credits=credits,
            observations=observations,
        )
        return WindowResult(start, len(page.items), len(observations), new, credits)


class SignaturesRpc(Protocol):
    async def get_signatures_for_address(
        self,
        address: str,
        *,
        before: str | None = None,
        until: str | None = None,
        limit: int = 1000,
        commitment: str = "confirmed",
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Activity:
    wallet: str
    transactions: int  # con éxito, en los últimos ``days`` días
    capped: bool  # la consulta llegó a su máximo: hay al menos ``transactions``
    days: int

    @property
    def per_day(self) -> Decimal:
        return Decimal(self.transactions) / self.days


class ActivityScreener:
    """Cuánto opera una wallet, con una sola consulta barata (``getSignaturesForAddress``).

    Sirve para encontrar traders que operan **poco**, que el muestreo del relayer apenas
    ve, sin pagar la importación completa de su historial.
    """

    def __init__(
        self,
        rpc: SignaturesRpc,
        repo: Repository,
        *,
        days: int = 37,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._rpc = rpc
        self._repo = repo
        self._days = days
        self._clock = clock

    async def screen(self, wallet: str) -> Activity:
        now = self._clock()
        cutoff = (now - timedelta(days=self._days)).timestamp()
        limit = 1000
        entries = await self._rpc.get_signatures_for_address(wallet, limit=limit)
        recent = [e for e in entries if (e.get("blockTime") or 0) >= cutoff]
        # Si la página está llena y su transacción más antigua aún es reciente, hay más.
        capped = len(entries) == limit and len(recent) == len(entries)
        succeeded = sum(1 for e in recent if e.get("err") is None)
        await self._repo.save_screening(
            wallet, days=self._days, transactions=succeeded, capped=capped, screened_at=now
        )
        return Activity(wallet, succeeded, capped, self._days)


class TokenCreationLookup:
    """Fecha de creación de un token: su primera transacción (una petición por token).

    Se guarda para siempre en la tabla ``tokens``; nunca se consulta dos veces.
    """

    def __init__(
        self,
        rpc: HistoryRpc,
        repo: Repository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._rpc = rpc
        self._repo = repo
        self._clock = clock

    async def lookup(self, mint: str) -> datetime | None:
        page = await self._rpc.get_transactions_for_address(
            mint,
            full=False,
            limit=1,
            sort_order="asc",
            associated_accounts="none",
            only_succeeded=True,
        )
        first = page.items[0] if page.items else None
        created_at = (
            datetime.fromtimestamp(first["blockTime"], tz=UTC)
            if first and first.get("blockTime") is not None
            else None
        )
        await self._repo.save_token_creation(
            mint,
            created_slot=first.get("slot") if first else None,
            created_at=created_at,
            signature=first.get("signature") if first else None,
            checked_at=self._clock(),
        )
        return created_at

    async def ensure(self, mints: list[str], *, max_lookups: int) -> int:
        """Consulta como mucho ``max_lookups`` tokens aún no consultados.

        El orden es pseudoaleatorio pero fijo (por la dirección del token), para que la
        muestra no dependa de qué tokens son más populares ni del orden de llegada.
        """
        known = await self._repo.token_creation_times(mints)
        pending = sorted({m for m in mints if m not in known}, key=_stable_order)
        done = 0
        for mint in pending[:max_lookups]:
            await self.lookup(mint)
            done += 1
        return done


def _stable_order(mint: str) -> str:
    return hashlib.sha256(mint.encode()).hexdigest()

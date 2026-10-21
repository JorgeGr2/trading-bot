"""Precio retrasado: a qué precio se operaba un token unos segundos después de un
instante, sacado de las operaciones reales de otros usuarios en ese token (Helius).

Es lo que habríamos pagado (antes de nuestros propios costes) al copiar una operación
con el retraso medido en vivo: decidimos unos 4 s después del bloque del trader y la
compra simulada termina a los 6 s. Por defecto se miran las operaciones entre 4 y 8 s
después; si no hay ninguna, hasta 60 s.

Cuenta cualquier firmante de la transacción, también el que paga la comisión (en un
swap normal es el propio usuario; en FOMO es el relayer, que se descarta). Muchas
transacciones son de bots que no llegan a cambiar ningún saldo: si una página no da
bastantes operaciones se piden más, hasta ``MAX_PAGES``. Las operaciones pagadas en SOL
se pasan a dólares con el precio de SOL de los pools SOL-USDC (``poolprice``).

Cada página cuesta 10 créditos de Helius y el resultado se guarda en
``backtest_delayed_prices``: repetir el backtest no vuelve a gastar y da lo mismo.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.backtest.poolprice import SOL, PoolPrices
from copybot.db.models import DelayedPrice
from copybot.solana.classifier import (
    Confidence,
    EventKind,
    MalformedTransactionError,
    classify,
    summarize,
)
from copybot.solana.rpc import TransactionPage

log = structlog.get_logger(__name__)

DEFAULT_WINDOW = (4, 8)
EXTEND_TO_S = 60
# Precios de las primeras operaciones de la ventana que se promedian (mediana).
FIRST_SWAPS = 5
PAGE_SIZE = 100  # lo máximo por página: cuesta lo mismo que una más corta
# Operaciones más pequeñas no cuentan: en ellas pesan demasiado las propinas a los
# validadores y las comisiones de los bots, que inflan el precio de compra.
MIN_QUOTE = {"SOL": Decimal("0.05"), "USDC": Decimal(5), "USDT": Decimal(5)}
# Una ventana que acaba menos de esto antes de ahora aún puede recibir operaciones.
SETTLE_S = 120
MAX_PAGES = 3  # por tramo de ventana
USD_QUOTES = frozenset({"USDC", "USDT"})


class MintHistoryRpc(Protocol):
    async def get_transactions_for_address(
        self,
        address: str,
        *,
        full: bool,
        limit: int = 1000,
        sort_order: str = "desc",
        block_time_gte: int | None = None,
        block_time_lt: int | None = None,
        pagination_token: str | None = None,
        associated_accounts: str = "balanceChanged",
        only_succeeded: bool = True,
    ) -> TransactionPage: ...


@dataclass(frozen=True)
class SwapPrice:
    offset_s: int  # segundos después del instante pedido
    quote: str  # USDC, USDT o SOL
    price: Decimal  # en la moneda de cotización por token


def swap_prices(
    txs: Iterable[dict[str, Any]], mint: str, at: int, relayers: frozenset[str]
) -> list[SwapPrice]:
    """Precios de las compraventas de ``mint`` (de cualquier usuario) en ``txs``. Una
    sola por transacción (si firman las dos partes de un intercambio, cuenta la primera)."""
    out = []
    for tx in txs:
        block_time = tx.get("blockTime")
        if block_time is None:
            continue
        try:
            signers = set(summarize(tx, relayers).signers) - relayers
            events = classify(tx, signers, relayers)
        except (MalformedTransactionError, KeyError, IndexError, TypeError, ValueError):
            continue
        for ev in events:
            if ev.mint != mint or ev.kind not in (EventKind.BUY, EventKind.SELL):
                continue
            if ev.confidence == Confidence.LOW or ev.quote_symbol not in MIN_QUOTE:
                continue
            price = ev.price_in_quote
            if price is None or price <= 0 or abs(ev.quote_delta) < MIN_QUOTE[ev.quote_symbol]:
                continue
            out.append(SwapPrice(int(block_time) - at, ev.quote_symbol, price))
            break
    return sorted(out, key=lambda p: p.offset_s)


def usd_price(prices: list[SwapPrice], sol_usd: Decimal | None) -> tuple[Decimal | None, int]:
    """(precio en USD, operaciones usadas): mediana de las primeras operaciones. Las
    pagadas en SOL se convierten con ``sol_usd``; sin él, se descartan."""
    converted = []
    for p in prices:
        if p.quote in USD_QUOTES:
            converted.append(p.price)
        elif sol_usd is not None:
            converted.append(p.price * sol_usd)
        if len(converted) == FIRST_SWAPS:
            break
    if not converted:
        return None, 0
    return statistics.median(converted), len(converted)


class DelayedPrices:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        rpc: MintHistoryRpc | None,
        relayers: frozenset[str],
        *,
        sol: PoolPrices | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._rpc = rpc
        self._relayers = relayers
        self.sol = sol if sol is not None else PoolPrices(sessions, rpc, SOL)
        self._clock = clock
        self._pages = 0
        self._missing: set[tuple[str, int, int, int]] = set()

    @property
    def missing(self) -> int:
        """Precios distintos que hacían falta y no se pudieron consultar."""
        return len(self._missing)

    @property
    def lookups(self) -> int:
        """Páginas pedidas a Helius (10 créditos cada una), precio de SOL incluido."""
        return self._pages + self.sol.lookups

    async def get(
        self,
        mint: str,
        at: int,
        window: tuple[int, int] = DEFAULT_WINDOW,
        *,
        extend_to: int = EXTEND_TO_S,
        fetch: bool = True,
    ) -> DelayedPrice | None:
        """Precio de ``mint`` entre ``at+window[0]`` y ``at+window[1]`` segundos (o hasta
        ``at+extend_to`` si no hubo operaciones). ``None``: no está en caché y no se
        puede consultar. Una fila con ``price_usd`` nulo: no hubo operaciones."""
        lo, hi = window
        async with self._sessions() as s:
            cached = await s.get(DelayedPrice, (mint, at, lo, hi))
        if cached is not None:
            return cached
        settled = at + max(hi, extend_to) <= self._clock().timestamp() - SETTLE_S
        if not fetch or self._rpc is None or not settled:
            self._missing.add((mint, at, lo, hi))
            return None
        prices, pages = await self._swaps(mint, at, lo, hi)
        window_end = hi
        if not prices and extend_to > hi:
            prices, more = await self._swaps(mint, at, hi, extend_to)
            pages += more
            window_end = extend_to
        sol_usd = await self.sol.at(at + lo, fetch=True)
        price, used = usd_price(prices, sol_usd)
        if prices and not used:
            # Hubo operaciones, pero en SOL y sin precio de SOL: no es «nadie operó».
            self._missing.add((mint, at, lo, hi))
            return None
        row = {
            "mint": mint,
            "at": at,
            "delay_from": lo,
            "delay_to": hi,
            "price_usd": price,
            "swaps": used,
            "window_end": window_end,
            "sol_usd": sol_usd,
            "detail": {
                "paginas": pages,
                "operaciones": [
                    {"s": p.offset_s, "moneda": p.quote, "precio": str(p.price)}
                    for p in prices[:10]
                ],
            },
            "fetched_at": self._clock(),
        }
        async with self._sessions.begin() as s:
            await s.execute(insert(DelayedPrice).values(**row).on_conflict_do_nothing())
        async with self._sessions() as s:
            return await s.get(DelayedPrice, (mint, at, lo, hi))

    async def _swaps(self, mint: str, at: int, lo: int, hi: int) -> tuple[list[SwapPrice], int]:
        if self._rpc is None:
            raise RuntimeError("sin cliente de Helius")
        prices: list[SwapPrice] = []
        token = None
        pages = 0
        while pages < MAX_PAGES:
            pages += 1
            self._pages += 1
            page = await self._rpc.get_transactions_for_address(
                mint,
                full=True,
                limit=PAGE_SIZE,
                sort_order="asc",
                block_time_gte=at + lo,
                block_time_lt=at + hi,
                pagination_token=token,
            )
            prices += swap_prices(page.items, mint, at, self._relayers)
            token = page.pagination_token
            if len(prices) >= FIRST_SWAPS or len(page.items) < PAGE_SIZE or not token:
                break
        return sorted(prices, key=lambda p: p.offset_s), pages

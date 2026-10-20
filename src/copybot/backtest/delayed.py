"""Precio retrasado: a qué precio se operaba un token unos segundos después de un
instante, sacado de las operaciones reales de otros usuarios en ese token (Helius).

Es lo que habríamos pagado (antes de nuestros propios costes) al copiar una operación
con el retraso medido en vivo: decidimos unos 4 s después del bloque del trader y la
compra simulada termina a los 6 s. Por defecto se miran las operaciones entre 4 y 8 s
después; si no hay ninguna, hasta 60 s.

Cada consulta cuesta 10 créditos de Helius y se guarda en ``backtest_delayed_prices``:
repetir el backtest no vuelve a gastar y da exactamente lo mismo.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.db.models import DelayedPrice
from copybot.solana.classifier import EventKind, MalformedTransactionError, classify
from copybot.solana.rpc import TransactionPage

log = structlog.get_logger(__name__)

DEFAULT_WINDOW = (4, 8)
EXTEND_TO_S = 60
# Precios de las primeras operaciones de la ventana que se promedian (mediana).
FIRST_SWAPS = 5
USD_QUOTES = frozenset({"USDC", "USDT"})
# Un precio de SOL estimado en otra consulta sirve si es de menos de 2 h de distancia.
SOL_HINT_MAX_GAP_S = 7200


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
    """Precios de las compraventas de ``mint`` (de cualquier usuario) en ``txs``."""
    out = []
    for tx in txs:
        block_time = tx.get("blockTime")
        if block_time is None:
            continue
        try:
            events = classify(tx, None, relayers)
        except MalformedTransactionError:
            continue
        for ev in events:
            if ev.mint != mint or ev.kind not in (EventKind.BUY, EventKind.SELL):
                continue
            price = ev.price_in_quote
            if price is None or price <= 0 or ev.quote_symbol not in USD_QUOTES | {"SOL"}:
                continue
            out.append(SwapPrice(int(block_time) - at, ev.quote_symbol, price))
    return sorted(out, key=lambda p: p.offset_s)


def usd_price(
    prices: list[SwapPrice], sol_usd: Decimal | None
) -> tuple[Decimal | None, Decimal | None, int]:
    """(precio en USD, precio de SOL usado, operaciones usadas).

    Mediana de las primeras operaciones. Las pagadas en SOL se convierten con el precio
    de SOL de la propia ventana (si hay operaciones en USDC y en SOL a la vez) o con
    ``sol_usd``; si no hay ninguno, se descartan.
    """
    usd = [p.price for p in prices if p.quote in USD_QUOTES]
    sol = [p.price for p in prices if p.quote == "SOL"]
    if usd and sol:
        sol_usd = statistics.median(usd) / statistics.median(sol)
    converted = []
    for p in prices:
        if p.quote in USD_QUOTES:
            converted.append(p.price)
        elif sol_usd is not None:
            converted.append(p.price * sol_usd)
        if len(converted) == FIRST_SWAPS:
            break
    if not converted:
        return None, sol_usd, 0
    return statistics.median(converted), sol_usd, len(converted)


class DelayedPrices:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        rpc: MintHistoryRpc | None,
        relayers: frozenset[str],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._rpc = rpc
        self._relayers = relayers
        self._clock = clock
        self.lookups = 0  # consultas a Helius hechas
        self.missing = 0  # pedidas sin poder consultar (sin conexión permitida)

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
        if not fetch or self._rpc is None:
            self.missing += 1
            return None
        prices = await self._swaps(mint, at, lo, hi)
        window_end = hi
        if not prices and extend_to > hi:
            prices = await self._swaps(mint, at, hi, extend_to)
            window_end = extend_to
        hint = await self._sol_hint(at)
        price, sol_usd, used = usd_price(prices, hint)
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
                "operaciones": [
                    {"s": p.offset_s, "moneda": p.quote, "precio": str(p.price)}
                    for p in prices[:10]
                ]
            },
            "fetched_at": self._clock(),
        }
        async with self._sessions.begin() as s:
            await s.execute(insert(DelayedPrice).values(**row).on_conflict_do_nothing())
        async with self._sessions() as s:
            return await s.get(DelayedPrice, (mint, at, lo, hi))

    async def _swaps(self, mint: str, at: int, lo: int, hi: int) -> list[SwapPrice]:
        if self._rpc is None:
            raise RuntimeError("sin cliente de Helius")
        self.lookups += 1
        page = await self._rpc.get_transactions_for_address(
            mint,
            full=True,
            limit=40,
            sort_order="asc",
            block_time_gte=at + lo,
            block_time_lt=at + hi,
        )
        return swap_prices(page.items, mint, at, self._relayers)

    async def _sol_hint(self, at: int) -> Decimal | None:
        """Precio de SOL de la consulta guardada más cercana en el tiempo."""
        async with self._sessions() as s:
            row = await s.execute(
                select(DelayedPrice.sol_usd, DelayedPrice.at)
                .where(
                    DelayedPrice.sol_usd.is_not(None),
                    func.abs(DelayedPrice.at - at) <= SOL_HINT_MAX_GAP_S,
                )
                .order_by(func.abs(DelayedPrice.at - at))
                .limit(1)
            )
            found = row.first()
        return None if found is None else found[0]

    async def sol_usd_near(self, at: int) -> Decimal | None:
        return await self._sol_hint(at)

"""Precio histórico en dólares de SOL, BTC, ETH y del S&P 500 tokenizado, sacado de los
swaps reales de pools contra USDC en Solana (Helius).

Las APIs de precios de los exchanges no son accesibles desde este entorno, pero el propio
pool dice a qué precio se cambió el activo por USDC: en cada swap, las cuentas del pool
(propiedad de la dirección del pool en Raydium CLMM, Orca Whirlpool y Meteora DLMM)
reciben una moneda y entregan la otra.

Se toma una muestra al empezar cada hora (páginas de hasta 100 transacciones del pool
desde la hora en punto, 10 créditos cada una, hasta reunir unos pocos swaps) y se guarda
en ``backtest_price_bars`` como vela de muestra: apertura, máximo, mínimo y cierre **de
los swaps observados**, no de la hora entera, sin los que se alejan más de un 3 % de la
mediana de la muestra. El precio de la hora es la apertura (el primer swap válido desde
la hora en punto) y el de un instante, la interpolación lineal entre las dos horas que lo
rodean (SOL se mueve en torno a un 0,5 % por hora: el error es pequeño frente a lo que se
quiere medir).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.db.models import PriceBar
from copybot.solana.constants import USDC_MINT, WSOL_MINT

if TYPE_CHECKING:
    from copybot.backtest.delayed import MintHistoryRpc

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Asset:
    name: str
    mint: str
    # Pools contra USDC con más swaps (rutas de Jupiter); los siguientes solo se miran
    # si el primero no da bastantes swaps en la muestra.
    pools: tuple[str, ...]


SOL = Asset(
    "SOL",
    WSOL_MINT,
    (
        "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv",  # Raydium CLMM
        "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE",  # Orca Whirlpool
    ),
)
BTC = Asset(  # cbBTC, el BTC envuelto de Coinbase
    "BTC",
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
    (
        "HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM",  # Orca Whirlpool
        "7ubS3GccjhQY99AYNKXjNJqnXjaokEdfdV915xnCb96r",  # Meteora DLMM
    ),
)
ETH = Asset(  # ETH de Wormhole (Portal)
    "ETH",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
    ("AU971DrPyhhrpRnmEBp5pDTWL2ny7nofb5vYBjDJkR2E",),  # Orca Whirlpool
)
SPYX = Asset(  # SPYx: S&P 500 tokenizado de xStocks
    "SPYx",
    "XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W",
    (
        "Fae5dWVntUt6zbWu2voXxioDpMii7SqQwtsxBmoVCsHR",  # Orca Whirlpool
        "4pCZCVEiYyT4efNdXUdL2tJF8VGMgiMXrZWq6FiNXhRw",  # Raydium CLMM
    ),
)
ASSETS = {a.name: a for a in (SOL, BTC, ETH, SPYX)}

SOURCE = "helius-pool"
INTERVAL = "1h"
HOUR = 3600
MIN_SWAPS = 3
PAGE_SIZE = 100
MAX_PAGES = 3  # por pool: en los pools con más tráfico, casi todo son bots sin swap
MIN_USDC = Decimal(1)  # swaps más pequeños: el redondeo pesa demasiado
# Swaps a más de esto de la mediana de la muestra no cuentan: una transacción que cambia y
# además añade liquidez en el mismo pool deja unos saldos netos sin sentido como precio.
MAX_DEVIATION = Decimal("0.03")


@dataclass(frozen=True)
class PoolSwap:
    at: int
    price: Decimal  # USDC por unidad del activo
    amount: Decimal  # unidades del activo que cambiaron de manos


def _owner_deltas(tx: dict[str, Any], owner: str) -> dict[str, Decimal]:
    deltas: dict[str, Decimal] = defaultdict(Decimal)
    meta = tx["meta"]
    for key, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(key) or []:
            if b.get("owner") != owner:
                continue
            ui = b["uiTokenAmount"]
            deltas[b["mint"]] += sign * Decimal(ui["amount"]).scaleb(-int(ui["decimals"]))
    return deltas


def pool_swaps(txs: Iterable[dict[str, Any]], pool: str, mint: str) -> list[PoolSwap]:
    """Swaps ``mint``↔USDC de ``pool``: el pool recibe una moneda y entrega la otra.

    Las transacciones que pasan por el pool sin cambiar sus saldos (muchas son de bots
    que comprueban un arbitraje y no lo ejecutan) no cuentan."""
    out = []
    for tx in txs:
        block_time = tx.get("blockTime")
        if block_time is None or (tx.get("meta") or {}).get("err") is not None:
            continue
        try:
            d = _owner_deltas(tx, pool)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        amount, usdc = d.get(mint, Decimal(0)), d.get(USDC_MINT, Decimal(0))
        if not amount or abs(usdc) < MIN_USDC or (amount > 0) == (usdc > 0):
            continue
        out.append(PoolSwap(int(block_time), abs(usdc / amount), abs(amount)))
    return out


class PoolPrices:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        rpc: MintHistoryRpc | None,
        asset: Asset = SOL,
        *,
        max_lookups: int | None = None,
    ) -> None:
        self._sessions = sessions
        self._rpc = rpc
        self.asset = asset
        self._max = max_lookups
        self._key = f"{asset.name}/USD"
        self.lookups = 0  # consultas a Helius hechas
        self._memo: dict[int, Decimal | None] = {}

    async def at(self, ts: int, *, fetch: bool) -> Decimal | None:
        """Precio en ``ts``; ``None`` si no hay muestra (o no se puede pedir)."""
        hour = ts - ts % HOUR
        p0 = await self.hour(hour, fetch=fetch)
        p1 = await self.hour(hour + HOUR, fetch=fetch)
        if p0 is None or p1 is None:
            return p0 if p1 is None else p1
        return p0 + (p1 - p0) * Decimal(ts - hour) / HOUR

    async def hourly(self, start: int, end: int, *, fetch: bool) -> list[tuple[int, Decimal]]:
        """Precio de cada hora en punto entre ``start`` y ``end`` (las que tengan)."""
        first = start - start % HOUR
        await self._load(first, end)
        out = []
        for hour in range(first, end + 1, HOUR):
            price = await self.hour(hour, fetch=fetch)
            if price is not None:
                out.append((hour, price))
        return out

    async def _load(self, start: int, end: int) -> None:
        """Trae de la caché de una vez todas las horas del intervalo."""
        async with self._sessions() as s:
            bars = await s.scalars(
                select(PriceBar).where(
                    PriceBar.source == SOURCE,
                    PriceBar.asset == self._key,
                    PriceBar.interval == INTERVAL,
                    PriceBar.ts >= datetime.fromtimestamp(start, UTC),
                    PriceBar.ts <= datetime.fromtimestamp(end, UTC),
                )
            )
            for bar in bars:
                self._memo[int(bar.ts.timestamp())] = bar.open

    async def hour(self, hour: int, *, fetch: bool) -> Decimal | None:
        if hour in self._memo:
            return self._memo[hour]
        ts = datetime.fromtimestamp(hour, UTC)
        async with self._sessions() as s:
            bar = await s.get(PriceBar, (SOURCE, self._key, INTERVAL, ts))
        if bar is not None:
            self._memo[hour] = bar.open
            return bar.open
        capped = self._max is not None and self.lookups >= self._max
        if not fetch or self._rpc is None or capped:
            return None  # sin memorizar: otra llamada con permiso sí puede pedirla
        swaps: list[PoolSwap] = []
        for pool in self.asset.pools:
            token = None
            for _ in range(MAX_PAGES):
                self.lookups += 1
                page = await self._rpc.get_transactions_for_address(
                    pool,
                    full=True,
                    limit=PAGE_SIZE,
                    sort_order="asc",
                    block_time_gte=hour,
                    block_time_lt=hour + HOUR,
                    pagination_token=token,
                )
                swaps += pool_swaps(page.items, pool, self.asset.mint)
                token = page.pagination_token
                if len(swaps) >= MIN_SWAPS or len(page.items) < PAGE_SIZE or not token:
                    break
            if len(swaps) >= MIN_SWAPS:
                break
        if not swaps:
            log.warning("precio_pool_sin_swaps", activo=self.asset.name, hora=ts.isoformat())
            self._memo[hour] = None
            return None
        swaps.sort(key=lambda s: s.at)
        middle = statistics.median(s.price for s in swaps)
        prices = [s.price for s in swaps if abs(s.price / middle - 1) <= MAX_DEVIATION]
        row = {
            "source": SOURCE,
            "asset": self._key,
            "interval": INTERVAL,
            "ts": ts,
            "open": prices[0],
            "high": max(prices),
            "low": min(prices),
            "close": prices[-1],
            "volume": sum((s.amount for s in swaps), Decimal(0)),
        }
        async with self._sessions.begin() as s:
            await s.execute(insert(PriceBar).values(**row).on_conflict_do_nothing())
        self._memo[hour] = prices[0]
        return prices[0]

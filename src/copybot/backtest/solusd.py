"""Precio histórico de SOL en dólares, sacado de los swaps reales de pools SOL-USDC.

Las APIs de precios de los exchanges no son accesibles desde este entorno, pero el propio
pool dice a qué precio se cambió SOL por USDC: en cada swap, las cuentas del pool (que
son propiedad de la dirección del pool en Raydium CLMM y Orca Whirlpool) reciben una
moneda y entregan la otra.

Se toma una muestra al empezar cada hora (una página de transacciones del pool desde la
hora en punto, 10 créditos) y se guarda en ``backtest_price_bars`` como vela de muestra:
apertura, máximo, mínimo y cierre **de los swaps observados**, no de la hora entera. El
precio en un instante es la interpolación lineal entre los cierres de las dos muestras
que lo rodean (SOL se mueve en torno a un 0,5 % por hora: el error es pequeño frente a
lo que se quiere medir).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.db.models import PriceBar
from copybot.solana.constants import USDC_MINT, WSOL_MINT

if TYPE_CHECKING:
    from copybot.backtest.delayed import MintHistoryRpc

log = structlog.get_logger(__name__)

# Pools SOL-USDC con mucho volumen (rutas de Jupiter); el segundo solo se mira si el
# primero no da bastantes swaps en la muestra.
SOL_USDC_POOLS = (
    "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv",  # Raydium CLMM
    "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE",  # Orca Whirlpool
)
SOURCE = "helius-pool"
ASSET = "SOL/USD"
INTERVAL = "1h"
HOUR = 3600
MIN_SWAPS = 3
MIN_SOL = Decimal("0.01")  # swaps más pequeños: el redondeo pesa demasiado


@dataclass(frozen=True)
class PoolSwap:
    at: int
    price: Decimal  # USDC por SOL
    sol: Decimal  # SOL que cambiaron de manos


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


def pool_swaps(txs: Iterable[dict[str, Any]], pool: str) -> list[PoolSwap]:
    """Swaps SOL↔USDC de ``pool``: el pool recibe una moneda y entrega la otra.

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
        sol, usdc = d.get(WSOL_MINT, Decimal(0)), d.get(USDC_MINT, Decimal(0))
        if abs(sol) < MIN_SOL or not usdc or (sol > 0) == (usdc > 0):
            continue
        out.append(PoolSwap(int(block_time), abs(usdc / sol), abs(sol)))
    return out


class SolUsd:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        rpc: MintHistoryRpc | None,
        *,
        pools: tuple[str, ...] = SOL_USDC_POOLS,
    ) -> None:
        self._sessions = sessions
        self._rpc = rpc
        self._pools = pools
        self.lookups = 0  # consultas a Helius hechas
        self._memo: dict[int, Decimal | None] = {}

    async def at(self, ts: int, *, fetch: bool) -> Decimal | None:
        """Precio de SOL en ``ts``; ``None`` si no hay muestra (o no se puede pedir)."""
        hour = ts - ts % HOUR
        p0 = await self._sample(hour, fetch=fetch)
        p1 = await self._sample(hour + HOUR, fetch=fetch)
        if p0 is None or p1 is None:
            return p0 if p1 is None else p1
        return p0 + (p1 - p0) * Decimal(ts - hour) / HOUR

    async def _sample(self, hour: int, *, fetch: bool) -> Decimal | None:
        if hour in self._memo:
            return self._memo[hour]
        ts = datetime.fromtimestamp(hour, UTC)
        async with self._sessions() as s:
            bar = await s.get(PriceBar, (SOURCE, ASSET, INTERVAL, ts))
        if bar is not None:
            self._memo[hour] = bar.close
            return bar.close
        if not fetch or self._rpc is None:
            return None  # sin memorizar: otra llamada con permiso sí puede pedirla
        swaps: list[PoolSwap] = []
        for pool in self._pools:
            self.lookups += 1
            page = await self._rpc.get_transactions_for_address(
                pool,
                full=True,
                limit=100,
                sort_order="asc",
                block_time_gte=hour,
                block_time_lt=hour + HOUR,
            )
            swaps += pool_swaps(page.items, pool)
            if len(swaps) >= MIN_SWAPS:
                break
        if not swaps:
            log.warning("sol_usd_sin_swaps", hora=ts.isoformat())
            self._memo[hour] = None
            return None
        swaps.sort(key=lambda s: s.at)
        prices = [s.price for s in swaps]
        row = {
            "source": SOURCE,
            "asset": ASSET,
            "interval": INTERVAL,
            "ts": ts,
            "open": prices[0],
            "high": max(prices),
            "low": min(prices),
            "close": prices[-1],
            "volume": sum((s.sol for s in swaps), Decimal(0)),
        }
        async with self._sessions.begin() as s:
            await s.execute(insert(PriceBar).values(**row).on_conflict_do_nothing())
        self._memo[hour] = prices[-1]
        return prices[-1]

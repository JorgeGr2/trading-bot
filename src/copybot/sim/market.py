"""Datos de mercado en vivo para el motor: precio de SOL y liquidez (Jupiter Price API)
y cuenta del token (RPC). Todo de solo lectura."""

from __future__ import annotations

from decimal import Decimal

from copybot.ingest.prices import JupiterPrices
from copybot.sim.safety import AccountRpc, MintInfo, fetch_mint_info
from copybot.solana.constants import WSOL_MINT


class LiveMarket:
    def __init__(self, prices: JupiterPrices, rpc: AccountRpc) -> None:
        self._prices = prices
        self._rpc = rpc

    async def sol_usd(self) -> Decimal | None:
        price, _ = (await self._prices.fetch([WSOL_MINT])).get(WSOL_MINT, (None, None))
        return price

    async def liquidity_usd(self, mint: str) -> Decimal | None:
        _, liquidity = (await self._prices.fetch([mint])).get(mint, (None, None))
        return liquidity

    async def mint_info(self, mint: str) -> MintInfo | None:
        return await fetch_mint_info(self._rpc, mint)

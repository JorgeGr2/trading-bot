"""Ejecutor de órdenes. Solo existe el simulado.

El motor entrega cada orden a un ejecutor a través de esta interfaz. Si algún día se
aprobara el modo real (Fase 9), se añadiría otro ejecutor sin tocar la lógica de
decisión, de modo que la simulación mide exactamente lo que haría el bot real.

Ejecución simulada (apartado 6.1 del informe de la Fase 1):

1. t2: cotización para nuestro tamaño (la recibe ya hecha, del filtro de mercado).
2. Espera de verdad la latencia configurada (envío y confirmación).
3. t3: vuelve a cotizar. Si t3 queda por debajo de lo cotizado en t2 menos la
   tolerancia de slippage (el mínimo de la transacción), la orden falla como fallaría
   on-chain y se paga igualmente la comisión. Si no, se aplica el recorte supuesto por
   MEV: un *sandwich* se lleva su parte, pero como mucho hasta ese mínimo (si se llevara
   más, la transacción fallaría y el atacante no ganaría nada). Si Jupiter no responde
   en t3, la transacción ya se habría enviado: también falla y paga la comisión.
4. Si no falla, se descuenta la comisión de transferencia del token (Token-2022).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from copybot.sim.costs import CostModel
from copybot.sim.quotes import Quote, QuoteError
from copybot.solana.constants import USDC_MINT

USDC_DECIMALS = 6


class QuoteSource(Protocol):
    async def quote(
        self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int
    ) -> Quote | None: ...


@dataclass(frozen=True)
class Execution:
    ok: bool
    reason: str = ""
    token_amount: Decimal = Decimal(0)  # compra: recibidos; venta: vendidos
    usd_amount: Decimal = Decimal(0)  # compra: pagado; venta: cobrado (tras MEV)
    fees_usd: Decimal = Decimal(0)  # comisiones de red y prioritaria (también si falla)
    no_route: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


def to_raw(amount: Decimal, decimals: int) -> int:
    return int(amount.scaleb(decimals))


def from_raw(raw: int, decimals: int) -> Decimal:
    return Decimal(raw).scaleb(-decimals)


class Executor(Protocol):
    async def buy(
        self, mint: str, decimals: int, usd: Decimal, fee_bps: int, t2: Quote, costs: CostModel
    ) -> Execution: ...

    async def sell(
        self, mint: str, decimals: int, amount: Decimal, fee_bps: int, costs: CostModel
    ) -> Execution: ...


class SimExecutor:
    def __init__(
        self,
        quotes: QuoteSource,
        *,
        latency_ms: int,
        slippage_tolerance_bps: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._quotes = quotes
        self._latency = latency_ms / 1000
        self._tolerance = slippage_tolerance_bps
        self._sleep = sleep

    def _floor(self, t2: Quote) -> Decimal:
        """Mínimo que acepta la transacción: lo cotizado en t2 menos la tolerancia."""
        return Decimal(t2.out_raw) * (1 - Decimal(self._tolerance) / 10_000)

    def _slipped(self, t2: Quote, t3: Quote) -> Decimal | None:
        """Cuánto peor que t2 sale t3, si queda por debajo del mínimo."""
        if Decimal(t3.out_raw) < self._floor(t2):
            return 1 - Decimal(t3.out_raw) / Decimal(t2.out_raw)
        return None

    def _received(self, t2: Quote, t3: Quote, costs: CostModel) -> Decimal:
        """Lo recibido tras el MEV, que nunca baja del mínimo de la transacción."""
        return max(costs.after_mev(Decimal(t3.out_raw)), self._floor(t2))

    async def _t3(self, input_mint: str, output_mint: str, amount_raw: int) -> Quote | str | None:
        try:
            return await self._quotes.quote(input_mint, output_mint, amount_raw, self._tolerance)
        except QuoteError as exc:
            return f"Jupiter no responde al ejecutar (la transacción ya se habría enviado): {exc}"

    async def buy(
        self, mint: str, decimals: int, usd: Decimal, fee_bps: int, t2: Quote, costs: CostModel
    ) -> Execution:
        fee = costs.tx_fee_usd()
        await self._sleep(self._latency)
        t3 = await self._t3(USDC_MINT, mint, to_raw(usd, USDC_DECIMALS))
        detail = {"t2_out": t2.out_raw, "t2_at": t2.at.isoformat(), "route": list(t2.route)}
        if isinstance(t3, str):
            return Execution(False, t3, fees_usd=fee, detail=detail)
        if t3 is None:
            return Execution(False, "sin ruta de compra al ejecutar", fees_usd=fee, detail=detail)
        detail |= {"t3_out": t3.out_raw, "t3_at": t3.at.isoformat()}
        slipped = self._slipped(t2, t3)
        if slipped is not None:
            return Execution(
                False,
                f"slippage: en la ejecución se recibiría un {slipped:.2%} menos que al decidir",
                fees_usd=fee,
                detail=detail,
            )
        received_raw = self._received(t2, t3, costs)
        received = costs.after_transfer_fee(received_raw.scaleb(-decimals), fee_bps)
        return Execution(True, token_amount=received, usd_amount=usd, fees_usd=fee, detail=detail)

    async def sell(
        self, mint: str, decimals: int, amount: Decimal, fee_bps: int, costs: CostModel
    ) -> Execution:
        fee = costs.tx_fee_usd()
        # Lo que llega al pool tras la comisión de transferencia del token.
        sent = to_raw(costs.after_transfer_fee(amount, fee_bps), decimals)
        t2 = await self._quotes.quote(mint, USDC_MINT, sent, self._tolerance)
        if t2 is None:
            return Execution(False, "Jupiter no encuentra ruta de venta", no_route=True)
        await self._sleep(self._latency)
        t3 = await self._t3(mint, USDC_MINT, sent)
        detail = {"t2_out": t2.out_raw, "t2_at": t2.at.isoformat(), "route": list(t2.route)}
        if isinstance(t3, str):
            return Execution(False, t3, fees_usd=fee, detail=detail)
        if t3 is None:
            return Execution(False, "sin ruta de venta al ejecutar", fees_usd=fee, detail=detail)
        detail |= {"t3_out": t3.out_raw, "t3_at": t3.at.isoformat()}
        slipped = self._slipped(t2, t3)
        if slipped is not None:
            return Execution(
                False,
                f"slippage: en la ejecución se recibiría un {slipped:.2%} menos que al decidir",
                fees_usd=fee,
                detail=detail,
            )
        proceeds = self._received(t2, t3, costs).scaleb(-USDC_DECIMALS)
        return Execution(
            True, token_amount=amount, usd_amount=proceeds, fees_usd=fee, detail=detail
        )

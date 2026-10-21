"""Cotizaciones de Jupiter (solo lectura: cuánto se recibiría, sin construir nada).

Se usa el endpoint de cotización. Los que construyen o ejecutan transacciones están
prohibidos en el código (test de «solo simulación»).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import structlog

from copybot.ratelimit import RateLimiter

log = structlog.get_logger(__name__)

JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
# Errores con los que Jupiter dice que no hay ruta (o que el token no se puede negociar).
_NO_ROUTE_CODES = {"COULD_NOT_FIND_ANY_ROUTE", "TOKEN_NOT_TRADABLE", "NO_ROUTES_FOUND"}


@dataclass(frozen=True)
class Quote:
    input_mint: str
    output_mint: str
    in_raw: int
    out_raw: int
    price_impact_pct: Decimal
    route: tuple[str, ...]
    at: datetime


class QuoteError(RuntimeError):
    pass


class JupiterQuotes:
    def __init__(
        self,
        *,
        url: str = JUPITER_QUOTE_URL,
        rate_per_second: float = 0.9,
        max_attempts: int = 6,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._url = url
        self._limiter = RateLimiter(rate_per_second, sleep=sleep)
        self._attempts = max_attempts
        self._client = client or httpx.AsyncClient(timeout=20)
        self._owns_client = client is None
        self._sleep = sleep
        self._clock = clock
        self.requests = 0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def quote(
        self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int
    ) -> Quote | None:
        """Cotización para vender ``amount_raw`` unidades mínimas de ``input_mint``.

        ``None`` si Jupiter no encuentra ruta. Lanza ``QuoteError`` si no responde.
        """
        if amount_raw <= 0:
            return None
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(slippage_bps),
            "restrictIntermediateTokens": "true",
        }
        for attempt in range(self._attempts):
            await self._limiter.acquire()
            self.requests += 1
            try:
                response = await self._client.get(self._url, params=params)
            except httpx.HTTPError as exc:
                log.warning("jupiter_quote_transport_error", error=type(exc).__name__)
                await self._sleep(1.5 * (attempt + 1))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                await self._sleep(float(retry_after) if retry_after else 1.5 * (attempt + 1))
                continue
            try:
                data = response.json() if response.content else {}
            except ValueError:  # p. ej. una página HTML de un cortafuegos
                data = None
            if not isinstance(data, dict):
                raise QuoteError(
                    f"Jupiter respondió {response.status_code} sin JSON: {response.text[:120]!r}"
                )
            if response.status_code != 200:
                if (
                    data.get("errorCode") in _NO_ROUTE_CODES
                    or "route" in str(data.get("error", "")).lower()
                ):
                    return None
                raise QuoteError(f"Jupiter respondió {response.status_code}: {data}")
            try:
                return self._parse(data, input_mint, output_mint)
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                raise QuoteError(f"respuesta inesperada de Jupiter: {exc!r}") from exc
        raise QuoteError(f"Jupiter sin respuesta tras {self._attempts} intentos")

    def _parse(self, data: dict, input_mint: str, output_mint: str) -> Quote:
        return Quote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_raw=int(data["inAmount"]),
            out_raw=int(data["outAmount"]),
            price_impact_pct=Decimal(str(data.get("priceImpactPct") or 0)) * 100,
            route=tuple(
                str((leg.get("swapInfo") or {}).get("label", "?"))
                for leg in data.get("routePlan") or []
            ),
            at=self._clock(),
        )

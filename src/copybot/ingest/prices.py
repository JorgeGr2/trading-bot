"""Precio y liquidez actuales de tokens (Jupiter Price API v3, sin clave).

Solo se usa para valorar posiciones que siguen abiertas en el momento del análisis. El
precio se guarda con su hora de observación: un análisis a fecha ``as_of`` solo usa
precios observados antes de esa fecha.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import structlog

from copybot.db.repo import Repository

log = structlog.get_logger(__name__)

JUPITER_PRICE_URL = "https://lite-api.jup.ag/price/v3"
MAX_IDS_PER_REQUEST = 50


class JupiterPrices:
    def __init__(
        self,
        *,
        url: str = JUPITER_PRICE_URL,
        seconds_between_requests: float = 3.0,
        max_attempts: int = 6,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = url
        self._gap = seconds_between_requests
        self._attempts = max_attempts
        self._client = client or httpx.AsyncClient(timeout=30)
        self._owns_client = client is None
        self._sleep = sleep
        self.requests = 0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, mints: Iterable[str]) -> dict[str, tuple[Decimal | None, Decimal | None]]:
        """{mint: (precio en USD, liquidez en USD)}; (None, None) si Jupiter no lo cotiza."""
        mints = sorted(set(mints))
        out: dict[str, tuple[Decimal | None, Decimal | None]] = {}
        for i in range(0, len(mints), MAX_IDS_PER_REQUEST):
            chunk = mints[i : i + MAX_IDS_PER_REQUEST]
            data = await self._get(chunk)
            for mint in chunk:
                item = data.get(mint) or {}
                price = item.get("usdPrice")
                liquidity = item.get("liquidity")
                out[mint] = (
                    Decimal(str(price)) if price is not None else None,
                    Decimal(str(liquidity)) if liquidity is not None else None,
                )
            if i + MAX_IDS_PER_REQUEST < len(mints):
                await self._sleep(self._gap)
        return out

    async def _get(self, ids: list[str]) -> dict[str, Any]:
        for attempt in range(self._attempts):
            self.requests += 1
            try:
                response = await self._client.get(self._url, params={"ids": ",".join(ids)})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                log.warning("jupiter_transport_error", error=type(exc).__name__)
                await self._sleep(self._gap * (attempt + 1))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                await self._sleep(float(retry_after) if retry_after else self._gap * (attempt + 2))
                continue
            response.raise_for_status()
            return response.json() or {}
        raise RuntimeError(f"Jupiter Price API sin respuesta tras {self._attempts} intentos")


class PriceRefresher:
    """Guarda el precio actual de los tokens que no tengan uno reciente."""

    def __init__(
        self,
        prices: JupiterPrices,
        repo: Repository,
        *,
        fresh_minutes: int = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._prices = prices
        self._repo = repo
        self._fresh = timedelta(minutes=fresh_minutes)
        self._clock = clock

    async def refresh(self, mints: Iterable[str]) -> int:
        now = self._clock()
        mints = set(mints)
        recent = await self._repo.token_prices_at(mints, as_of=now, max_age=self._fresh)
        pending = sorted(mints - recent.keys())
        if not pending:
            return 0
        quotes = await self._prices.fetch(pending)
        await self._repo.save_token_prices(quotes, observed_at=now, source="jupiter")
        return len(pending)

"""Cliente JSON-RPC de Solana de solo lectura.

Incluye límite de peticiones por segundo, timeouts, reintentos con espera
exponencial (respetando ``Retry-After``) y contabilidad de uso por método para el
control de gasto. La URL puede contener la clave del proveedor, así que nunca se
registra.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

# Errores JSON-RPC que indican que conviene reintentar (nodo atrasado o saturado).
_RETRYABLE_RPC_CODES = {-32005, -32007, -32014, -32016, 429}


class RpcError(RuntimeError):
    def __init__(self, method: str, message: str, code: int | None = None) -> None:
        super().__init__(f"{method}: {message}")
        self.method = method
        self.code = code


class RateLimiter:
    """Espaciado mínimo entre peticiones (token bucket de capacidad 1)."""

    def __init__(
        self,
        rate_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second debe ser positivo")
        self._interval = 1.0 / rate_per_second
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next_at - now
            if wait > 0:
                await self._sleep(wait)
                now += wait
            self._next_at = max(now, self._next_at) + self._interval


UsageCallback = Callable[[str, int], Awaitable[None]]


class SolanaRpc:
    def __init__(
        self,
        url: str,
        *,
        rate_per_second: float = 1.0,
        timeout_seconds: float = 20.0,
        max_attempts: int = 5,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
        on_usage: UsageCallback | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = url
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None
        self._limiter = RateLimiter(rate_per_second, sleep=sleep)
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._on_usage = on_usage
        self._sleep = sleep
        self._ids = itertools.count(1)
        self.usage: Counter[str] = Counter()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> SolanaRpc:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _backoff(self, attempt: int) -> float:
        delay = min(self._backoff_max, self._backoff_base * 2**attempt)
        return delay * (0.5 + random.random() / 2)  # noqa: S311 (jitter, no criptografía)

    async def call(self, method: str, params: list[Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            await self._limiter.acquire()
            self.usage[method] += 1
            if self._on_usage is not None:
                await self._on_usage(method, 1)
            body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
            try:
                response = await self._client.post(self._url, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                delay = self._backoff(attempt)
                log.warning("rpc_transport_error", method=method, error=type(exc).__name__)
                await self._sleep(delay)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else self._backoff(attempt)
                delay = min(delay, self._backoff_max)
                last_error = RpcError(method, f"HTTP {response.status_code}", response.status_code)
                log.warning("rpc_http_retry", method=method, status=response.status_code)
                await self._sleep(delay)
                continue
            if response.status_code != 200:
                raise RpcError(method, f"HTTP {response.status_code}", response.status_code)
            data = response.json()
            error = data.get("error")
            if error:
                code = error.get("code")
                if code in _RETRYABLE_RPC_CODES:
                    last_error = RpcError(method, str(error.get("message")), code)
                    await self._sleep(self._backoff(attempt))
                    continue
                raise RpcError(method, str(error.get("message")), code)
            return data.get("result")
        raise RpcError(method, f"sin éxito tras {self._max_attempts} intentos: {last_error}")

    # --- Métodos usados por el sistema ----------------------------------------

    async def get_signatures_for_address(
        self,
        address: str,
        *,
        before: str | None = None,
        until: str | None = None,
        limit: int = 1000,
        commitment: str = "confirmed",
    ) -> list[dict[str, Any]]:
        opts: dict[str, Any] = {"limit": limit, "commitment": commitment}
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        return await self.call("getSignaturesForAddress", [address, opts]) or []

    async def get_transaction(
        self, signature: str, commitment: str = "confirmed"
    ) -> dict[str, Any] | None:
        """Transacción en ``jsonParsed``; ``None`` si el nodo aún no la tiene."""
        return await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 1,
                    "commitment": commitment,
                },
            ],
        )

    async def get_account_info(self, address: str) -> dict[str, Any] | None:
        result = await self.call("getAccountInfo", [address, {"encoding": "jsonParsed"}])
        return (result or {}).get("value")

    async def get_slot(self, commitment: str = "confirmed") -> int:
        return int(await self.call("getSlot", [{"commitment": commitment}]))

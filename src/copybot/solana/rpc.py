"""Cliente JSON-RPC de Solana de solo lectura.

Incluye límite de peticiones por segundo, timeouts, reintentos con espera
exponencial (respetando ``Retry-After``), contabilidad de uso por método para el
control de gasto y un control de presupuesto opcional que se consulta **antes** de
cada petición. La URL puede contener la clave del proveedor, así que nunca se
registra.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

# Errores JSON-RPC que indican que conviene reintentar (nodo atrasado o saturado).
_RETRYABLE_RPC_CODES = {-32005, -32007, -32014, -32016, 429}

# Tarifa de Helius, contrastada con su panel en la Fase 3 (89.142 créditos reales frente
# a ~92.000 estimados con este modelo):
# * getTransactionsForAddress con transacciones completas: 10 créditos por cada 100
#   transacciones devueltas (una página llena de 1.000 cuesta 100);
# * getTransactionsForAddress solo con firmas: 10 por petición;
# * el resto de métodos, 1 por petición;
# * las peticiones rechazadas (429, 5xx o error de red) no se cobran.
# Para el control de presupuesto se reserva antes el máximo posible de la petición.
HELIUS_CREDITS_PER_REQUEST = {"getTransactionsForAddress": 100}
GTFA_SIGNATURES_CREDITS = 10
GTFA_CREDITS_PER_100_TRANSACTIONS = 10


def gtfa_full_credits(result: Any) -> int:
    """Créditos de una página completa de getTransactionsForAddress según lo devuelto."""
    returned = len((result or {}).get("data") or [])
    return max(1, -(-returned // 100)) * GTFA_CREDITS_PER_100_TRANSACTIONS


# Máximo de transacciones por página de ``getTransactionsForAddress`` (medido: con
# 1001 el nodo responde «You can only request up to 1000 transactions at a time»).
GTFA_MAX_LIMIT = 1000


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


# (método, peticiones, créditos)
UsageCallback = Callable[[str, int, int], Awaitable[None]]
# Se llama antes de cada petición con (método, créditos); lanza una excepción para
# impedirla (p. ej. ``BudgetExceededError``).
BudgetCheck = Callable[[str, int], Awaitable[None]]


@dataclass(frozen=True)
class TransactionPage:
    items: list[dict[str, Any]]
    pagination_token: str | None


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
        budget_check: BudgetCheck | None = None,
        credits_per_request: Mapping[str, int] | None = None,
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
        self._budget_check = budget_check
        self._credits = dict(credits_per_request or {})
        self._sleep = sleep
        self._ids = itertools.count(1)
        self.usage: Counter[str] = Counter()
        self.credits: Counter[str] = Counter()

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

    async def call(
        self,
        method: str,
        params: list[Any],
        *,
        credits: int | None = None,
        credits_for_result: Callable[[Any], int] | None = None,
    ) -> Any:
        """``credits`` sustituye a la tarifa del método (es lo que se reserva en el control
        de presupuesto); ``credits_for_result`` calcula el coste real a partir de la
        respuesta, cuando depende de lo devuelto."""
        last_error: Exception | None = None
        cost = self._credits.get(method, 1)
        if credits is not None and method in self._credits:
            cost = credits
        for attempt in range(self._max_attempts):
            if self._budget_check is not None:
                await self._budget_check(method, cost)
            await self._limiter.acquire()
            self.usage[method] += 1
            body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
            try:
                response = await self._client.post(self._url, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                await self._account(method, 0)
                last_error = exc
                delay = self._backoff(attempt)
                log.warning("rpc_transport_error", method=method, error=type(exc).__name__)
                await self._sleep(delay)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                await self._account(method, 0)  # rechazada: no se cobra
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else self._backoff(attempt)
                delay = min(delay, self._backoff_max)
                last_error = RpcError(method, f"HTTP {response.status_code}", response.status_code)
                log.warning("rpc_http_retry", method=method, status=response.status_code)
                await self._sleep(delay)
                continue
            if response.status_code != 200:
                await self._account(method, 0)
                raise RpcError(method, f"HTTP {response.status_code}", response.status_code)
            data = response.json()
            error = data.get("error")
            charged = cost
            if credits_for_result is not None and not error:
                charged = credits_for_result(data.get("result"))
            await self._account(method, charged)
            if error:
                code = error.get("code")
                if code in _RETRYABLE_RPC_CODES:
                    last_error = RpcError(method, str(error.get("message")), code)
                    await self._sleep(self._backoff(attempt))
                    continue
                raise RpcError(method, str(error.get("message")), code)
            return data.get("result")
        raise RpcError(method, f"sin éxito tras {self._max_attempts} intentos: {last_error}")

    async def _account(self, method: str, credits: int) -> None:
        self.credits[method] += credits
        if self._on_usage is not None:
            await self._on_usage(method, 1, credits)

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

    async def get_transactions_for_address(
        self,
        address: str,
        *,
        full: bool,
        limit: int = GTFA_MAX_LIMIT,
        sort_order: str = "desc",
        block_time_gte: int | None = None,
        block_time_lt: int | None = None,
        pagination_token: str | None = None,
        associated_accounts: str = "balanceChanged",
        only_succeeded: bool = True,
    ) -> TransactionPage:
        """Una página del historial de ``address`` (método propio de Helius).

        Con ``full`` cada elemento es la transacción en ``jsonParsed`` más
        ``transactionIndex`` (posición dentro del bloque); si no, solo firmas.
        ``associated_accounts="balanceChanged"`` incluye las transacciones de sus cuentas de
        token en las que cambia el saldo (p. ej. tokens recibidos sin que la wallet
        firme). La página siguiente se pide con ``pagination_token``; cuando no hay
        más, el nodo devuelve una página con menos elementos que ``limit``.
        """
        if not 1 <= limit <= GTFA_MAX_LIMIT:
            raise ValueError(f"limit debe estar en [1, {GTFA_MAX_LIMIT}]")
        if sort_order not in ("asc", "desc"):
            raise ValueError("sort_order debe ser 'asc' o 'desc'")
        filters: dict[str, Any] = {"tokenAccounts": associated_accounts}
        if only_succeeded:
            filters["status"] = "succeeded"
        block_time: dict[str, int] = {}
        if block_time_gte is not None:
            block_time["gte"] = block_time_gte
        if block_time_lt is not None:
            block_time["lt"] = block_time_lt
        if block_time:
            filters["blockTime"] = block_time
        opts: dict[str, Any] = {
            "transactionDetails": "full" if full else "signatures",
            "limit": limit,
            "sortOrder": sort_order,
            "filters": filters,
        }
        if full:
            opts["encoding"] = "jsonParsed"
            opts["maxSupportedTransactionVersion"] = 1
        if pagination_token:
            opts["paginationToken"] = pagination_token
        result = (
            await self.call(
                "getTransactionsForAddress",
                [address, opts],
                credits=GTFA_CREDITS_PER_100_TRANSACTIONS * -(-limit // 100)
                if full
                else GTFA_SIGNATURES_CREDITS,
                credits_for_result=gtfa_full_credits if full else None,
            )
            or {}
        )
        items = list(result.get("data") or [])
        token = result.get("paginationToken") if len(items) == limit else None
        return TransactionPage(items, token or None)

    async def get_account_info(self, address: str) -> dict[str, Any] | None:
        result = await self.call("getAccountInfo", [address, {"encoding": "jsonParsed"}])
        return (result or {}).get("value")

    async def get_slot(self, commitment: str = "confirmed") -> int:
        return int(await self.call("getSlot", [{"commitment": commitment}]))

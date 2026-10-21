"""Datos públicos de mercado de Binance (solo lectura).

Solo se usan los endpoints públicos de mercado: pares negociables, velas y estadísticas
de 24 h. No hay clave API ni firma: los endpoints de órdenes y de cuenta están
prohibidos en el código (test de «solo simulación»).

Se pide primero al espejo de solo datos de mercado (``data-api.binance.vision``) y, si
no responde, a ``api.binance.com``. Binance limita por «peso» (6.000 por minuto e IP):
cada respuesta dice cuánto se ha gastado en el minuto y, al acercarse al límite, se
espera al minuto siguiente. Un 418 significa que la IP está vetada temporalmente: se
para y no se insiste.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog

from copybot.solana.rpc import RateLimiter

log = structlog.get_logger(__name__)

BASE_URLS = ("https://data-api.binance.vision", "https://api.binance.com")
KLINES_LIMIT = 1000
WEIGHT_LIMIT = 6000
WEIGHT_MARGIN = 1000  # se para antes de llegar al límite del minuto
INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


class BinanceError(RuntimeError):
    pass


class BinanceBannedError(BinanceError):
    """418: Binance ha vetado la IP un tiempo por pasarse del límite."""


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    base: str
    quote: str
    status: str  # TRADING, BREAK, END…
    spot: bool  # se puede negociar al contado


@dataclass(frozen=True)
class Kline:
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal  # en la moneda base
    quote_volume: Decimal  # en la moneda de cotización (USDT…)
    trades: int


@dataclass(frozen=True)
class Ticker24h:
    symbol: str
    last_price: Decimal
    quote_volume: Decimal
    change_pct: Decimal
    trades: int


@dataclass(frozen=True)
class BookTicker:
    symbol: str
    bid: Decimal
    ask: Decimal

    @property
    def spread_pct(self) -> Decimal | None:
        if self.bid <= 0 or self.ask <= 0:
            return None
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 100


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def parse_kline(row: list[Any]) -> Kline:
    k = Kline(
        open_time=_dt(int(row[0])),
        open=Decimal(row[1]),
        high=Decimal(row[2]),
        low=Decimal(row[3]),
        close=Decimal(row[4]),
        volume=Decimal(row[5]),
        close_time=_dt(int(row[6])),
        quote_volume=Decimal(row[7]),
        trades=int(row[8]),
    )
    if not (k.low <= min(k.open, k.close) and max(k.open, k.close) <= k.high and k.low >= 0):
        raise BinanceError(f"vela incoherente: {row[:5]}")
    return k


class BinanceMarket:
    def __init__(
        self,
        *,
        base_urls: tuple[str, ...] = BASE_URLS,
        rate_per_second: float = 8.0,
        max_attempts: int = 5,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._urls = base_urls
        self._limiter = RateLimiter(rate_per_second, sleep=sleep)
        self._attempts = max_attempts
        self._client = client or httpx.AsyncClient(timeout=30)
        self._owns_client = client is None
        self._sleep = sleep
        self._clock = clock
        self.requests = 0
        self.weight_used = 0  # último peso del minuto que dijo Binance

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> BinanceMarket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- Petición genérica ----------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last: str = "sin respuesta"
        for attempt in range(self._attempts):
            for base in self._urls:
                await self._limiter.acquire()
                if self.weight_used >= WEIGHT_LIMIT - WEIGHT_MARGIN:
                    now = self._clock()
                    wait = 61 - now.second
                    log.info("binance_espera_limite_peso", segundos=wait)
                    await self._sleep(wait)
                    self.weight_used = 0
                self.requests += 1
                try:
                    response = await self._client.get(f"{base}{path}", params=params)
                except httpx.HTTPError as exc:
                    last = f"{base}: {type(exc).__name__}"
                    continue
                used = response.headers.get("x-mbx-used-weight-1m")
                if used and used.isdigit():
                    self.weight_used = int(used)
                if response.status_code == 418:
                    raise BinanceBannedError(
                        f"Binance ha vetado la IP temporalmente (418): {response.text[:120]}"
                    )
                if response.status_code == 451:
                    # Binance no sirve a la región de esta IP: se prueba la otra dirección.
                    last = f"{base}: 451 (no disponible desde esta ubicación)"
                    continue
                if response.status_code == 429:
                    retry = response.headers.get("Retry-After")
                    await self._sleep(float(retry) if retry else 30.0)
                    last = f"{base}: 429"
                    continue
                if response.status_code >= 500:
                    last = f"{base}: {response.status_code}"
                    continue
                try:
                    data = response.json()
                except ValueError:
                    last = f"{base}: {response.status_code} sin JSON ({response.text[:80]!r})"
                    continue
                if response.status_code != 200:
                    raise BinanceError(f"Binance respondió {response.status_code}: {data}")
                return data
            await self._sleep(2.0 * (attempt + 1))
        raise BinanceError(f"Binance sin respuesta tras {self._attempts} intentos ({last})")

    # --- Endpoints ------------------------------------------------------------------

    async def exchange_info(self) -> list[SymbolInfo]:
        """Todos los pares, también los que ya no se negocian (estado BREAK o END)."""
        data = await self._get("/api/v3/exchangeInfo", {"showPermissionSets": "false"})
        out = []
        for s in data.get("symbols") or []:
            spot = bool(s.get("isSpotTradingAllowed"))
            out.append(
                SymbolInfo(
                    symbol=s["symbol"],
                    base=s["baseAsset"],
                    quote=s["quoteAsset"],
                    status=s.get("status", "?"),
                    spot=spot,
                )
            )
        return out

    async def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = KLINES_LIMIT,
    ) -> list[Kline]:
        """Una página de velas (como mucho ``limit``), de la más antigua a la más nueva."""
        if interval not in INTERVAL_MS:
            raise ValueError(f"intervalo no soportado: {interval}")
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start is not None:
            params["startTime"] = _ms(start)
        if end is not None:
            params["endTime"] = _ms(end)
        rows = await self._get("/api/v3/klines", params)
        return [parse_kline(r) for r in rows]

    async def klines_range(
        self, symbol: str, interval: str, start: datetime, end: datetime
    ) -> list[Kline]:
        """Todas las velas **cerradas** con apertura en ``[start, end)``, paginando."""
        step = INTERVAL_MS[interval]
        now_ms = _ms(self._clock())
        out: list[Kline] = []
        cursor = _ms(start)
        stop = _ms(end)
        while cursor < stop:
            page = await self.klines(
                symbol, interval, start=_dt(cursor), end=_dt(stop - 1), limit=KLINES_LIMIT
            )
            if not page:
                break
            for k in page:
                closed = _ms(k.close_time) < now_ms
                if _ms(k.open_time) < stop and closed:
                    out.append(k)
            nxt = _ms(page[-1].open_time) + step
            if nxt <= cursor or len(page) < KLINES_LIMIT:
                break
            cursor = nxt
        return out

    async def tickers_24h(self) -> list[Ticker24h]:
        data = await self._get("/api/v3/ticker/24hr")
        return [
            Ticker24h(
                symbol=t["symbol"],
                last_price=Decimal(t["lastPrice"]),
                quote_volume=Decimal(t["quoteVolume"]),
                change_pct=Decimal(t["priceChangePercent"]),
                trades=int(t.get("count") or 0),
            )
            for t in data
        ]

    async def book_tickers(self) -> list[BookTicker]:
        data = await self._get("/api/v3/ticker/bookTicker")
        return [
            BookTicker(symbol=t["symbol"], bid=Decimal(t["bidPrice"]), ask=Decimal(t["askPrice"]))
            for t in data
        ]

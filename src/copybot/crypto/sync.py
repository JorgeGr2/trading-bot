"""Descarga de pares y velas de Binance a la base de datos (idempotente).

Repetirla solo pide lo que falta: cada par recuerda hasta dónde tiene sus velas. Los
pares retirados se guardan con su historial (hasta el día en que dejaron de cotizar),
para que el backtest no vea solo a los supervivientes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.crypto.binance import (
    INTERVAL_MS,
    INVALID_SYMBOL,
    BinanceError,
    BinanceMarket,
    Kline,
    SymbolInfo,
)
from copybot.db.models import MarketBar, MarketSymbol

log = structlog.get_logger(__name__)

EXCHANGE = "binance"
HISTORY_START = datetime(2017, 7, 1, tzinfo=UTC)
DISAPPEARED = "DESAPARECIDO"  # ya no sale en la lista de pares de Binance
# Monedas estables y fiat como moneda base: no son «cripto en la que invertir».
STABLE_BASES = frozenset(
    {
        "USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "USDS", "USDE",
        "AEUR", "EUR", "EURI", "GBP", "AUD", "BRL", "TRY", "UAH", "RUB", "BIDR", "IDRT",
        "NGN", "ZAR", "SUSD", "UST", "USTC", "XUSD", "BFUSD", "PYUSD", "RLUSD", "U",
    }
)  # fmt: skip
# Tokens apalancados de Binance (todos retirados en 2022): BTCUP, ETHDOWN, BNBBULL…
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def leveraged(info: SymbolInfo, bases: frozenset[str]) -> bool:
    """Token apalancado: otra moneda conocida más UP/DOWN/BULL/BEAR, y ya retirado (así
    no se confunde con monedas de verdad como JUP)."""
    if info.status == "TRADING":
        return False
    return any(
        info.base.endswith(suffix) and info.base[: -len(suffix)] in bases
        for suffix in LEVERAGED_SUFFIXES
    )


def investable(info: SymbolInfo, bases: frozenset[str], quote: str = "USDT") -> bool:
    """Par contra ``quote`` de una cripto (no estable, fiat ni apalancada)."""
    if info.quote != quote or info.base in STABLE_BASES:
        return False
    return not leveraged(info, bases)


@dataclass(frozen=True)
class SyncResult:
    symbols: int
    new_symbols: int
    updated_symbols: int
    bars: int
    requests: int


async def sync_symbols(
    sessions: async_sessionmaker[AsyncSession],
    infos: Iterable[SymbolInfo],
    now: datetime,
) -> int:
    """Guarda o actualiza los pares (estado y filtros); devuelve cuántos son nuevos. Los
    pares conocidos que ya no salen en Binance pasan a ``DESAPARECIDO``."""
    infos = list(infos)
    seen = {i.symbol for i in infos}
    async with sessions.begin() as s:
        known = {
            m.symbol: m
            for m in await s.scalars(select(MarketSymbol).where(MarketSymbol.exchange == EXCHANGE))
        }
        for info in infos:
            row = known.get(info.symbol)
            if row is None:
                s.add(
                    MarketSymbol(
                        exchange=EXCHANGE,
                        symbol=info.symbol,
                        base=info.base,
                        quote=info.quote,
                        status=info.status,
                        spot=info.spot,
                        first_seen_at=now,
                        last_seen_at=now,
                        listing_source="api",
                        tick_size=info.tick_size,
                        step_size=info.step_size,
                        min_notional=info.min_notional,
                        filters_updated_at=now,
                    )
                )
                continue
            if row.status != info.status:
                row.status, row.status_changed_at = info.status, now
            row.spot = info.spot
            row.last_seen_at = now
            row.tick_size, row.step_size = info.tick_size, info.step_size
            row.min_notional, row.filters_updated_at = info.min_notional, now
        for symbol, row in known.items():
            if symbol not in seen and row.status != DISAPPEARED:
                row.status, row.status_changed_at = DISAPPEARED, now
    return sum(1 for symbol in seen if symbol not in known)


async def store_bars(
    sessions: async_sessionmaker[AsyncSession], symbol: str, interval: str, bars: list[Kline]
) -> None:
    if not bars:
        return
    rows = [
        {
            "exchange": EXCHANGE,
            "symbol": symbol,
            "interval": interval,
            "open_time": k.open_time,
            "open": k.open,
            "high": k.high,
            "low": k.low,
            "close": k.close,
            "volume": k.volume,
            "quote_volume": k.quote_volume,
            "trades": k.trades,
        }
        for k in bars
    ]
    async with sessions.begin() as s:
        for i in range(0, len(rows), 1000):
            await s.execute(insert(MarketBar).values(rows[i : i + 1000]).on_conflict_do_nothing())


async def _first_bar(sessions: async_sessionmaker[AsyncSession], symbol: str) -> datetime | None:
    async with sessions() as s:
        return await s.scalar(
            select(func.min(MarketBar.open_time)).where(
                MarketBar.exchange == EXCHANGE, MarketBar.symbol == symbol
            )
        )


async def sync_bars(
    sessions: async_sessionmaker[AsyncSession],
    market: BinanceMarket,
    *,
    quote: str = "USDT",
    interval: str = "1d",
    since: datetime = HISTORY_START,
    now: datetime | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[int, int]:
    """Velas diarias de todos los pares invertibles contra ``quote``. Devuelve (pares
    actualizados, velas nuevas).

    * Pide lo que falta hacia delante y, si ``since`` es anterior a lo ya pedido, rellena
      hacia atrás.
    * Un par que deja de cotizar se descarga **una vez más** (para no perder los días de
      su caída final) y después se congela (``final_sync_at``).
    * Un par que Binance ya no reconoce (−1121) se congela sin parar la descarga.
    """
    if interval != "1d":
        raise ValueError("de momento solo se guardan velas diarias")
    now = now or datetime.now(UTC)
    step = timedelta(milliseconds=INTERVAL_MS[interval])
    async with sessions() as s:
        symbols = list(
            await s.scalars(
                select(MarketSymbol)
                .where(MarketSymbol.exchange == EXCHANGE, MarketSymbol.quote == quote)
                .order_by(MarketSymbol.symbol)
            )
        )
    bases = frozenset(m.base for m in symbols)
    todo = [
        m
        for m in symbols
        if investable(SymbolInfo(m.symbol, m.base, m.quote, m.status, m.spot), bases, quote)
        and m.final_sync_at is None
    ]
    added = updated = 0
    for i, m in enumerate(todo):
        requested_from = m.history_requested_from
        if requested_from is None and m.bars_synced_to is not None:
            requested_from = await _first_bar(sessions, m.symbol) or since
        ranges = []
        if requested_from is None:
            ranges.append((since, now))
        else:
            if since < requested_from:
                ranges.append((since, requested_from))  # relleno hacia atrás
            forward = (m.bars_synced_to + step) if m.bars_synced_to else requested_from
            if forward + step <= now:
                ranges.append((forward, now))
        bars: list[Kline] = []
        gone = False
        try:
            for lo, hi in ranges:
                bars += await market.klines_range(m.symbol, interval, lo, hi)
        except BinanceError as exc:
            if exc.code != INVALID_SYMBOL:
                raise
            gone = True
            log.info("binance_par_inexistente", par=m.symbol)
        await store_bars(sessions, m.symbol, interval, bars)
        async with sessions.begin() as s:
            row = await s.get(MarketSymbol, (EXCHANGE, m.symbol))
            if row is not None:
                if bars:
                    last = max(k.open_time for k in bars)
                    if row.bars_synced_to is None or last > row.bars_synced_to:
                        row.bars_synced_to = last
                row.history_requested_from = min(since, requested_from or since)
                if gone or row.status != "TRADING":
                    row.final_sync_at = now  # última descarga tras dejar de cotizar
        if bars:
            added += len(bars)
            updated += 1
        if progress is not None:
            progress(i + 1, len(todo), m.symbol)
    return updated, added


async def sync_all(
    sessions: async_sessionmaker[AsyncSession],
    market: BinanceMarket,
    *,
    quote: str = "USDT",
    since: datetime = HISTORY_START,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    progress: Callable[[int, int, str], None] | None = None,
) -> SyncResult:
    infos = await market.exchange_info()
    now = clock()
    new = await sync_symbols(sessions, infos, now)
    updated, bars = await sync_bars(
        sessions, market, quote=quote, since=since, now=now, progress=progress
    )
    log.info("binance_sincronizado", pares=len(infos), nuevos=new, velas=bars)
    return SyncResult(len(infos), new, updated, bars, market.requests)

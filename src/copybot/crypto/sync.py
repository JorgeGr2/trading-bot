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
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.crypto.binance import INTERVAL_MS, BinanceMarket, Kline, SymbolInfo
from copybot.db.models import MarketBar, MarketSymbol

log = structlog.get_logger(__name__)

EXCHANGE = "binance"
HISTORY_START = datetime(2019, 1, 1, tzinfo=UTC)
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
    """Guarda o actualiza los pares; devuelve cuántos son nuevos."""
    infos = list(infos)
    async with sessions() as s:
        known = set(
            await s.scalars(select(MarketSymbol.symbol).where(MarketSymbol.exchange == EXCHANGE))
        )
    async with sessions.begin() as s:
        for info in infos:
            stmt = insert(MarketSymbol).values(
                exchange=EXCHANGE,
                symbol=info.symbol,
                base=info.base,
                quote=info.quote,
                status=info.status,
                spot=info.spot,
                first_seen_at=now,
                last_seen_at=now,
            )
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["exchange", "symbol"],
                    set_={"status": info.status, "spot": info.spot, "last_seen_at": now},
                )
            )
    return sum(1 for i in infos if i.symbol not in known)


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
    actualizados, velas nuevas). Un par retirado ya descargado no se vuelve a pedir."""
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
        and not (m.status != "TRADING" and m.bars_synced_to is not None)
    ]
    added = updated = 0
    for i, m in enumerate(todo):
        start = since if m.bars_synced_to is None else m.bars_synced_to + step
        if start + step > now:
            continue
        bars = await market.klines_range(m.symbol, interval, start, now)
        await store_bars(sessions, m.symbol, interval, bars)
        if bars:
            async with sessions.begin() as s:
                row = await s.get(MarketSymbol, (EXCHANGE, m.symbol))
                if row is not None:
                    row.bars_synced_to = bars[-1].open_time
            added += len(bars)
            updated += 1
        elif m.status != "TRADING" and m.bars_synced_to is None:
            # Retirado sin velas en el periodo: se marca para no volver a pedirlo.
            async with sessions.begin() as s:
                row = await s.get(MarketSymbol, (EXCHANGE, m.symbol))
                if row is not None:
                    row.bars_synced_to = since
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

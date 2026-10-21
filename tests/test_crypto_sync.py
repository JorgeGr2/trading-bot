"""Descarga de pares y velas a la base de datos (Binance simulado con respx)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import func, select

from copybot.crypto.binance import BASE_URLS, BinanceMarket, SymbolInfo
from copybot.crypto.sync import investable, sync_all
from copybot.db.models import MarketBar, MarketSymbol
from copybot.db.session import make_sessionmaker

MIRROR = BASE_URLS[0]
DAY = 86_400_000
NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)
TODAY_MS = int(NOW.timestamp() * 1000) // DAY * DAY


def info(symbol: str, base: str, status: str = "TRADING", quote: str = "USDT") -> dict:
    return {
        "symbol": symbol,
        "baseAsset": base,
        "quoteAsset": quote,
        "status": status,
        "isSpotTradingAllowed": status == "TRADING",
    }


def kline(open_ms: int) -> list:
    return [open_ms, "1", "2", "0.5", "1.5", "10", open_ms + DAY - 1, "15", 7, "5", "7", "0"]


def test_investable_pairs():
    bases = frozenset({"BTC", "BTCUP", "JUP", "USDC", "J"})
    assert investable(SymbolInfo("BTCUSDT", "BTC", "USDT", "TRADING", True), bases)
    assert investable(SymbolInfo("JUPUSDT", "JUP", "USDT", "TRADING", True), bases)
    assert not investable(SymbolInfo("BTCUPUSDT", "BTCUP", "USDT", "BREAK", False), bases)
    assert not investable(SymbolInfo("USDCUSDT", "USDC", "USDT", "TRADING", True), bases)
    assert not investable(SymbolInfo("BTCEUR", "BTC", "EUR", "TRADING", True), bases)


@pytest.mark.postgres
@respx.mock
async def test_sync_downloads_only_what_is_missing(engine):
    respx.get(f"{MIRROR}/api/v3/exchangeInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "symbols": [
                    info("BTCUSDT", "BTC"),
                    info("LUNAUSDT", "LUNA", status="BREAK"),
                    info("USDCUSDT", "USDC"),
                    info("BTCEUR", "BTC", quote="EUR"),
                ]
            },
        )
    )
    asked: list[tuple[str, int]] = []

    def klines(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["symbol"]
        start = int(request.url.params["startTime"])
        asked.append((symbol, start))
        last = TODAY_MS if symbol == "BTCUSDT" else TODAY_MS - 100 * DAY  # LUNA, retirada
        return httpx.Response(200, json=[kline(t) for t in range(start, last + 1, DAY)][:1000])

    respx.get(f"{MIRROR}/api/v3/klines").mock(side_effect=klines)
    sessions = make_sessionmaker(engine)
    since = datetime(2026, 1, 1, tzinfo=UTC)

    async def no_sleep(_s: float) -> None:
        return None

    market = BinanceMarket(sleep=no_sleep, clock=lambda: NOW, rate_per_second=1000)
    first = await sync_all(sessions, market, since=since, clock=lambda: NOW)
    assert first.symbols == 4 and first.new_symbols == 4
    assert first.updated_symbols == 2  # BTC y LUNA (USDC y el par en EUR no interesan)
    assert {s for s, _ in asked} == {"BTCUSDT", "LUNAUSDT"}
    async with sessions() as s:
        n = await s.scalar(select(func.count()).select_from(MarketBar))
        btc = await s.get(MarketSymbol, ("binance", "BTCUSDT"))
    assert n == first.bars
    assert btc.bars_synced_to == datetime(2026, 10, 20, tzinfo=UTC)  # la de hoy sigue abierta
    # Al día siguiente solo se pide la vela nueva de BTC; LUNA, retirada, ya no se pide.
    asked.clear()
    later = datetime(2026, 10, 22, 12, tzinfo=UTC)
    market2 = BinanceMarket(sleep=no_sleep, clock=lambda: later, rate_per_second=1000)
    second = await sync_all(sessions, market2, since=since, clock=lambda: later)
    assert asked == [("BTCUSDT", TODAY_MS)] and second.bars == 1 and second.new_symbols == 0
    await market.aclose()
    await market2.aclose()

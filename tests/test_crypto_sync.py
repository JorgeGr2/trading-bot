"""Descarga de pares y velas a la base de datos (Binance simulado con respx)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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
    luna = None
    async with sessions() as s:
        luna = await s.get(MarketSymbol, ("binance", "LUNAUSDT"))
    assert luna.final_sync_at == NOW  # retirada: descargada una vez y congelada
    # Al día siguiente solo se pide la vela nueva de BTC; LUNA, retirada, ya no se pide.
    asked.clear()
    later = datetime(2026, 10, 22, 12, tzinfo=UTC)
    market2 = BinanceMarket(sleep=no_sleep, clock=lambda: later, rate_per_second=1000)
    second = await sync_all(sessions, market2, since=since, clock=lambda: later)
    assert asked == [("BTCUSDT", TODAY_MS)] and second.bars == 1 and second.new_symbols == 0
    await market.aclose()
    await market2.aclose()


def _info_response(*symbols: dict) -> httpx.Response:
    return httpx.Response(200, json={"symbols": list(symbols)})


async def _no_sleep(_s: float) -> None:
    return None


def _market(now: datetime) -> BinanceMarket:
    return BinanceMarket(sleep=_no_sleep, clock=lambda: now, rate_per_second=1000)


@pytest.mark.postgres
@respx.mock
async def test_delisting_keeps_the_final_days_and_missing_pairs_do_not_abort(engine):
    """S1: un par retirado entre dos descargas conserva sus últimas velas. S2: un par que
    ya no existe (−1121) no para la descarga de los demás."""
    sessions = make_sessionmaker(engine)
    since = datetime(2026, 10, 1, tzinfo=UTC)
    day1 = datetime(2026, 10, 10, 12, tzinfo=UTC)
    day5 = datetime(2026, 10, 14, 12, tzinfo=UTC)
    delist_ms = int(datetime(2026, 10, 13, tzinfo=UTC).timestamp() * 1000)
    info_route = respx.get(f"{MIRROR}/api/v3/exchangeInfo")
    info_route.mock(return_value=_info_response(info("AAAUSDT", "AAA"), info("GONEUSDT", "GONE")))

    def klines(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["symbol"]
        start = int(request.url.params["startTime"])
        end = int(request.url.params["endTime"])
        if symbol == "GONEUSDT" and end > int(day1.timestamp() * 1000):
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
        last = min(end, delist_ms - DAY) if symbol == "AAAUSDT" else end
        return httpx.Response(200, json=[kline(t) for t in range(start, last + 1, DAY)])

    respx.get(f"{MIRROR}/api/v3/klines").mock(side_effect=klines)
    await sync_all(sessions, _market(day1), since=since, clock=lambda: day1)
    # Día 5: AAA pasa a BREAK (dejó de cotizar el 13) y GONE ya no sale en la lista.
    info_route.mock(return_value=_info_response(info("AAAUSDT", "AAA", status="BREAK")))
    result = await sync_all(sessions, _market(day5), since=since, clock=lambda: day5)
    assert result.bars == 3  # días 10, 11 y 12 de AAA
    async with sessions() as s:
        aaa = await s.get(MarketSymbol, ("binance", "AAAUSDT"))
        gone = await s.get(MarketSymbol, ("binance", "GONEUSDT"))
    assert aaa.bars_synced_to == datetime(2026, 10, 12, tzinfo=UTC)
    assert aaa.final_sync_at == day5 and aaa.status_changed_at == day5
    assert gone.status == "DESAPARECIDO" and gone.final_sync_at == day5


@pytest.mark.postgres
@respx.mock
async def test_backfill_when_since_moves_earlier_and_filters_are_saved(engine):
    """S3 y S4: ampliar el inicio rellena hacia atrás sin duplicar, y se guardan filtros."""
    sessions = make_sessionmaker(engine)
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    symbol = info("AAAUSDT", "AAA")
    symbol["filters"] = [
        {"filterType": "PRICE_FILTER", "tickSize": "0.0100"},
        {"filterType": "LOT_SIZE", "stepSize": "0.00100"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00000000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00"},
    ]
    respx.get(f"{MIRROR}/api/v3/exchangeInfo").mock(return_value=_info_response(symbol))

    def klines(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startTime"])
        end = int(request.url.params["endTime"])
        first = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)  # listado el 1-sep
        return httpx.Response(
            200, json=[kline(t) for t in range(max(start, first), end + 1, DAY)][:1000]
        )

    respx.get(f"{MIRROR}/api/v3/klines").mock(side_effect=klines)
    await sync_all(sessions, _market(now), since=datetime(2026, 10, 1, tzinfo=UTC))
    again = await sync_all(sessions, _market(now), since=datetime(2026, 8, 1, tzinfo=UTC))
    async with sessions() as s:
        n = await s.scalar(select(func.count()).select_from(MarketBar))
        aaa = await s.get(MarketSymbol, ("binance", "AAAUSDT"))
    assert again.bars == 30  # del 1 al 30 de septiembre
    assert n == 30 + 9  # sin duplicados
    assert aaa.history_requested_from == datetime(2026, 8, 1, tzinfo=UTC)
    assert (aaa.tick_size, aaa.step_size, aaa.min_notional) == (
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal(5),
    )


@pytest.mark.postgres
@respx.mock
async def test_a_pair_back_to_trading_after_a_break_is_synced_again(engine):
    """T7: un BREAK pasajero congela el par (última descarga), pero si vuelve a TRADING se
    descongela y la descarga sigue desde la última vela guardada."""
    sessions = make_sessionmaker(engine)
    since = datetime(2026, 10, 1, tzinfo=UTC)
    day1 = datetime(2026, 10, 10, 12, tzinfo=UTC)
    day2 = datetime(2026, 10, 12, 12, tzinfo=UTC)
    day3 = datetime(2026, 10, 16, 12, tzinfo=UTC)
    break_from = int(datetime(2026, 10, 11, tzinfo=UTC).timestamp() * 1000)
    back_from = int(datetime(2026, 10, 14, tzinfo=UTC).timestamp() * 1000)
    info_route = respx.get(f"{MIRROR}/api/v3/exchangeInfo")
    asked: list[int] = []

    def klines(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startTime"])
        end = int(request.url.params["endTime"])
        asked.append(start)
        days = [t for t in range(start, end + 1, DAY) if t < break_from or t >= back_from]
        return httpx.Response(200, json=[kline(t) for t in days])

    respx.get(f"{MIRROR}/api/v3/klines").mock(side_effect=klines)
    info_route.mock(return_value=_info_response(info("AAAUSDT", "AAA")))
    await sync_all(sessions, _market(day1), since=since, clock=lambda: day1)
    info_route.mock(return_value=_info_response(info("AAAUSDT", "AAA", status="BREAK")))
    await sync_all(sessions, _market(day2), since=since, clock=lambda: day2)
    async with sessions() as s:
        frozen = await s.get(MarketSymbol, ("binance", "AAAUSDT"))
    assert frozen.final_sync_at == day2
    assert frozen.bars_synced_to == datetime(2026, 10, 10, tzinfo=UTC)
    asked.clear()
    info_route.mock(return_value=_info_response(info("AAAUSDT", "AAA")))
    result = await sync_all(sessions, _market(day3), since=since, clock=lambda: day3)
    async with sessions() as s:
        aaa = await s.get(MarketSymbol, ("binance", "AAAUSDT"))
    assert asked == [int(datetime(2026, 10, 11, tzinfo=UTC).timestamp() * 1000)]
    assert result.bars == 2  # 14 y 15 de octubre
    assert aaa.status == "TRADING" and aaa.final_sync_at is None
    assert aaa.bars_synced_to == datetime(2026, 10, 15, tzinfo=UTC)

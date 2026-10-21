"""Cliente de datos públicos de Binance con respuestas simuladas (formato de su API)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from copybot.crypto.binance import (
    BASE_URLS,
    BinanceBannedError,
    BinanceError,
    BinanceMarket,
    parse_kline,
)

D = Decimal
MIRROR, MAIN = BASE_URLS
DAY = 86_400_000
T0 = 1_788_480_000_000  # 2026-09-04 00:00 UTC, en ms
NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


def row(open_ms: int, close: str = "100", step: int = DAY) -> list:
    return [
        open_ms,
        "99.5",
        "101",
        "99",
        close,
        "12.5",
        open_ms + step - 1,
        "1250.0",
        321,
        "6.0",
        "600.0",
        "0",
    ]


async def no_sleep(_seconds: float) -> None:
    return None


def market(**kw) -> BinanceMarket:
    return BinanceMarket(sleep=no_sleep, clock=lambda: NOW, rate_per_second=1000, **kw)


def test_parse_kline_and_reject_incoherent_ones():
    k = parse_kline(row(T0, close="100.5"))
    assert k.open_time == datetime(2026, 9, 4, tzinfo=UTC)
    assert (k.open, k.high, k.low, k.close) == (D("99.5"), D(101), D(99), D("100.5"))
    assert k.quote_volume == D("1250.0") and k.trades == 321
    bad = row(T0)
    bad[3] = "102"  # mínimo por encima del máximo
    with pytest.raises(BinanceError):
        parse_kline(bad)


@respx.mock
async def test_klines_range_pages_and_drops_the_open_candle():
    calls = []

    def klines(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startTime"])
        calls.append(start)
        last_open = int(NOW.timestamp() * 1000) // DAY * DAY  # la vela de hoy sigue abierta
        rows = [row(t) for t in range(start, min(start + 1000 * DAY, last_open + 1), DAY)]
        return httpx.Response(200, json=rows, headers={"x-mbx-used-weight-1m": "12"})

    respx.get(f"{MIRROR}/api/v3/klines").mock(side_effect=klines)
    async with market() as m:
        start = datetime(2023, 1, 1, tzinfo=UTC)
        bars = await m.klines_range("BTCUSDT", "1d", start, NOW + timedelta(days=1))
    assert bars[0].open_time == start
    assert bars[-1].open_time == datetime(2026, 10, 20, tzinfo=UTC)  # la de hoy no está cerrada
    assert len(calls) == 2  # 1.000 velas por página
    assert len({b.open_time for b in bars}) == len(bars)


@respx.mock
async def test_falls_back_to_the_main_api_and_retries_429():
    respx.get(f"{MIRROR}/api/v3/ticker/24hr").mock(side_effect=httpx.ConnectError("caído"))
    main = respx.get(f"{MAIN}/api/v3/ticker/24hr").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(
                200,
                json=[
                    {
                        "symbol": "ETHUSDT",
                        "lastPrice": "2500.1",
                        "quoteVolume": "123456789.5",
                        "priceChangePercent": "-1.25",
                        "count": 99,
                    }
                ],
            ),
        ]
    )
    async with market() as m:
        (t,) = await m.tickers_24h()
    assert t.symbol == "ETHUSDT" and t.last_price == D("2500.1") and t.change_pct == D("-1.25")
    assert main.call_count == 2


@respx.mock
async def test_ban_stops_immediately():
    respx.get(f"{MIRROR}/api/v3/exchangeInfo").mock(return_value=httpx.Response(418, text="ban"))
    async with market() as m:
        with pytest.raises(BinanceBannedError):
            await m.exchange_info()


@respx.mock
async def test_waits_for_the_next_minute_near_the_weight_limit():
    slept = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    respx.get(f"{MIRROR}/api/v3/ticker/bookTicker").mock(
        return_value=httpx.Response(
            200,
            json=[{"symbol": "SOLUSDT", "bidPrice": "99.9", "askPrice": "100.1"}],
            headers={"x-mbx-used-weight-1m": "5500"},
        )
    )
    m = BinanceMarket(sleep=sleep, clock=lambda: NOW, rate_per_second=1000)
    (b,) = await m.book_tickers()
    assert b.spread_pct == D("0.2")
    await m.book_tickers()
    assert [s for s in slept if s >= 1] == [61]  # 61 − segundo actual (0)
    await m.aclose()


@respx.mock
async def test_exchange_info_keeps_delisted_pairs():
    respx.get(f"{MIRROR}/api/v3/exchangeInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "isSpotTradingAllowed": True,
                    },
                    {
                        "symbol": "LUNAUSDT",
                        "baseAsset": "LUNA",
                        "quoteAsset": "USDT",
                        "status": "BREAK",
                        "isSpotTradingAllowed": False,
                    },
                ]
            },
        )
    )
    async with market() as m:
        info = await m.exchange_info()
    assert [(s.symbol, s.status, s.spot) for s in info] == [
        ("BTCUSDT", "TRADING", True),
        ("LUNAUSDT", "BREAK", False),
    ]

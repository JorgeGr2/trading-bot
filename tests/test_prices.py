from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from copybot.ingest.prices import JUPITER_PRICE_URL, JupiterPrices, PriceRefresher

NOW = datetime(2026, 10, 18, 18, tzinfo=UTC)


class Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@respx.mock
async def test_fetch_batches_retries_and_marks_missing_prices():
    mints = [f"M{i:02d}" for i in range(60)]
    route = respx.get(JUPITER_PRICE_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "4"}),
            httpx.Response(200, json={"M00": {"usdPrice": 0.5, "liquidity": 1000.0}}),
            httpx.Response(200, json={}),
        ]
    )
    sleeper = Sleeper()
    client = JupiterPrices(sleep=sleeper)
    quotes = await client.fetch(mints)
    await client.aclose()
    assert route.call_count == 3  # 50 + 10 tokens, con un reintento
    assert 4.0 in sleeper.delays
    assert quotes["M00"] == (Decimal("0.5"), Decimal("1000.0"))
    assert quotes["M59"] == (None, None)  # Jupiter no lo cotiza


class FakePrices:
    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    async def fetch(self, mints):
        self.asked.append(sorted(mints))
        return {m: (Decimal("0.1"), Decimal(500)) for m in mints}


@pytest.mark.postgres
async def test_prices_never_come_from_after_as_of(repo):
    await repo.save_token_prices(
        {"A": (Decimal(1), Decimal(10))}, observed_at=NOW - timedelta(minutes=30), source="t"
    )
    await repo.save_token_prices(
        {"A": (Decimal(2), Decimal(10))}, observed_at=NOW - timedelta(minutes=5), source="t"
    )
    await repo.save_token_prices(
        {"A": (Decimal(99), Decimal(10))}, observed_at=NOW + timedelta(minutes=1), source="t"
    )
    age = timedelta(hours=2)
    assert await repo.token_prices_at(["A"], as_of=NOW, max_age=age) == {
        "A": (Decimal(2), Decimal(10))
    }
    early = NOW - timedelta(minutes=10)
    assert (await repo.token_prices_at(["A"], as_of=early, max_age=age))["A"][0] == 1
    assert await repo.token_prices_at(["A"], as_of=NOW + timedelta(hours=3), max_age=age) == {}


@pytest.mark.postgres
async def test_refresher_only_asks_for_stale_prices(repo):
    await repo.save_token_prices(
        {"A": (Decimal(1), Decimal(10))}, observed_at=NOW - timedelta(minutes=10), source="t"
    )
    fake = FakePrices()
    refresher = PriceRefresher(fake, repo, fresh_minutes=60, clock=lambda: NOW)
    assert await refresher.refresh(["A", "B"]) == 1
    assert fake.asked == [["B"]]
    assert await refresher.refresh(["A", "B"]) == 0

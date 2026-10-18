from __future__ import annotations

from datetime import UTC, datetime

import pytest

from copybot.budget import BudgetExceededError, CreditBudget

NOW = datetime(2026, 10, 18, tzinfo=UTC)


class Usage:
    def __init__(self, used: int) -> None:
        self.used = used

    async def monthly_credits(self, provider: str, now: datetime) -> int:
        assert provider == "helius"
        return self.used


async def test_blocks_before_exceeding():
    budget = CreditBudget(Usage(999_950), provider="helius", monthly_limit=1_000_000)
    await budget.check("getSlot", 1)
    with pytest.raises(BudgetExceededError, match="quedan 50"):
        await budget.check("getTransactionsForAddress", 100)


def test_limit_must_be_positive():
    with pytest.raises(ValueError):
        CreditBudget(Usage(0), provider="helius", monthly_limit=0)


@pytest.mark.postgres
async def test_monthly_credits_only_counts_this_month(repo, engine):
    from copybot.db.models import ProviderUsage
    from copybot.db.session import make_sessionmaker

    async with make_sessionmaker(engine).begin() as s:
        s.add(
            ProviderUsage(
                day=datetime(2026, 9, 30).date(),
                provider="helius",
                method="x",
                requests=1,
                credits=500,
            )
        )
    await repo.record_usage("helius", "getTransactionsForAddress", 1, 100)
    await repo.record_usage("solana-public", "getTransaction", 1, 1)
    assert await repo.monthly_credits("helius", datetime.now(UTC)) == 100
    budget = CreditBudget(repo, provider="helius", monthly_limit=150)
    with pytest.raises(BudgetExceededError):
        await budget.check("getTransactionsForAddress", 100)

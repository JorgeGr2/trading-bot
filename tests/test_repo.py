from __future__ import annotations

import pytest

from copybot.solana.addresses import InvalidAddressError

pytestmark = pytest.mark.postgres

W = "7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm"


async def test_add_wallet_is_idempotent_and_validated(repo):
    assert await repo.add_wallet(W, source="manual")
    assert not await repo.add_wallet(W, source="manual")
    assert await repo.active_wallets() == [W]
    with pytest.raises(InvalidAddressError):
        await repo.add_wallet("'; DROP TABLE tracked_wallets; --", source="manual")


async def test_checkpoint_never_moves_backwards(repo):
    await repo.set_checkpoint(W, "sig200", 200)
    await repo.set_checkpoint(W, "sig150", 150)
    assert (await repo.get_checkpoint(W)).last_signature == "sig200"
    await repo.set_checkpoint(W, "sig300", 300)
    assert (await repo.get_checkpoint(W)).last_slot == 300


async def test_usage_accumulates(repo, engine):
    from sqlalchemy import select

    from copybot.db.models import ProviderUsage
    from copybot.db.session import make_sessionmaker

    await repo.record_usage("helius", "getTransaction", 1, 1)
    await repo.record_usage("helius", "getTransaction", 2, 2)
    async with make_sessionmaker(engine)() as s:
        row = (await s.scalars(select(ProviderUsage))).one()
    assert (row.requests, row.credits) == (3, 3)


async def test_heartbeat_upsert(repo):
    await repo.heartbeat("ingestor", {"ok": True})
    await repo.heartbeat("ingestor", {"ok": False})

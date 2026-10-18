from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from copybot.db.models import Base
from copybot.db.repo import Repository
from copybot.db.session import make_engine, make_sessionmaker

TEST_DB_URL = os.environ.get(
    "COPYBOT_TEST_DATABASE_URL",
    "postgresql+asyncpg://copybot:copybot_test@localhost:5432/copybot_test",
)


@pytest.fixture
async def engine():
    engine = make_engine(TEST_DB_URL)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL de pruebas no disponible: {exc}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def repo(engine) -> AsyncIterator[Repository]:
    yield Repository(make_sessionmaker(engine))

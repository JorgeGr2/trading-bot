"""Las migraciones crean exactamente el esquema de los modelos y son reversibles."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get(
    "COPYBOT_TEST_MIGRATIONS_URL",
    "postgresql+asyncpg://copybot:copybot_test@localhost:5432/copybot_migrations",
)


def make_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.cmd_opts = type("Opts", (), {"x": [f"url={URL}"]})()
    return cfg


def test_upgrade_matches_models_and_downgrade_is_clean():
    cfg = make_config()
    try:
        command.downgrade(cfg, "base")
    except OSError as exc:
        pytest.skip(f"PostgreSQL de migraciones no disponible: {exc}")
    command.upgrade(cfg, "head")
    # Falla si los modelos tienen cambios sin migración.
    command.check(cfg)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def _sql(statements: list[str]) -> list:
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run() -> list:
        engine = create_async_engine(URL)
        out = []
        async with engine.begin() as conn:
            for stmt in statements:
                result = await conn.execute(text(stmt))
                out.append(result.all() if result.returns_rows else None)
        await engine.dispose()
        return out

    return asyncio.run(run())


def test_downgrade_removes_only_imported_history():
    cfg = make_config()
    try:
        command.downgrade(cfg, "base")
    except OSError as exc:
        pytest.skip(f"PostgreSQL de migraciones no disponible: {exc}")
    command.upgrade(cfg, "head")
    raw = (
        "INSERT INTO raw_transactions (signature, slot, fee_payer, via_fomo, succeeded, source,"
        " detected_via, first_seen_at, fetched_at, payload) VALUES"
        " ('{sig}', 1, 'x', true, true, 'helius', '{via}', now(), now(), {payload})"
    )
    event = (
        "INSERT INTO wallet_events (signature, wallet, kind, confidence, token_delta,"
        " quote_delta, wallet_is_signer, via_fomo, reasons, slot, detected_via, detected_at,"
        " classifier_version) VALUES ('{sig}', 'w', 'buy', 'high', 1, -1, true, true, '[]', 1,"
        " '{via}', now(), '2')"
    )
    _sql(
        [
            raw.format(sig="live1", via="live", payload="'{}'"),
            event.format(sig="live1", via="live"),
            raw.format(sig="hist1", via="history", payload="NULL"),
            event.format(sig="hist1", via="history"),
        ]
    )
    command.downgrade(cfg, "75d7cd1b817a")
    [raws, events] = _sql(
        ["SELECT signature FROM raw_transactions", "SELECT signature FROM wallet_events"]
    )
    assert raws == [("live1",)] and events == [("live1",)]
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

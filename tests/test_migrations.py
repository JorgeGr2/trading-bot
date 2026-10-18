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

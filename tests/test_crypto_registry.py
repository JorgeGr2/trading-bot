"""Registro de backtests (K) y preregistro de la regla."""

from __future__ import annotations

from datetime import date

import pytest

from copybot.crypto.registry import (
    PREREG,
    config_hash,
    config_of,
    expected_hashes,
    k_count,
    prereg_check,
    record_run,
)
from copybot.crypto.strategy import STRATEGY_V1, VARIANTS
from copybot.db.session import make_sessionmaker


def test_config_hash_ignores_set_order():
    from dataclasses import replace

    a = replace(STRATEGY_V1, universe=replace(STRATEGY_V1.universe, exclude=frozenset("AB")))
    b = replace(STRATEGY_V1, universe=replace(STRATEGY_V1.universe, exclude=frozenset("BA")))
    assert config_hash(config_of(a)) == config_hash(config_of(b))
    assert a.params_hash() == b.params_hash()


def test_prereg_detects_a_changed_rule(tmp_path):
    doc = tmp_path / "prereg.md"
    good = "\n".join(f"- Huella {name}: `{value}`" for name, value in expected_hashes().items())
    doc.write_text(good, encoding="utf-8")
    assert prereg_check(doc) == []
    doc.write_text(good.replace(STRATEGY_V1.params_hash(), "0" * 64), encoding="utf-8")
    assert any("regla" in p for p in prereg_check(doc))
    assert prereg_check(tmp_path / "no-existe.md")


def test_repository_prereg_matches_the_code():
    if not PREREG.exists():
        pytest.skip("aún no hay preregistro")
    assert prereg_check() == []


@pytest.mark.postgres
async def test_k_counts_distinct_configurations(engine):
    sessions = make_sessionmaker(engine)
    common = {
        "period": (date(2020, 1, 1), date(2026, 10, 20)),
        "data_hash": "d" * 64,
        "seed": 7,
        "n_days": 2000,
        "sr_daily": 0.01,
        "skew": 0.0,
        "kurt": 3.0,
        "metrics": {},
    }
    assert await k_count(sessions) == 3  # nunca menos que la regla y sus 2 variantes
    for _ in range(2):  # repetir la misma configuración no suma
        await record_run(sessions, cfg=config_of(STRATEGY_V1), role="principal", **common)
    for params in VARIANTS.values():
        await record_run(sessions, cfg=config_of(params), role="variante", **common)
    await record_run(sessions, cfg=config_of(STRATEGY_V1, estres="x2"), role="estres", **common)
    assert await k_count(sessions) == 3  # el estrés no cuenta
    await record_run(sessions, cfg=config_of(STRATEGY_V1, adhoc="n=3"), role="adhoc", **common)
    assert await k_count(sessions) == 4

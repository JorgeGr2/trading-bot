"""Registro obligatorio de backtests y preregistro de la regla.

* Cada ``crypto backtest`` escribe en ``crypto_backtest_runs`` (no hay forma de ejecutarlo
  sin registrar). K, el número de configuraciones probadas, cuenta los hashes distintos
  con papel principal, variante o ad hoc: repetir una configuración no lo sube.
* ``docs/cripto/preregistro-v1.md`` lleva las huellas de la regla y de la tabla de costes;
  ``prereg_check()`` comprueba que el código sigue siendo el registrado.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.crypto import costs
from copybot.crypto.strategy import STRATEGY_V1, STRATEGY_VERSION, VARIANTS, StrategyParams
from copybot.db.models import CryptoBacktestRun

PREREG = Path(__file__).resolve().parents[3] / "docs" / "cripto" / "preregistro-v1.md"
K_ROLES = ("principal", "variante", "adhoc")


def config_of(params: StrategyParams, **extra: object) -> dict:
    cfg = json.loads(json.dumps(asdict(params), default=_canonical))
    cfg.update({k: v for k, v in extra.items() if v is not None})
    return cfg


def _canonical(value: object) -> object:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def code_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parent,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


async def record_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    cfg: dict,
    role: str,
    period: tuple[date, date],
    data_hash: str,
    seed: int,
    n_days: int,
    sr_daily: float | None,
    skew: float | None,
    kurt: float | None,
    metrics: dict,
    report_md: str | None = None,
) -> int:
    row = CryptoBacktestRun(
        strategy_version=STRATEGY_VERSION,
        config_hash=config_hash(cfg),
        config=cfg,
        role=role,
        counts_for_k=role in K_ROLES,
        period_from=period[0],
        period_to=period[1],
        data_hash=data_hash,
        cost_table_hash=costs.cost_table_hash(),
        code_commit=code_commit(),
        seed=seed,
        n_days=n_days,
        sr_daily=sr_daily,
        skew=skew,
        kurt=kurt,
        metrics=json.loads(json.dumps(metrics, default=str)),
        report_md=report_md,
    )
    async with sessions.begin() as s:
        s.add(row)
        await s.flush()
        return row.id


async def k_count(sessions: async_sessionmaker[AsyncSession]) -> int:
    """Configuraciones distintas probadas de esta versión (mínimo, las registradas: 3)."""
    async with sessions() as s:
        n = await s.scalar(
            select(func.count(func.distinct(CryptoBacktestRun.config_hash))).where(
                CryptoBacktestRun.strategy_version == STRATEGY_VERSION,
                CryptoBacktestRun.counts_for_k.is_(True),
            )
        )
    return max(int(n or 0), 1 + len(VARIANTS))


def expected_hashes() -> dict[str, str]:
    return {
        "regla": STRATEGY_V1.params_hash(),
        "costes": costs.cost_table_hash(),
        **{f"variante {k}": v.params_hash() for k, v in VARIANTS.items()},
    }


def prereg_check(path: Path = PREREG) -> list[str]:
    """Diferencias entre las huellas del preregistro y las del código (vacía: coinciden)."""
    if not path.exists():
        return [f"no existe el preregistro {path}"]
    text = path.read_text(encoding="utf-8")
    problems = []
    for name, value in expected_hashes().items():
        m = re.search(rf"^- Huella {re.escape(name)}: `([0-9a-f]{{64}})`", text, re.MULTILINE)
        if m is None:
            problems.append(f"falta la huella «{name}» en el preregistro")
        elif m.group(1) != value:
            problems.append(f"la huella «{name}» del código ({value[:12]}…) no es la registrada")
    return problems

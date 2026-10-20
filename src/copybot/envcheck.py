"""Comprobación de las variables ``COPYBOT_*`` al arrancar.

pydantic-settings ignora en silencio una variable de entorno con una errata (por ejemplo
``COPYBOT_RISK_MAX_OPEN_POSITONS``): el límite quedaría en su valor por defecto sin que
nadie se enterase. Aquí se rechaza cualquier ``COPYBOT_*`` del entorno o de ``.env`` que
no corresponda a ninguna opción conocida.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from pydantic_settings import BaseSettings

from copybot.analysis.metrics import AnalysisParams
from copybot.analysis.scoring import ScoringParams
from copybot.config import RiskLimits, Settings
from copybot.sim.params import EngineParams, SafetyParams

GROUPS: tuple[type[BaseSettings], ...] = (
    Settings,
    RiskLimits,
    EngineParams,
    SafetyParams,
    AnalysisParams,
    ScoringParams,
)
# Variables que no son opciones del programa (las usan los tests).
EXTRA_ALLOWED = frozenset({"COPYBOT_TEST_DATABASE_URL"})


def known_names() -> frozenset[str]:
    names = set(EXTRA_ALLOWED)
    for group in GROUPS:
        prefix = str(group.model_config.get("env_prefix", "")).upper()
        names |= {prefix + field.upper() for field in group.model_fields}
    return frozenset(names)


def dotenv_names(path: Path) -> list[str]:
    if not path.is_file():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            names.append(line.split("=", 1)[0].removeprefix("export ").strip())
    return names


def unknown_settings(
    environ: Mapping[str, str] | None = None, dotenv: Path | None = Path(".env")
) -> list[str]:
    """Variables ``COPYBOT_*`` que no corresponden a ninguna opción (vacío: todo bien)."""
    env = os.environ if environ is None else environ
    candidates: Iterable[str] = list(env) + (dotenv_names(dotenv) if dotenv else [])
    known = known_names()
    return sorted({n for n in candidates if n.upper().startswith("COPYBOT_")} - known)

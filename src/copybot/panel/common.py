"""Piezas comunes del panel: números para JSON, estado de los agentes y caídas."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal

SIN_DATOS = "sin datos todavía"
TRABAJANDO = "TRABAJANDO"
EN_ESPERA = "EN ESPERA"
# Un paso está «trabajando» si corrió hace menos de esto (el paso diario reintenta cada
# 10 minutos hasta las 02:00 UTC).
ACTIVE_WINDOW = timedelta(minutes=10)


def num(x: float | int | Decimal | None, places: int = 6) -> float | None:
    """Número para el JSON (``None`` si falta o no es finito)."""
    if x is None:
        return None
    value = float(x)
    if not math.isfinite(value):
        return None
    return round(value, places)


def iso(x: datetime | date | None) -> str | None:
    return None if x is None else x.isoformat()


def age_seconds(last: datetime | None, now: datetime) -> float | None:
    """Segundos desde ``last`` (nunca negativos: un reloj adelantado cuenta como 0)."""
    if last is None:
        return None
    return max(0.0, (now - last).total_seconds())


def agent_state(
    last: datetime | None,
    now: datetime,
    *,
    running: bool = False,
    window: timedelta = ACTIVE_WINDOW,
) -> str:
    """TRABAJANDO si el paso está en marcha o corrió hace menos de ``window``; si no (o si
    no ha corrido nunca), EN ESPERA."""
    if running:
        return TRABAJANDO
    seconds = age_seconds(last, now)
    if seconds is not None and seconds <= window.total_seconds():
        return TRABAJANDO
    return EN_ESPERA


def reason_group(reason: str) -> str:
    """Motivo sin cifras, para contar motivos parecidos («solo lleva N días…»)."""
    return re.sub(r"\d[\d.,]*", "N", reason.split(":")[0].strip()) or "(sin motivo)"


def max_drawdown(initial: float, days: Iterable[tuple[float, float]]) -> float | None:
    """Caída máxima (0-1) de una curva diaria ``(cierre, peor momento del día)``: el pico
    es el mayor cierre hasta el día anterior (o el capital inicial) y el valle, el peor
    momento de cada día. ``None`` sin días."""
    peak, worst_dd, seen = initial, 0.0, False
    for close, worst in days:
        seen = True
        if peak > 0:
            worst_dd = max(worst_dd, 1 - worst / peak)
        peak = max(peak, close)
    return worst_dd if seen else None


def base_of(symbol: str, quote: str = "USDT") -> str:
    return symbol[: -len(quote)] if symbol.endswith(quote) and len(symbol) > len(quote) else symbol

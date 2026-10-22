"""Universo invertible de cada día: qué monedas se pueden evaluar y, si no, por qué.

Todo se decide **con los datos hasta el cierre del día** (nunca con la lista ni el volumen
de hoy aplicados al pasado). Incluye los pares que Binance retiró después, mientras
cotizaban, para no ver solo a los supervivientes.
"""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from copybot.crypto.panel import Panel, Series
from copybot.crypto.sync import STABLE_BASES

# Oro tokenizado, versiones envueltas o con staking de otra moneda (mismo riesgo dos
# veces) y monedas que siguen a una divisa.
PEGGED_OR_WRAPPED = frozenset(
    {"PAXG", "XAUT", "WBTC", "WBETH", "BETH", "BNSOL", "STETH", "WSTETH", "CBBTC", "RENBTC"}
)
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


@dataclass(frozen=True)
class UniverseParams:
    min_age_days: int = 120
    liquidity_days: int = 30
    min_median_quote_volume: float = 5_000_000  # USDT al día (mediana de 30 días)
    max_rank_by_liquidity: int = 100
    quality_days: int = 100  # sin huecos ni saltos raros (cubre la SMA100 y 91 días atrás)
    max_jump: float = 4.0  # un cierre ×4 o ÷4 del anterior: redenominación o error
    min_daily_vol: float = 0.005  # por debajo: moneda anclada (estable)
    vol_days: int = 60
    # Solo para pruebas de estrés (sin las monedas que más aportaron, solo supervivientes).
    exclude: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Member:
    symbol: str
    base: str
    median_quote_volume: float
    liquidity_rank: int


@dataclass(frozen=True)
class Universe:
    day: date
    members: list[Member]
    excluded: dict[str, str]  # símbolo → motivo en castellano


def _leveraged(series: Series, bases: frozenset[str]) -> bool:
    return any(
        series.base.endswith(suffix)
        and len(series.base) > len(suffix) + 1
        and series.base[: -len(suffix)] in bases
        for suffix in LEVERAGED_SUFFIXES
    )


def daily_vol(closes: list[float]) -> float:
    """Desviación típica de los rendimientos logarítmicos diarios."""
    rets = [math.log(b / a) for a, b in itertools.pairwise(closes) if a > 0 and b > 0]
    return statistics.pstdev(rets) if len(rets) >= 2 else 0.0


def universe(panel: Panel, day: date, p: UniverseParams | None = None) -> Universe:
    p = p or UniverseParams()
    bases = frozenset(s.base for s in panel.series.values())
    excluded: dict[str, str] = {}
    candidates: list[tuple[Series, float]] = []
    gaps = panel.global_gaps()
    for series in panel.series.values():
        i = series.at(day)
        if i is None or not series.traded(i):
            continue  # no cotizaba ese día (aún no listada, ya retirada o sin negociación)
        if series.symbol in p.exclude:
            excluded[series.symbol] = "excluida en esta prueba de estrés"
            continue
        if series.base in STABLE_BASES:
            excluded[series.symbol] = "moneda estable o divisa"
            continue
        if series.base in PEGGED_OR_WRAPPED:
            excluded[series.symbol] = "versión envuelta, con staking o anclada a otro activo"
            continue
        if _leveraged(series, bases):
            excluded[series.symbol] = "token apalancado"
            continue
        age = (day - series.days[series.episode_start(i)]).days + 1
        if age < p.min_age_days:
            excluded[series.symbol] = f"solo lleva {age} días en Binance (mínimo {p.min_age_days})"
            continue
        q0 = series.index(day - timedelta(days=p.quality_days - 1))
        q0 = 0 if q0 is None else q0
        if series.days[q0] < day - timedelta(days=p.quality_days - 1):
            q0 += 1
        traded = {series.days[k] for k in range(q0, i + 1) if series.traded(k)}
        needed = {day - timedelta(days=k) for k in range(p.quality_days)} - gaps
        if not needed <= traded:
            excluded[series.symbol] = f"le faltan velas en los últimos {p.quality_days} días"
            continue
        closes = series.close[q0 : i + 1]
        if any(
            a <= 0 or b / a > p.max_jump or b / a < 1 / p.max_jump
            for a, b in itertools.pairwise(closes)
        ):
            excluded[series.symbol] = (
                f"precio que se multiplica o divide por más de {p.max_jump:g} en un día "
                f"(últimos {p.quality_days} días): posible cambio de denominación o error"
            )
            continue
        vol = daily_vol(series.close[max(0, i - p.vol_days) : i + 1])
        if vol < p.min_daily_vol:
            excluded[series.symbol] = "apenas se mueve: moneda estable o anclada"
            continue
        liq = statistics.median(series.quote_volume[max(0, i - p.liquidity_days + 1) : i + 1])
        if liq < p.min_median_quote_volume:
            excluded[series.symbol] = (
                f"poco volumen: mediana de {liq / 1e6:,.1f} M $ al día "
                f"(mínimo {p.min_median_quote_volume / 1e6:,.0f} M $)"
            )
            continue
        candidates.append((series, liq))
    candidates.sort(key=lambda c: (-c[1], c[0].symbol))
    members = []
    for rank, (series, liq) in enumerate(candidates, start=1):
        if rank > p.max_rank_by_liquidity:
            excluded[series.symbol] = (
                f"fuera de las {p.max_rank_by_liquidity} más líquidas (puesto {rank})"
            )
            continue
        members.append(Member(series.symbol, series.base, liq, rank))
    return Universe(day, members, excluded)

"""Señales de cada moneda al cierre de un día (solo con datos hasta ese cierre).

* **Momentum ajustado por volatilidad** en varios horizontes, saltando el último día:
  ``a_L = ln(C[t−1] / C[t−1−L]) / (σ60 · √L)``. Es una especie de «estadístico t» de la
  tendencia: así no encabezan siempre el ranking las monedas más erráticas.
* **Puntuación de 0 a 100**: la media de los percentiles de ``a_L`` entre las monedas del
  universo de ese día, con el mismo peso para cada horizonte (no se elige el «mejor»
  horizonte con el backtest).
* **Tendencia propia**: cierre frente a sus medias de 20, 50 y 100 días.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date

from copybot.crypto.panel import Panel, Series


@dataclass(frozen=True)
class SignalParams:
    horizons: tuple[int, ...] = (7, 14, 30, 60, 90)
    skip_days: int = 1
    vol_days: int = 60
    vol_floor: float = 0.01  # 1 % diario como mínimo
    sma_days: tuple[int, ...] = (20, 50, 100)


@dataclass(frozen=True)
class CoinSignal:
    symbol: str
    base: str
    close: float
    vol: float  # volatilidad diaria (σ60, con suelo)
    momentum: dict[int, float]  # horizonte → rentabilidad logarítmica (saltando días)
    adjusted: dict[int, float]  # horizonte → momentum / (σ·√L)
    above_sma: dict[int, bool]  # media → cierre por encima
    score: float = 0.0  # 0-100, relativa al universo del día
    rank: int = 0

    @property
    def trend_share(self) -> float:
        """Fracción de medias por encima de las que cierra (0, 1/3, 2/3 o 1)."""
        return sum(self.above_sma.values()) / len(self.above_sma) if self.above_sma else 0.0


def sma(values: list[float], end: int, days: int) -> float | None:
    """Media de ``values[end−days+1 .. end]``; ``None`` si no hay bastantes."""
    if end + 1 < days:
        return None
    return sum(values[end - days + 1 : end + 1]) / days


def coin_signal(series: Series, day: date, p: SignalParams) -> CoinSignal | None:
    i = series.at(day)
    longest = max(p.horizons) + p.skip_days
    if i is None or i < max(longest, p.vol_days, max(p.sma_days)):
        return None
    closes = series.close
    rets = [
        math.log(closes[k] / closes[k - 1])
        for k in range(i - p.vol_days + 1, i + 1)
        if closes[k] > 0 and closes[k - 1] > 0
    ]
    if len(rets) < p.vol_days // 2:
        return None
    vol = max(statistics.pstdev(rets), p.vol_floor)
    end = i - p.skip_days
    momentum, adjusted = {}, {}
    for horizon in p.horizons:
        a, b = closes[end - horizon], closes[end]
        if a <= 0 or b <= 0:
            return None
        momentum[horizon] = math.log(b / a)
        adjusted[horizon] = momentum[horizon] / (vol * math.sqrt(horizon))
    above = {}
    for days in p.sma_days:
        mean = sma(closes, i, days)
        if mean is None:
            return None
        above[days] = closes[i] > mean
    return CoinSignal(series.symbol, series.base, closes[i], vol, momentum, adjusted, above)


def rank(signals: list[CoinSignal], p: SignalParams) -> list[CoinSignal]:
    """Puntuación (media de percentiles por horizonte) y puesto. Orden determinista: más
    puntuación, después más momentum ajustado a 30 días (o el horizonte central) y luego
    el símbolo."""
    n = len(signals)
    if n == 0:
        return []
    pct: dict[str, list[float]] = {s.symbol: [] for s in signals}
    for horizon in p.horizons:
        values = sorted(s.adjusted[horizon] for s in signals)
        for s in signals:
            below = _count_below(values, s.adjusted[horizon])
            ties = _count_equal(values, s.adjusted[horizon])
            pct[s.symbol].append((below + 0.5 * (ties - 1)) / max(n - 1, 1))
    tiebreak = 30 if 30 in p.horizons else p.horizons[len(p.horizons) // 2]
    scored = [
        CoinSignal(
            s.symbol,
            s.base,
            s.close,
            s.vol,
            s.momentum,
            s.adjusted,
            s.above_sma,
            score=100 * statistics.fmean(pct[s.symbol]),
        )
        for s in signals
    ]
    scored.sort(key=lambda s: (-s.score, -s.adjusted[tiebreak], s.symbol))
    return [
        CoinSignal(
            s.symbol, s.base, s.close, s.vol, s.momentum, s.adjusted, s.above_sma, s.score, k
        )
        for k, s in enumerate(scored, start=1)
    ]


def _count_below(sorted_values: list[float], x: float) -> int:
    return bisect_left(sorted_values, x)


def _count_equal(sorted_values: list[float], x: float) -> int:
    return bisect_right(sorted_values, x) - bisect_left(sorted_values, x)


def ranked_day(panel: Panel, day: date, symbols: list[str], p: SignalParams) -> list[CoinSignal]:
    """Señales y puesto de ``symbols`` (el universo del día) al cierre de ``day``."""
    out = []
    for symbol in symbols:
        sig = coin_signal(panel.series[symbol], day, p)
        if sig is not None:
            out.append(sig)
    return rank(out, p)

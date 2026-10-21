"""Estadística de un backtest: resultado, acierto, profit factor, caída máxima e
intervalo de confianza *bootstrap* de la rentabilidad media por operación.

El *bootstrap* usa una semilla fija: con los mismos datos, el mismo intervalo.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal(0)
MIN_SAMPLE = 30  # por debajo: muestra insuficiente (criterio de la Fase 1)
SMALL_SAMPLE = 100  # por debajo: aviso más suave


@dataclass(frozen=True)
class TradeStats:
    n: int
    total_usd: Decimal
    mean_return_pct: Decimal | None
    median_return_pct: Decimal | None
    win_rate: Decimal | None
    profit_factor: Decimal | None
    ci_low_pct: Decimal | None  # intervalo del 95 % de la rentabilidad media
    ci_high_pct: Decimal | None
    prob_mean_positive: Decimal | None  # fracción de remuestreos con media > 0

    @property
    def warning(self) -> str:
        if self.n < MIN_SAMPLE:
            return f"muestra insuficiente ({self.n} operaciones; hacen falta al menos {MIN_SAMPLE})"
        if self.n < SMALL_SAMPLE:
            return f"muestra pequeña ({self.n} operaciones; conclusiones débiles)"
        return ""


def trade_stats(
    pnls_usd: Sequence[Decimal],
    returns_pct: Sequence[Decimal],
    *,
    seed: int = 7,
    resamples: int = 5000,
) -> TradeStats:
    n = len(returns_pct)
    if n == 0:
        return TradeStats(0, ZERO, None, None, None, None, None, None, None)
    wins = sum(1 for p in pnls_usd if p > 0)
    gross_win = sum((p for p in pnls_usd if p > 0), ZERO)
    gross_loss = -sum((p for p in pnls_usd if p < 0), ZERO)
    floats = [float(r) for r in returns_pct]
    low = high = prob = None
    if n >= 2:
        rng = random.Random(seed)  # noqa: S311 (estadística, no criptografía)
        means = sorted(statistics.fmean(rng.choices(floats, k=n)) for _ in range(resamples))
        low = Decimal(str(round(means[int(0.025 * resamples)], 4)))
        high = Decimal(str(round(means[int(0.975 * resamples) - 1], 4)))
        prob = Decimal(sum(1 for m in means if m > 0)) / resamples
    return TradeStats(
        n=n,
        total_usd=sum(pnls_usd, ZERO),
        mean_return_pct=Decimal(str(round(statistics.fmean(floats), 4))),
        median_return_pct=Decimal(str(round(statistics.median(floats), 4))),
        win_rate=Decimal(wins) / n,
        profit_factor=None if gross_loss == 0 else gross_win / gross_loss,
        ci_low_pct=low,
        ci_high_pct=high,
        prob_mean_positive=prob,
    )


def max_drawdown_pct(curve: Sequence[tuple[int, Decimal]]) -> Decimal:
    """Mayor caída desde un máximo de la curva de patrimonio, en %."""
    peak = None
    worst = ZERO
    for _, equity in curve:
        peak = equity if peak is None else max(peak, equity)
        if peak and peak > 0:
            worst = max(worst, (1 - equity / peak) * 100)
    return worst

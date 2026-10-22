"""Estadística de un backtest: resultado, acierto, profit factor, caída máxima e
intervalo de confianza *bootstrap* de la rentabilidad media por operación.

Para el backtest de cripto (apartado 8.5 del diseño) añade el exceso diario logarítmico
frente a BTC, el *bootstrap* estacionario y por grupos, el Sharpe probabilístico y el
deflactado (PSR y DSR), los días necesarios (MinTRL) y la correlación de Spearman.

Todo *bootstrap* usa una semilla fija: con los mismos datos, el mismo intervalo.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Sequence
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


# --- Backtest de cripto (apartado 8.5 del diseño) -------------------------------------------

DAYS_PER_YEAR = 365  # cripto cotiza todos los días del año
EULER_GAMMA = 0.5772  # constante de Euler-Mascheroni, con los decimales del diseño
STATIONARY_BLOCKS = (7, 28, 56)  # longitudes medias de bloque L del bootstrap estacionario
_NORMAL = statistics.NormalDist()  # Φ y Φ⁻¹ de la normal estándar

# Estadístico que se recalcula en cada remuestreo (por defecto, la media).
Statistic = Callable[[Sequence[float]], float]


def log_excess(
    strategy_daily_returns: Sequence[float], benchmark_daily_returns: Sequence[float]
) -> list[float]:
    """Exceso diario logarítmico frente a la referencia: ``e_t = ln(1 + r_A,t) − ln(1 + r_B,t)``.

    Las rentabilidades son simples y diarias (0,01 = +1 %), del mismo día en las dos series.
    Al ser logarítmico, la suma es exacta: ``Σ e_t = ln(V_A / V_B)``, el logaritmo del cociente
    entre los valores finales de las dos curvas con el mismo capital inicial.
    """
    if len(strategy_daily_returns) != len(benchmark_daily_returns):
        raise ValueError("las dos series de rentabilidades deben cubrir los mismos días")
    excess = []
    for r_a, r_b in zip(strategy_daily_returns, benchmark_daily_returns, strict=True):
        if r_a <= -1 or r_b <= -1:
            raise ValueError("una rentabilidad diaria de −100 % o peor no tiene logaritmo")
        excess.append(math.log1p(float(r_a)) - math.log1p(float(r_b)))
    return excess


def annualized_excess(excess: Sequence[float]) -> float:
    """Exceso anual frente a la referencia, ``exp(365·ē) − 1`` (0,09 = +9 % al año).

    ``excess`` son los excesos diarios logarítmicos de :func:`log_excess` (al menos uno).
    """
    return math.expm1(DAYS_PER_YEAR * statistics.fmean(excess))


def _is_degenerate(values: Sequence[float]) -> bool:
    """Menos de 2 valores o todos iguales: no hay dispersión que medir."""
    return len(values) < 2 or min(values) == max(values)


def sharpe_ratio(values: Sequence[float]) -> float | None:
    """Sharpe por periodo (sin anualizar), ``media / desviación típica muestral``.

    Es el ``SR̂`` diario que usan :func:`psr`, :func:`deflated_sharpe` y :func:`min_trl`.
    ``None`` si hay menos de 2 valores o todos son iguales.
    """
    if _is_degenerate(values):
        return None
    return statistics.fmean(values) / statistics.stdev(values)


def information_ratio(excess: Sequence[float]) -> float | None:
    """Ratio de información anual del exceso diario: ``ē / sd(e) · √365``.

    ``None`` si hay menos de 2 días o el exceso es constante.
    """
    sr = sharpe_ratio(excess)
    return None if sr is None else sr * math.sqrt(DAYS_PER_YEAR)


def _central_moments(values: Sequence[float]) -> tuple[float, float, float] | None:
    """Momentos centrales muestrales m₂, m₃ y m₄ (divididos entre n)."""
    if _is_degenerate(values):
        return None
    n = len(values)
    mean = statistics.fmean(values)
    deviations = [v - mean for v in values]
    m2 = math.fsum(d * d for d in deviations) / n
    m3 = math.fsum(d * d * d for d in deviations) / n
    m4 = math.fsum(d * d * d * d for d in deviations) / n
    return m2, m3, m4


def skewness(values: Sequence[float]) -> float | None:
    """Asimetría muestral ``γ₃ = m₃ / m₂^1,5`` (0 en una distribución simétrica).

    ``None`` si hay menos de 2 valores o todos son iguales.
    """
    moments = _central_moments(values)
    if moments is None:
        return None
    m2, m3, _ = moments
    return m3 / m2**1.5


def kurtosis(values: Sequence[float]) -> float | None:
    """Curtosis muestral ``γ₄ = m₄ / m₂²``, **no** en exceso (3 en la normal).

    ``None`` si hay menos de 2 valores o todos son iguales.
    """
    moments = _central_moments(values)
    if moments is None:
        return None
    m2, _, m4 = moments
    return m4 / (m2 * m2)


def _check_resampling(resamples: int, alpha: float | None = None) -> None:
    if resamples < 1:
        raise ValueError("hace falta al menos un remuestreo")
    if alpha is not None and not 0 < alpha < 1:
        raise ValueError("alpha debe estar entre 0 y 1")


def _percentile_interval(sorted_stats: Sequence[float], alpha: float) -> tuple[float, float]:
    """Intervalo por percentiles ``α/2`` y ``1 − α/2`` de remuestreos ya ordenados.

    Mismo criterio que :func:`trade_stats`: con B = 5.000 y α = 0,05 toma los puestos 125
    y 4.874 (contando desde 0), simétricos.
    """
    b = len(sorted_stats)
    k = min(int(alpha / 2 * b + 1e-9), (b - 1) // 2)  # 1e-9: 0,025·5.000 debe dar 125
    return sorted_stats[k], sorted_stats[b - 1 - k]


def stationary_bootstrap(
    values: Sequence[float],
    statistic: Statistic,
    *,
    block: float,
    resamples: int = 5000,
    seed: int = 7,
) -> list[float]:
    """Bootstrap estacionario (Politis y Romano, 1994): el estadístico en cada remuestreo.

    Cada remuestreo tiene la misma longitud T que la serie. El primer índice es uniforme;
    en cada paso, con probabilidad ``1/block`` se salta a un índice nuevo uniforme y, si no,
    se toma el siguiente, ``(i + 1) mod T``. Así se conservan rachas de días seguidos de
    longitud media ``block`` (L), que respetan la dependencia entre días. Con L = 1 es el
    bootstrap i.i.d.

    Devuelve los ``resamples`` valores del estadístico **ordenados** de menor a mayor. Con la
    misma semilla, el mismo resultado.
    """
    data = [float(v) for v in values]
    t = len(data)
    if t == 0:
        raise ValueError("serie vacía: no hay nada que remuestrear")
    if block < 1:
        raise ValueError("la longitud media de bloque L debe ser ≥ 1")
    _check_resampling(resamples)
    jump = 1.0 / block
    rng = random.Random(seed)  # noqa: S311 (estadística, no criptografía)
    draw, pick = rng.random, rng.randrange
    stats = []
    for _ in range(resamples):
        i = pick(t)
        sample = [data[i]]
        for _ in range(t - 1):
            i = pick(t) if draw() < jump else (i + 1) % t
            sample.append(data[i])
        stats.append(float(statistic(sample)))
    stats.sort()
    return stats


def stationary_bootstrap_ci_by_block(
    values: Sequence[float],
    *,
    blocks: Sequence[float] = STATIONARY_BLOCKS,
    resamples: int = 5000,
    seed: int = 7,
    alpha: float = 0.05,
    statistic: Statistic = statistics.fmean,
) -> dict[float, tuple[float, float]]:
    """Intervalo por percentiles (2,5-97,5 con α = 0,05) para cada longitud de bloque L.

    Cada L usa su propio generador con la misma semilla, así que su intervalo no depende de
    qué otras L se pidan. Sirve para comprobar que el límite inferior es > 0 con todas.
    """
    if not blocks:
        raise ValueError("hace falta al menos una longitud de bloque")
    _check_resampling(resamples, alpha)
    return {
        block: _percentile_interval(
            stationary_bootstrap(values, statistic, block=block, resamples=resamples, seed=seed),
            alpha,
        )
        for block in blocks
    }


def stationary_bootstrap_ci(
    values: Sequence[float],
    *,
    blocks: Sequence[float] = STATIONARY_BLOCKS,
    resamples: int = 5000,
    seed: int = 7,
    alpha: float = 0.05,
    statistic: Statistic = statistics.fmean,
) -> tuple[float, float]:
    """Intervalo ``(bajo, alto)`` del estadístico con bootstrap estacionario.

    Se calcula con cada L de ``blocks`` (por defecto 7, 28 y 56 días), B = 5.000 remuestreos
    y semilla 7, y **cuenta el más ancho** (ante un empate, el de la primera L). Una serie
    constante da un intervalo degenerado (bajo = alto).
    """
    by_block = stationary_bootstrap_ci_by_block(
        values,
        blocks=blocks,
        resamples=resamples,
        seed=seed,
        alpha=alpha,
        statistic=statistic,
    )
    return max(by_block.values(), key=lambda ci: ci[1] - ci[0])


def cluster_bootstrap(
    groups: Sequence[Sequence[float]],
    statistic: Statistic,
    *,
    resamples: int = 5000,
    seed: int = 7,
) -> list[float]:
    """Bootstrap por grupos: se remuestrean grupos **enteros** con reemplazo.

    Pensado para las operaciones agrupadas por semana ISO de entrada, que no son
    independientes dentro de la misma semana. Cada remuestreo toma tantos grupos como hay,
    junta sus valores y calcula el estadístico. Los grupos vacíos no cuentan. Devuelve los
    valores del estadístico ordenados de menor a mayor.
    """
    clusters = [[float(v) for v in group] for group in groups if len(group) > 0]
    if not clusters:
        raise ValueError("no hay ningún grupo con datos que remuestrear")
    _check_resampling(resamples)
    g = len(clusters)
    rng = random.Random(seed)  # noqa: S311 (estadística, no criptografía)
    pick = rng.randrange
    stats = []
    for _ in range(resamples):
        sample: list[float] = []
        for _ in range(g):
            sample.extend(clusters[pick(g)])
        stats.append(float(statistic(sample)))
    stats.sort()
    return stats


def cluster_bootstrap_ci(
    groups: Sequence[Sequence[float]],
    *,
    resamples: int = 5000,
    seed: int = 7,
    alpha: float = 0.05,
    statistic: Statistic = statistics.fmean,
) -> tuple[float, float]:
    """Intervalo ``(bajo, alto)`` por percentiles con :func:`cluster_bootstrap`.

    Por defecto, el de la media por operación con B = 5.000, semilla 7 y percentiles 2,5-97,5.
    """
    _check_resampling(resamples, alpha)
    stats = cluster_bootstrap(groups, statistic, resamples=resamples, seed=seed)
    return _percentile_interval(stats, alpha)


def _sharpe_dispersion(sr: float, skew: float, kurt: float) -> float:
    """``1 − γ₃·SR̂ + (γ₄ − 1)/4·SR̂²``: corrige la varianza del Sharpe por colas y asimetría."""
    dispersion = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if dispersion <= 0:
        raise ValueError("asimetría y curtosis incompatibles con este Sharpe")
    return dispersion


def _check_observations(n: int) -> None:
    if n < 2:
        raise ValueError("hacen falta al menos 2 observaciones")


def psr(sr: float, sr_star: float, n: int, skew: float, kurt: float) -> float:
    """Sharpe probabilístico (Bailey y López de Prado): P(Sharpe verdadero > ``sr_star``).

    ``PSR = Φ( (SR̂ − SR*)·√(n − 1) / √(1 − γ₃·SR̂ + (γ₄ − 1)/4·SR̂²) )``

    ``sr`` y ``sr_star`` son Sharpe por periodo (diarios, sin anualizar), ``n`` el número de
    periodos, ``skew`` la asimetría γ₃ y ``kurt`` la curtosis γ₄ (3 en la normal).
    """
    _check_observations(n)
    z = (sr - sr_star) * math.sqrt(n - 1) / math.sqrt(_sharpe_dispersion(sr, skew, kurt))
    return _NORMAL.cdf(z)


def sr0(k: int, variance: float) -> float:
    """Sharpe máximo esperado por azar entre K configuraciones sin ventaja real.

    ``SR₀ = √V · ( (1 − γ)·Φ⁻¹(1 − 1/K) + γ·Φ⁻¹(1 − 1/(K·e)) )``, con γ = 0,5772
    (Euler-Mascheroni) y V la varianza de los Sharpe de las K configuraciones. Con K = 1
    no hay selección: SR₀ = 0.
    """
    if k < 1:
        raise ValueError("K debe ser al menos 1")
    if variance < 0:
        raise ValueError("la varianza no puede ser negativa")
    if k == 1:
        return 0.0
    factor = (1 - EULER_GAMMA) * _NORMAL.inv_cdf(1 - 1 / k) + EULER_GAMMA * _NORMAL.inv_cdf(
        1 - 1 / (k * math.e)
    )
    return math.sqrt(variance) * factor


def deflated_sharpe(sr: float, n: int, skew: float, kurt: float, k: int, variance: float) -> float:
    """Sharpe deflactado (Bailey y López de Prado, 2014): ``DSR = PSR(SR₀)``. Umbral ≥ 0,95.

    ``variance`` es la varianza de los Sharpe diarios de las K configuraciones registradas;
    se aplica el suelo conservador ``V = max(variance, 1/n)``. Con K = 1 es ``PSR(0)``.
    """
    _check_observations(n)
    return psr(sr, sr0(k, max(variance, 1 / n)), n, skew, kurt)


def min_trl(sr: float, skew: float, kurt: float, z: float) -> float | None:
    """Días necesarios (MinTRL) para afirmar que el Sharpe verdadero es > 0 con confianza Φ(z).

    ``MinTRL = 1 + (1 − γ₃·SR̂ + (γ₄ − 1)/4·SR̂²)·(z / SR̂)²``, con ``sr`` diario. Con
    n = MinTRL días, ``psr(sr, 0, n, skew, kurt) = Φ(z)``. ``None`` si ``sr ≤ 0``: no se puede
    demostrar, en el pasado no ganó a la referencia.
    """
    if sr <= 0:
        return None
    return 1 + _sharpe_dispersion(sr, skew, kurt) * (z / sr) ** 2


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Rangos de 1 a n; los empatados reciben la media de los puestos que ocupan."""
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        rank = (start + end) / 2 + 1
        for position in order[start : end + 1]:
            ranks[position] = rank
        start = end + 1
    return ranks


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Correlación de Spearman: Pearson entre los rangos, con rangos medios en los empates.

    ``None`` si hay menos de 2 pares o una de las dos series es constante (no hay orden).
    """
    if len(x) != len(y):
        raise ValueError("las dos series deben tener la misma longitud")
    if len(x) < 2:
        return None
    rx, ry = _average_ranks(x), _average_ranks(y)
    if _is_degenerate(rx) or _is_degenerate(ry):
        return None
    return statistics.correlation(rx, ry)

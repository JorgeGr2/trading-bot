"""Estadística del backtest de cripto (apartado 8.5 del diseño): cifras a mano y semillas."""

from __future__ import annotations

import itertools
import math
import random
import statistics

import pytest

from copybot.backtest.stats import (
    annualized_excess,
    cluster_bootstrap,
    cluster_bootstrap_ci,
    deflated_sharpe,
    information_ratio,
    kurtosis,
    log_excess,
    min_trl,
    psr,
    sharpe_ratio,
    skewness,
    spearman,
    sr0,
    stationary_bootstrap,
    stationary_bootstrap_ci,
    stationary_bootstrap_ci_by_block,
)

PHI = statistics.NormalDist()


def _autocorrelated(n: int, seed: int) -> list[float]:
    """Serie AR(1) con deriva: días seguidos parecidos, como un exceso diario real."""
    rng = random.Random(seed)  # noqa: S311
    values, prev = [], 0.0
    for _ in range(n):
        prev = 0.6 * prev + rng.gauss(0, 0.01)
        values.append(0.0005 + prev)
    return values


# --- Exceso diario logarítmico ------------------------------------------------------------


def test_log_excess_sums_to_log_ratio_of_final_values():
    rng = random.Random(3)  # noqa: S311
    r_a = [rng.uniform(-0.2, 0.25) for _ in range(500)]
    r_btc = [rng.uniform(-0.1, 0.1) for _ in range(500)]
    v_a = v_btc = 100.0
    for a, b in zip(r_a, r_btc, strict=True):
        v_a *= 1 + a
        v_btc *= 1 + b
    excess = log_excess(r_a, r_btc)
    assert len(excess) == 500
    assert math.fsum(excess) == pytest.approx(math.log(v_a / v_btc), rel=1e-9, abs=1e-12)


def test_log_excess_by_hand_and_invalid_inputs():
    assert log_excess([0.1, -0.5], [0.0, 0.0]) == pytest.approx([math.log(1.1), math.log(0.5)])
    assert log_excess([0.02], [0.02]) == [0.0]
    with pytest.raises(ValueError, match="mismos días"):
        log_excess([0.1, 0.2], [0.1])
    with pytest.raises(ValueError, match="logaritmo"):
        log_excess([-1.0], [0.0])


def test_annualized_excess_and_information_ratio():
    assert annualized_excess([0.001] * 30) == pytest.approx(math.exp(0.365) - 1)
    assert annualized_excess([0.0, 0.0]) == 0.0
    excess = [0.003, -0.001, 0.002, 0.0]
    mean, sd = 0.001, statistics.stdev(excess)
    assert information_ratio(excess) == pytest.approx(mean / sd * math.sqrt(365))
    assert sharpe_ratio(excess) == pytest.approx(mean / sd)
    assert information_ratio([0.001] * 10) is None  # sin dispersión
    assert information_ratio([0.001]) is None


# --- Asimetría y curtosis -----------------------------------------------------------------


def test_skewness_and_kurtosis_by_hand():
    # Bernoulli con p = 1/4: γ₃ = (1 − 2p)/√(p(1 − p)) y γ₄ = (1 − 3p(1 − p))/(p(1 − p)).
    p = 0.25
    values = [0.0, 0.0, 0.0, 1.0]
    assert skewness(values) == pytest.approx((1 - 2 * p) / math.sqrt(p * (1 - p)))
    assert kurtosis(values) == pytest.approx((1 - 3 * p * (1 - p)) / (p * (1 - p)))
    assert skewness([1.0, 2.0, 3.0]) == pytest.approx(0.0)
    assert skewness([5.0] * 4) is None and kurtosis([5.0] * 4) is None


def test_kurtosis_is_three_for_normal_data():
    rng = random.Random(11)  # noqa: S311
    values = [rng.gauss(0, 1) for _ in range(20_000)]
    assert kurtosis(values) == pytest.approx(3.0, abs=0.15)  # no es la curtosis en exceso
    assert skewness(values) == pytest.approx(0.0, abs=0.05)


# --- Bootstrap estacionario ---------------------------------------------------------------


def test_stationary_bootstrap_is_deterministic_with_the_seed():
    values = _autocorrelated(300, seed=1)
    a = stationary_bootstrap(values, statistics.fmean, block=28, resamples=500, seed=7)
    b = stationary_bootstrap(values, statistics.fmean, block=28, resamples=500, seed=7)
    c = stationary_bootstrap(values, statistics.fmean, block=28, resamples=500, seed=8)
    assert a == b
    assert a != c
    assert a == sorted(a) and len(a) == 500
    ci = stationary_bootstrap_ci(values, resamples=500)
    assert ci == stationary_bootstrap_ci(values, resamples=500)
    assert ci != stationary_bootstrap_ci(values, resamples=500, seed=8)


def test_constant_series_gives_degenerate_interval():
    low, high = stationary_bootstrap_ci([0.002] * 120, resamples=300)
    assert low == high == pytest.approx(0.002)
    assert cluster_bootstrap_ci([[0.01, 0.01], [0.01]], resamples=200) == pytest.approx(
        (0.01, 0.01)
    )


def test_stationary_bootstrap_takes_the_next_index_with_wraparound():
    t = 50
    values = [float(i) for i in range(t)]

    def follows_on(sample):  # 1.0 si cada índice es el siguiente del anterior (mod T)
        return float(all((b - a) % t == 1 for a, b in itertools.pairwise(sample)))

    def share_consecutive(sample):
        steps = [(b - a) % t == 1 for a, b in itertools.pairwise(sample)]
        return sum(steps) / len(steps)

    # Con L enorme nunca salta: un único bloque que da la vuelta al final de la serie.
    runs = stationary_bootstrap(values, follows_on, block=1e12, resamples=200, seed=7)
    assert set(runs) == {1.0}
    starts = stationary_bootstrap(values, lambda s: s[0], block=1e12, resamples=2000, seed=7)
    assert len(set(starts)) == t  # el primer índice es uniforme: salen todos
    # Con L = 10 salta con probabilidad 1/10 en cada paso (y cae en i + 1 con 1/T).
    shares = stationary_bootstrap(values, share_consecutive, block=10, resamples=400, seed=7)
    expected = (1 - 1 / 10) + (1 / 10) * (1 / t)
    assert statistics.fmean(shares) == pytest.approx(expected, abs=0.01)
    # Con L = 1 es el bootstrap i.i.d.: casi nunca toma el siguiente.
    iid = stationary_bootstrap(values, share_consecutive, block=1, resamples=200, seed=7)
    assert statistics.fmean(iid) == pytest.approx(1 / t, abs=0.01)


def test_stationary_ci_counts_the_widest_of_the_three_blocks():
    values = _autocorrelated(400, seed=5)
    by_block = stationary_bootstrap_ci_by_block(values, resamples=600)
    assert set(by_block) == {7, 28, 56}
    widest = max(by_block.values(), key=lambda ci: ci[1] - ci[0])
    assert stationary_bootstrap_ci(values, resamples=600) == widest
    # Cada L se calcula con su propio generador: no depende de las demás.
    assert stationary_bootstrap_ci(values, blocks=(28,), resamples=600) == by_block[28]
    for low, high in by_block.values():
        assert low < statistics.fmean(values) < high
    # Percentiles 2,5-97,5: puestos 15 y 584 de 600 remuestreos ordenados.
    stats = stationary_bootstrap(values, statistics.fmean, block=7, resamples=600, seed=7)
    assert by_block[7] == (stats[15], stats[584])


def test_stationary_bootstrap_rejects_bad_inputs():
    with pytest.raises(ValueError, match="vacía"):
        stationary_bootstrap([], statistics.fmean, block=7)
    with pytest.raises(ValueError, match="bloque"):
        stationary_bootstrap([1.0, 2.0], statistics.fmean, block=0.5)
    with pytest.raises(ValueError, match="alpha"):
        stationary_bootstrap_ci([1.0, 2.0], alpha=1.5, resamples=10)


# --- Bootstrap por grupos -----------------------------------------------------------------


def test_cluster_bootstrap_resamples_whole_groups():
    groups = [[10.0] * 3, [20.0] * 5, []]  # el grupo vacío no cuenta
    sizes = cluster_bootstrap(groups, lambda s: float(len(s)), resamples=400, seed=7)
    assert set(sizes) == {6.0, 8.0, 10.0}  # 3 + 3, 3 + 5 o 5 + 5: nunca se parte un grupo
    means = cluster_bootstrap(groups, statistics.fmean, resamples=400, seed=7)
    assert set(means) == {10.0, 20.0, (3 * 10.0 + 5 * 20.0) / 8}


def test_cluster_bootstrap_is_wider_than_iid_with_correlated_groups():
    rng = random.Random(2)  # noqa: S311
    # 20 semanas; las 10 operaciones de cada semana se mueven juntas.
    groups = [[rng.gauss(0.01, 0.05)] * 10 for _ in range(20)]
    flat = [v for group in groups for v in group]
    a = cluster_bootstrap_ci(groups, resamples=2000)
    b = cluster_bootstrap_ci(groups, resamples=2000)
    assert a == b  # determinista con la semilla 7
    assert a != cluster_bootstrap_ci(groups, resamples=2000, seed=8)
    iid = stationary_bootstrap_ci(flat, blocks=(1,), resamples=2000)
    assert a[1] - a[0] > 2 * (iid[1] - iid[0])  # ≈ √10 veces más ancho
    assert a[0] < statistics.fmean(flat) < a[1]
    with pytest.raises(ValueError, match="grupo"):
        cluster_bootstrap_ci([[], []])


# --- PSR, DSR y MinTRL --------------------------------------------------------------------


def test_psr_known_value():
    # SR̂ = 0,1 diario, T = 101, normal (γ₃ = 0, γ₄ = 3): z = 0,1·√100 / √(1 + 0,5·0,01).
    z = 1.0 / math.sqrt(1.005)
    expected = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    assert psr(0.1, 0.0, 101, 0.0, 3.0) == pytest.approx(expected, abs=1e-12)
    assert psr(0.1, 0.0, 101, 0.0, 3.0) == pytest.approx(0.8407, abs=1e-4)
    assert psr(0.05, 0.05, 500, -1.0, 6.0) == pytest.approx(0.5)
    # Colas gruesas y asimetría negativa restan confianza.
    assert psr(0.1, 0.0, 101, -1.0, 8.0) < psr(0.1, 0.0, 101, 0.0, 3.0)


def test_dsr_with_one_configuration_is_psr_of_zero():
    sr, n, g3, g4 = 0.08, 900, -0.4, 5.0
    assert deflated_sharpe(sr, n, g3, g4, 1, 0.01) == psr(sr, 0.0, n, g3, g4)
    assert sr0(1, 0.5) == 0.0
    # Más configuraciones probadas → listón más alto → DSR más bajo.
    dsr3 = deflated_sharpe(sr, n, g3, g4, 3, 0.0)
    dsr8 = deflated_sharpe(sr, n, g3, g4, 8, 0.0)
    assert dsr8 < dsr3 < psr(sr, 0.0, n, g3, g4)
    # Suelo conservador de la varianza: V = max(varianza, 1/T).
    assert dsr3 == deflated_sharpe(sr, n, g3, g4, 3, 1 / n)
    assert dsr3 == psr(sr, sr0(3, 1 / n), n, g3, g4)
    assert deflated_sharpe(sr, n, g3, g4, 3, 0.01) == psr(sr, sr0(3, 0.01), n, g3, g4)


def test_sr0_factor_for_three_and_eight_configurations():
    # Con V = 1, SR₀ es el factor ((1 − γ)·Φ⁻¹(1 − 1/K) + γ·Φ⁻¹(1 − 1/(K·e))).
    assert sr0(3, 1.0) == pytest.approx(0.853, abs=1e-3)
    assert sr0(8, 1.0) == pytest.approx(1.459, abs=1e-3)
    factor3 = 0.4228 * PHI.inv_cdf(2 / 3) + 0.5772 * PHI.inv_cdf(1 - 1 / (3 * math.e))
    assert sr0(3, 1.0) == pytest.approx(factor3, rel=1e-12)
    assert sr0(3, 4.0) == pytest.approx(2 * sr0(3, 1.0))  # escala con √V
    with pytest.raises(ValueError):
        sr0(0, 1.0)


def test_min_trl():
    # 1 + (1 − 0 + (3 − 1)/4·0,01)·(1,645/0,1)² = 1 + 1,005·270,6025
    assert min_trl(0.1, 0.0, 3.0, 1.645) == pytest.approx(1 + 1.005 * 16.45**2)
    # Con n = MinTRL días, PSR(0) es exactamente Φ(z).
    n = min_trl(0.06, -0.5, 6.0, 2.89)
    sr, g3, g4 = 0.06, -0.5, 6.0
    z = sr * math.sqrt(n - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr**2)
    assert z == pytest.approx(2.89)
    assert PHI.cdf(z) == pytest.approx(PHI.cdf(2.89))
    # Más asimetría negativa y más curtosis → hacen falta más días.
    assert min_trl(0.06, -0.5, 6.0, 2.89) > min_trl(0.06, 0.0, 3.0, 2.89)
    assert min_trl(0.0, 0.0, 3.0, 1.645) is None
    assert min_trl(-0.02, 0.0, 3.0, 1.645) is None


# --- Spearman -----------------------------------------------------------------------------


def test_spearman_with_ties_uses_average_ranks():
    # Rangos de x: 1; 2,5; 2,5; 4. Rangos de y: 1; 3; 2; 4. Pearson de los rangos = 3/√10.
    x = [1.0, 2.0, 2.0, 3.0]
    y = [10.0, 30.0, 20.0, 40.0]
    assert spearman(x, y) == pytest.approx(3 / math.sqrt(10))
    assert spearman(x, y) == pytest.approx(statistics.correlation(x, y, method="ranked"))
    # Monótona pero no lineal: Spearman = 1; al revés: −1.
    assert spearman([1.0, 2.0, 3.0, 4.0], [1.0, 8.0, 27.0, 1000.0]) == pytest.approx(1.0)
    assert spearman([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_spearman_degenerate_cases():
    assert spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None  # todas empatadas
    assert spearman([1.0], [2.0]) is None
    with pytest.raises(ValueError, match="longitud"):
        spearman([1.0, 2.0], [1.0])

"""Protocolo del backtest de la regla v1 (apartado 8 del diseño).

Todo el histórico es **pseudo-fuera de muestra**: los parámetros no se ajustan a los
datos, pero quien los eligió conocía el mercado de esos años. Lo único fuera de muestra de
verdad es la prueba en vivo desde el preregistro.

Este módulo calcula; ``crypto/btreport.py`` lo cuenta en castellano.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from copybot.backtest import stats
from copybot.config import RiskLimits
from copybot.crypto import costs
from copybot.crypto.benchmarks import (
    Benchmark,
    btc_filtered,
    btc_hold,
    daily_returns,
    top10_basket,
    usdt,
)
from copybot.crypto.engine import (
    RunConfig,
    RunResult,
    SignalTrade,
    build_views,
    random_chooser,
    signals_book,
    simulate,
)
from copybot.crypto.panel import Panel
from copybot.crypto.strategy import STRATEGY_V1, VARIANTS, DayView, StrategyParams

MIN_UNIVERSE = 20  # la evaluación empieza el primer día con |U| ≥ 20
RANK_BUCKETS = ((1, 1), (2, 5), (6, 12), (13, 30), (31, 10_000))
HORIZONS_C = (28, 90, 365)
Progress = Callable[[str], None]


@dataclass(frozen=True)
class Period:
    name: str
    start: date
    end: date


@dataclass
class Excess:
    """Exceso diario logarítmico de una curva frente a BTC comprar y mantener."""

    days: int
    annual: float  # exp(365·ē) − 1
    ci_by_block: dict[float, tuple[float, float]]  # intervalo anual por longitud de bloque
    ir: float | None
    sr_daily: float | None
    skew: float | None
    kurt: float | None

    @property
    def ci_low(self) -> float:
        return min(low for low, _ in self.ci_by_block.values()) if self.ci_by_block else -1.0

    @property
    def ci_high(self) -> float:
        return max(high for _, high in self.ci_by_block.values()) if self.ci_by_block else 1.0


def _annual(mean_daily: float) -> float:
    return math.exp(365 * mean_daily) - 1


def excess_vs(curve, bench, *, seed: int = 7, resamples: int = 5000) -> Excess:
    """Exceso de ``curve`` frente a ``bench`` (mismas fechas)."""
    a = dict(curve)
    b = dict(bench)
    days = [d for d, _ in curve if d in b]
    ra = daily_returns([(d, a[d]) for d in days])
    rb = daily_returns([(d, b[d]) for d in days])
    e = stats.log_excess(ra, rb)
    if len(e) < 2:
        return Excess(len(e), 0.0, {}, None, None, None, None)
    by_block = stats.stationary_bootstrap_ci_by_block(e, resamples=resamples, seed=seed)
    ci = {blk: (_annual(lo), _annual(hi)) for blk, (lo, hi) in by_block.items()}
    return Excess(
        len(e),
        _annual(statistics.fmean(e)),
        ci,
        stats.information_ratio(e),
        stats.sharpe_ratio(e),
        stats.skewness(e),
        stats.kurtosis(e),
    )


def max_drawdown(curve) -> float:
    peak, worst = 0.0, 0.0
    for _, v in curve:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, 1 - v / peak)
    return worst


def curve_metrics(curve, worst_curve=None) -> dict:
    values = [v for _, v in curve]
    rets = daily_returns(curve)
    years = max(len(values) - 1, 1) / 365
    total = values[-1] / values[0] - 1 if values and values[0] else 0.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    down = [r for r in rets if r < 0]
    dd_sd = math.sqrt(sum(r * r for r in down) / len(rets)) if rets else 0.0
    mean = statistics.fmean(rets) if rets else 0.0
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    mdd = max_drawdown(worst_curve or curve)
    months: dict[tuple[int, int], list[float]] = defaultdict(list)
    for d, v in curve:
        months[(d.year, d.month)].append(v)
    monthly = []
    prev = None
    for key in sorted(months):
        end = months[key][-1]
        if prev is not None:
            monthly.append(end / prev - 1)
        prev = end
    return {
        "total": total,
        "anual": cagr,
        "caida_maxima": mdd,
        "sharpe": mean / sd * math.sqrt(365) if sd else None,
        "sortino": mean / dd_sd * math.sqrt(365) if dd_sd else None,
        "calmar": cagr / mdd if mdd else None,
        "peor_mes": min(monthly) if monthly else None,
    }


@dataclass
class BacktestResult:
    start: date
    end: date
    days: list[date]
    universe_sizes: dict[date, int]
    curve_a: RunResult
    curve_b: RunResult
    variants: dict[str, RunResult]
    stress: dict[str, RunResult]
    benchmarks: dict[str, Benchmark]
    excess_a: Excess
    excess_variants: dict[str, Excess]
    excess_stress: dict[str, Excess]
    subperiods: dict[str, dict]
    signals: list[SignalTrade]
    signals_excess: dict
    calibration: dict[str, dict]
    ic: dict
    quintiles: list[float]
    null_finals: list[float]
    null_percentile: float
    starts: dict[int, dict]
    survival: dict
    top3: list[str]
    data_hash: str
    params_hash: str
    cost_table_hash: str
    seed: int
    extra: dict = field(default_factory=dict)


def data_hash(panel: Panel) -> str:
    h = hashlib.sha256()
    for symbol in sorted(panel.series):
        s = panel.series[symbol]
        for k in range(len(s.days)):
            h.update(
                f"{symbol}|{s.days[k]}|{s.open[k]!r}|{s.high[k]!r}|{s.low[k]!r}|"
                f"{s.close[k]!r}|{s.quote_volume[k]!r}|{s.trades[k]}\n".encode()
            )
    return h.hexdigest()


def evaluation_days(panel: Panel, views: dict[date, DayView], end: date) -> list[date]:
    days = sorted(d for d, v in views.items() if d <= end)
    first = next((d for d in days if len(views[d].universe.members) >= MIN_UNIVERSE), None)
    if first is None:
        return []
    out, day = [], first
    while day <= end:
        out.append(day)
        day += timedelta(days=1)
    return out


def _forward(panel: Panel, symbol: str, day: date, horizon: int, view: DayView) -> float | None:
    """Rentabilidad neta de costes de la apertura de t+1 a la de t+1+horizon."""
    s = panel.series.get(symbol)
    if s is None:
        return None
    i0 = s.at(day + timedelta(days=1))
    i1 = s.at(day + timedelta(days=1 + horizon))
    if i0 is None or i1 is None:
        return None
    qv = next((m.median_quote_volume for m in view.universe.members if m.symbol == symbol), 5e6)
    back = costs.sell_usd(costs.buy_qty(1.0, s.open[i0], qv), s.open[i1], qv)
    return back - 1


def calibration(
    panel: Panel, views: dict[date, DayView], days: list[date], *, horizon: int = 7, seed: int = 7
) -> tuple[dict[str, dict], dict, list[float]]:
    """Por tramos de puesto: exceso frente a BTC a 7 días y % de veces que lo bate; IC de
    rangos (Spearman entre puntos y rentabilidad) y rentabilidad por quintil."""
    by_bucket: dict[str, list[float]] = defaultdict(list)  # media diaria por tramo
    beats: dict[str, list[float]] = defaultdict(list)
    ics: list[float] = []
    quint: list[list[float]] = [[] for _ in range(5)]
    for day in days:
        view = views.get(day)
        if view is None or not view.ranked:
            continue
        btc = _forward(panel, "BTCUSDT", day, horizon, view)
        if btc is None:
            continue
        rows = []
        for sig in view.ranked:
            r = _forward(panel, sig.symbol, day, horizon, view)
            if r is not None:
                rows.append((sig, r))
        if len(rows) < 5:
            continue
        for lo, hi in RANK_BUCKETS:
            sel = [math.log1p(r) - math.log1p(btc) for sig, r in rows if lo <= sig.rank <= hi]
            if sel:
                key = f"{lo}" if lo == hi else (f"{lo}-{hi}" if hi < 10_000 else f"{lo}+")
                by_bucket[key].append(statistics.fmean(sel))
                beats[key].append(sum(1 for x in sel if x > 0) / len(sel))
        rho = stats.spearman([s.score for s, _ in rows], [r for _, r in rows])
        if rho is not None:
            ics.append(rho)
        ordered = sorted(rows, key=lambda x: x[0].score)
        n = len(ordered)
        for q in range(5):
            chunk = ordered[q * n // 5 : (q + 1) * n // 5]
            if chunk:
                quint[q].append(statistics.fmean(r for _, r in chunk))
    cal = {}
    for key, values in by_bucket.items():
        lo, hi = stats.stationary_bootstrap_ci(values, blocks=(28,), seed=seed, resamples=2000)
        cal[key] = {
            "dias": len(values),
            "exceso_medio": statistics.fmean(values),
            "ic": (lo, hi),
            "bate_btc": statistics.fmean(beats[key]),
        }
    ic = {}
    if len(ics) > 2:
        lo, hi = stats.stationary_bootstrap_ci(ics, blocks=(28,), seed=seed, resamples=2000)
        ic = {
            "media": statistics.fmean(ics),
            "dias_positivo": sum(1 for x in ics if x > 0) / len(ics),
            "ic": (lo, hi),
            "dias": len(ics),
        }
    return cal, ic, [statistics.fmean(q) if q else 0.0 for q in quint]


def signals_summary(trades: list[SignalTrade], seed: int = 7) -> dict:
    """Exceso por operación frente a BTC, agregado primero por día de entrada."""
    by_day: dict[date, list[float]] = defaultdict(list)
    for t in trades:
        by_day[t.day].append(t.excess)
    daily = [statistics.fmean(v) for _, v in sorted(by_day.items())]
    if len(daily) < 3:
        return {"operaciones": len(trades), "dias": len(daily)}
    ci = {
        blk: stats.stationary_bootstrap_ci(daily, blocks=(blk,), seed=seed, resamples=2000)
        for blk in (7, 28)
    }
    return {
        "operaciones": len(trades),
        "dias": len(daily),
        "exceso_medio": statistics.fmean(daily),
        "ic": ci,
        "bate_btc": sum(1 for t in trades if t.excess > 0) / len(trades),
    }


def monday_starts(
    panel: Panel,
    views: dict[date, DayView],
    days: list[date],
    params: StrategyParams,
    limits: RiskLimits,
    btc: Benchmark,
    *,
    horizons=HORIZONS_C,
) -> dict[int, dict]:
    """Curva C: una cartera nueva de 100 $ cada lunes, a 28, 90 y 365 días."""
    btc_close = dict(btc.curve)
    out = {}
    for h in horizons:
        rets, vs_btc, halted_day, halted_50 = [], [], 0, 0
        for k, day in enumerate(days):
            if day.weekday() != 0 or k + h >= len(days):
                continue
            window = days[k : k + h + 1]
            cfg = RunConfig(params, limits, mode="dolares", apply_drawdown_halt=True)
            run = simulate(panel, views, window, cfg, keep_decisions=True)
            r = run.final / cfg.initial - 1
            b0, b1 = btc_close.get(window[0]), btc_close.get(window[-1])
            rets.append(r)
            if b0 and b1:
                vs_btc.append(r > b1 / b0 - 1)
            if any(code == "daily_loss" for d in run.decisions for code, _ in d.halts):
                halted_day += 1
            if run.halted_on is not None:
                halted_50 += 1
        if not rets:
            continue
        ordered = sorted(rets)
        out[h] = {
            "arranques": len(rets),
            "pierden": sum(1 for r in rets if r < 0) / len(rets),
            "mediana": statistics.median(rets),
            "p5": ordered[int(0.05 * (len(ordered) - 1))],
            "p95": ordered[int(0.95 * (len(ordered) - 1))],
            "peor": ordered[0],
            "baten_btc": sum(vs_btc) / len(vs_btc) if vs_btc else None,
            "tocan_parada_diaria": halted_day / len(rets),
            "tocan_parada_50": halted_50 / len(rets),
        }
    return out


def _subperiods(start: date, end: date) -> list[Period]:
    mid = start + (end - start) / 2
    out = [
        Period("inicio-2020", start, min(end, date(2020, 12, 31))),
        Period("2021-2022", date(2021, 1, 1), date(2022, 12, 31)),
        Period("2023-hoy", date(2023, 1, 1), end),
        Period("primera mitad", start, mid),
        Period("segunda mitad", mid + timedelta(days=1), end),
    ]
    out += [
        Period(str(y), max(start, date(y, 1, 1)), min(end, date(y, 12, 31)))
        for y in range(start.year, end.year + 1)
    ]
    return [p for p in out if p.start < p.end and p.start >= start and p.end <= end]


def _slice(curve, period: Period):
    return [(d, v) for d, v in curve if period.start <= d <= period.end]


def top_contributors(run: RunResult, n: int = 3) -> list[str]:
    pnl: dict[str, float] = defaultdict(float)
    for t in run.trades:
        pnl[t.symbol] += t.pnl
    return [s for s, _ in sorted(pnl.items(), key=lambda kv: -kv[1])[:n]]


def run_backtest(
    panel: Panel,
    *,
    end: date,
    limits: RiskLimits,
    seed: int = 7,
    null_seeds: int = 1000,
    params: StrategyParams = STRATEGY_V1,
    progress: Progress | None = None,
) -> BacktestResult:
    say = progress or (lambda _m: None)
    all_days = sorted(d for d in panel.days if d <= end)
    say(f"Universo y puestos de {len(all_days)} días…")
    views = build_views(panel, all_days, params)
    days = evaluation_days(panel, views, end)
    if not days:
        raise ValueError("no hay ningún día con al menos 20 monedas en el universo")
    start = days[0]
    sizes = {d: len(views[d].universe.members) for d in days if d in views}

    def curve_a(p: StrategyParams, vws=None, **kw) -> RunResult:
        cfg = RunConfig(p, limits, mode="porcentaje", **kw)
        return simulate(panel, vws or views, days, cfg)

    say("Curvas A y B, variantes y referencias…")
    a = curve_a(params)
    b = simulate(
        panel,
        views,
        days,
        RunConfig(params, limits, mode="dolares", apply_drawdown_halt=True),
        keep_decisions=True,
    )
    variants = {k: curve_a(v) for k, v in VARIANTS.items()}
    bench = {
        "usdt": usdt(days),
        "btc": btc_hold(panel, days),
        "btc_filtro": btc_filtered(panel, views, days),
        "btc90": btc_hold(panel, days, share=0.9),
        "cesta10": top10_basket(panel, views, days),
    }
    btc = bench["btc"].curve

    say("Pruebas de estrés…")
    stress = {
        "costes_x2": curve_a(params, cost_mult=2.0),
        "costes_x3": curve_a(params, cost_mult=3.0),
        "un_dia_tarde": curve_a(params, exec_delay=2),
        "retirada_0": curve_a(params, delist_loss=0.0),
        "retirada_100": curve_a(params, delist_loss=1.0),
    }
    top3 = top_contributors(a)
    say(f"Sin las 3 que más aportaron ({', '.join(top3)})…")
    p_top = replace(params, universe=replace(params.universe, exclude=frozenset(top3)))
    stress["sin_top3"] = curve_a(p_top, build_views(panel, days, p_top))
    dead = frozenset(s for s, ser in panel.series.items() if ser.status != "TRADING")
    say("Solo supervivientes…")
    p_surv = replace(params, universe=replace(params.universe, exclude=dead))
    stress["solo_supervivientes"] = curve_a(p_surv, build_views(panel, days, p_surv))

    excess_a = excess_vs(a.curve, btc, seed=seed)
    excess_variants = {k: excess_vs(v.curve, btc, seed=seed) for k, v in variants.items()}
    excess_stress = {k: excess_vs(v.curve, btc, seed=seed) for k, v in stress.items()}
    subperiods = {}
    for per in _subperiods(start, end):
        ca, cb = _slice(a.curve, per), _slice(btc, per)
        if len(ca) > 30:
            subperiods[per.name] = {
                "desde": per.start,
                "hasta": per.end,
                "regla": curve_metrics(ca),
                "btc": curve_metrics(cb),
                "btc_filtro": curve_metrics(_slice(bench["btc_filtro"].curve, per)),
                "cesta10": curve_metrics(_slice(bench["cesta10"].curve, per)),
                "exceso": excess_vs(ca, cb, seed=seed, resamples=1000).annual,
            }

    say("Libro de señales y calibración por puesto…")
    signals = signals_book(panel, views, days, params)
    cal, ic, quint = calibration(panel, views, days, seed=seed)

    say(f"Nulo aleatorio ({null_seeds} carteras)…")
    durations = [
        (t.exit_day - t.entry_day).days
        for t in a.trades
        if t.exit_purpose == "puesto" and t.exit_day is not None
    ]
    finals = []
    for s in range(1, null_seeds + 1):
        chooser = random_chooser(s, durations, params.n_positions)
        cfg = RunConfig(params, limits, mode="porcentaje")
        finals.append(simulate(panel, views, days, cfg, chooser=chooser).final)
    pct = 100 * sum(1 for f in finals if f < a.final) / len(finals) if finals else 0.0

    say("Arranques de cada lunes (28, 90 y 365 días)…")
    starts = monday_starts(panel, views, days, params, limits, bench["btc"])

    ever = {m.symbol for d in days if d in views for m in views[d].universe.members}
    held_dead_days = sum(
        1
        for d in days
        if d in views
        and any(t.symbol in dead for t in a.trades if t.entry_day <= d <= (t.exit_day or end))
    )
    survival = {
        "en_universo_alguna_vez": len(ever),
        "ya_no_cotizan": len(ever & dead),
        "dias_con_retiradas": held_dead_days / max(len(days), 1),
    }
    return BacktestResult(
        start=start,
        end=end,
        days=days,
        universe_sizes=sizes,
        curve_a=a,
        curve_b=b,
        variants=variants,
        stress=stress,
        benchmarks=bench,
        excess_a=excess_a,
        excess_variants=excess_variants,
        excess_stress=excess_stress,
        subperiods=subperiods,
        signals=signals,
        signals_excess=signals_summary(signals, seed),
        calibration=cal,
        ic=ic,
        quintiles=quint,
        null_finals=finals,
        null_percentile=pct,
        starts=starts,
        survival=survival,
        top3=top3,
        data_hash=data_hash(panel),
        params_hash=params.params_hash(),
        cost_table_hash=costs.cost_table_hash(),
        seed=seed,
    )


def deflated(result: BacktestResult, k: int) -> float | None:
    """Sharpe deflactado del exceso diario de la curva A con las K configuraciones."""
    e = result.excess_a
    if e.sr_daily is None or e.skew is None or e.kurt is None:
        return None
    srs = [x.sr_daily for x in [e, *result.excess_variants.values()] if x.sr_daily is not None]
    variance = statistics.pvariance(srs) if len(srs) > 1 else 0.0
    variance = max(variance, 1 / max(e.days, 1))
    return stats.deflated_sharpe(e.sr_daily, e.days, e.skew, e.kurt, k, variance)


def pairwise_days(days: list[date]) -> list[tuple[date, date]]:
    return list(itertools.pairwise(days))

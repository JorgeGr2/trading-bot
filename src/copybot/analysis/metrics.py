"""Métricas de una wallet en una ventana ``(as_of − días, as_of]``.

Las posiciones se reconstruyen con todos los eventos hasta ``as_of`` (para conocer el
coste de lo comprado antes de la ventana), pero solo cuentan para la ventana las ventas
y las operaciones cerradas dentro de ella. Nada posterior a ``as_of`` interviene.

Para copiar con un tamaño fijo por operación, lo que importa es la **rentabilidad
media por operación** (cada operación pesa lo mismo), no el PnL en dólares del trader,
que dominan sus apuestas más grandes. Por eso se calculan las dos.

La serie **prudente** de rentabilidades incluye también las posiciones abandonadas
(abiertas y sin movimiento desde hace más de ``stale_open_hours``), valorando a 0 lo
que queda. Sin ellas, quien vende sus aciertos y se queda con las pérdidas parecería
rentable: solo se verían las operaciones que cerró.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from copybot.analysis.positions import ZERO, Ledger, RoundTrip, Trade, build_ledger

# Cambiarla cuando cambie el cálculo: forma parte del params_hash de los snapshots.
# 2: serie prudente con las posiciones abandonadas; abandono según la última actividad.
ANALYZER_VERSION = "2"


class AnalysisParams(BaseSettings):
    """Parámetros del cálculo de métricas (variables ``COPYBOT_ANALYSIS_*``)."""

    model_config = SettingsConfigDict(env_prefix="COPYBOT_ANALYSIS_", extra="forbid")

    # Una operación se da por cerrada cuando queda como mucho esta fracción de la
    # cantidad máxima que llegó a tener.
    close_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0, lt=Decimal("0.2"))
    # Una posición abierta sin movimientos durante más de estas horas se considera
    # abandonada y, en el PnL y la rentabilidad prudentes, lo que queda se valora a 0.
    stale_open_hours: int = Field(default=72, gt=0, le=24 * 30)
    # Salidas «rápidas»: la primera venta llega antes de estos segundos.
    quick_exit_seconds: int = Field(default=60, gt=0, le=3600)
    # Compra «en el lanzamiento» (sniping): dentro de estos segundos desde la creación.
    early_buy_seconds: int = Field(default=60, gt=0, le=3600)
    # Para la media robusta, la rentabilidad de cada operación se limita a +200 %.
    return_clip: Decimal = Field(default=Decimal("2"), gt=0, le=100)
    # Margen para considerar que el historial importado llega hasta ``as_of``.
    coverage_tolerance_minutes: int = Field(default=60, ge=0, le=24 * 60)


@dataclass(frozen=True)
class Coverage:
    covered_from: datetime
    covered_to: datetime
    truncated: bool = False


@dataclass
class WindowMetrics:
    as_of: datetime
    window_days: int
    window_start: datetime
    # Datos
    coverage_complete: bool
    events_total: int  # eventos hasta as_of usados para reconstruir posiciones
    buys: int
    sells: int
    transfers_in: int
    transfers_out: int
    ambiguous: int
    medium_confidence: int
    non_usd_trades: int
    tokens_traded: int
    active_days: int
    trades_per_day: Decimal
    buy_volume_usd: Decimal
    sell_volume_usd: Decimal
    # PnL realizado (ventas dentro de la ventana)
    realized_pnl_usd: Decimal
    realized_cost_usd: Decimal
    # Operaciones cerradas dentro de la ventana con coste conocido
    round_trips: int
    wins: int
    win_rate: Decimal | None
    profit_factor: Decimal | None
    mean_return: Decimal | None
    median_return: Decimal | None
    clipped_mean_return: Decimal | None
    clipped_stdev_return: Decimal | None
    # Serie prudente: cerradas + abandonadas (lo que queda, a 0), con el mismo tope.
    abandoned_round_trips: int
    prudent_round_trips: int
    prudent_mean_return: Decimal | None
    prudent_stdev_return: Decimal | None
    prudent_mean_without_top3: Decimal | None
    first_half_mean_return: Decimal | None  # serie prudente
    second_half_mean_return: Decimal | None
    round_trip_pnl_usd: Decimal
    pnl_without_best_usd: Decimal
    pnl_without_top3_usd: Decimal
    top1_profit_share: Decimal | None
    # Tiempos
    median_hold_seconds: Decimal | None
    median_seconds_to_first_sell: Decimal | None
    quick_exit_share: Decimal | None
    same_slot_exits: int
    # Riesgo
    peak_open_cost_usd: Decimal
    max_drawdown_usd: Decimal
    max_drawdown_pct: Decimal | None
    open_positions: int
    open_cost_usd: Decimal
    stale_open_cost_usd: Decimal
    conservative_pnl_usd: Decimal
    # Calidad de los datos y comportamientos dudosos
    unmatched_sell_usd: Decimal
    unmatched_sell_share: Decimal | None
    free_tokens_sold_usd: Decimal
    trips_ended_by_transfer: int
    # Sniping (solo operaciones abiertas en la ventana cuyo token tiene fecha de creación)
    launch_checked: int
    launch_unchecked: int
    early_buys: int
    early_buy_share: Decimal | None
    median_token_age_seconds: Decimal | None

    def to_json(self) -> dict[str, Any]:
        return {k: _jsonable(v) for k, v in asdict(self).items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.000001")).normalize())
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _median(values: list[Decimal]) -> Decimal | None:
    return statistics.median(values) if values else None


def _mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, ZERO) / len(values) if values else None


def _stdev(values: list[Decimal]) -> Decimal | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def _share(part: int | Decimal, total: int | Decimal) -> Decimal | None:
    return Decimal(part) / Decimal(total) if total else None


def compute_window_metrics(
    trades: list[Trade],
    *,
    as_of: datetime,
    window_days: int,
    params: AnalysisParams,
    coverage: Coverage | None,
    token_created_at: dict[str, datetime | None] | None = None,
    ledger: Ledger | None = None,
) -> WindowMetrics:
    if window_days <= 0:
        raise ValueError("window_days debe ser positivo")
    known = [t for t in trades if t.time <= as_of]
    if ledger is None:
        ledger = build_ledger(known, as_of, params.close_tolerance)
    start = as_of - timedelta(days=window_days)
    mid = start + (as_of - start) / 2

    def in_window(moment: datetime) -> bool:
        return start < moment <= as_of

    window_trades = [t for t in known if in_window(t.time)]
    trading = [t for t in window_trades if t.kind in ("buy", "sell")]
    buys = [t for t in trading if t.kind == "buy"]
    sells = [t for t in trading if t.kind == "sell"]

    # --- PnL realizado y curva de capital ---------------------------------------
    realized = [r for r in ledger.realizations if in_window(r.time)]
    pnl = sum((r.pnl for r in realized), ZERO)
    peak = running = ZERO
    max_dd = ZERO
    for r in sorted(realized, key=lambda r: r.time):
        running += r.pnl
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    carried = [v.usd for v in ledger.open_cost_curve if v.time <= start and v.usd is not None]
    capital = [v.usd for v in ledger.open_cost_curve if in_window(v.time) and v.usd is not None]
    peak_capital = max([*(carried[-1:]), *capital], default=ZERO)

    # --- Operaciones cerradas en la ventana -------------------------------------
    closed: list[RoundTrip] = [
        t for t in ledger.round_trips if t.measurable and t.closed_at and in_window(t.closed_at)
    ]
    returns = [t.roi for t in closed if t.roi is not None]
    clipped = [min(r, params.return_clip) for r in returns]
    trip_pnls = sorted((t.pnl for t in closed), reverse=True)
    gains = sum((p for p in trip_pnls if p > 0), ZERO)
    losses = -sum((p for p in trip_pnls if p < 0), ZERO)
    holds = [h for t in closed if (h := t.hold_seconds) is not None]
    first_sells = [s for t in closed if (s := t.seconds_to_first_sell) is not None]

    # --- Posiciones abiertas en as_of y serie prudente --------------------------
    open_trips = ledger.open_trips()
    stale_limit = as_of - timedelta(hours=params.stale_open_hours)
    abandoned = [
        t
        for t in open_trips
        if in_window(t.opened_at) and (t.last_activity_at or t.opened_at) < stale_limit
    ]
    stale_cost = sum((t.remaining_cost for t in abandoned), ZERO)
    prudent: list[tuple[datetime, Decimal]] = [
        (t.closed_at, min(t.roi, params.return_clip))
        for t in closed
        if t.closed_at is not None and t.roi is not None
    ]
    prudent += [
        (t.last_activity_at or t.opened_at, min(roi, params.return_clip))
        for t in abandoned
        if (roi := t.abandoned_roi) is not None
    ]
    prudent_values = [r for _, r in prudent]
    first_half = [r for moment, r in prudent if moment <= mid]
    second_half = [r for moment, r in prudent if moment > mid]

    # --- Sniping: edad del token en la compra que abre cada operación -----------
    created = token_created_at or {}
    opened_here = [t for t in ledger.round_trips if in_window(t.opened_at) and t.buys > 0]
    ages: list[Decimal] = []
    unchecked = 0
    for trip in opened_here:
        created_at = created.get(trip.mint)
        if created_at is None:
            unchecked += 1
            continue
        ages.append(Decimal(max(0.0, (trip.opened_at - created_at).total_seconds())))
    early = sum(1 for a in ages if a <= params.early_buy_seconds)

    sell_volume = sum((t.usd for t in sells if t.usd is not None), ZERO)
    unmatched = sum(
        (v.usd for v in ledger.unmatched_sales if in_window(v.time) and v.usd is not None), ZERO
    )
    covered = coverage is not None and (
        coverage.covered_from <= start
        and coverage.covered_to >= as_of - timedelta(minutes=params.coverage_tolerance_minutes)
    )
    return WindowMetrics(
        as_of=as_of,
        window_days=window_days,
        window_start=start,
        coverage_complete=covered,
        events_total=len(known),
        buys=len(buys),
        sells=len(sells),
        transfers_in=sum(1 for t in window_trades if t.kind == "transfer_in"),
        transfers_out=sum(1 for t in window_trades if t.kind == "transfer_out"),
        ambiguous=sum(1 for t in window_trades if t.kind == "ambiguous"),
        medium_confidence=sum(1 for t in trading if t.confidence != "high"),
        non_usd_trades=sum(1 for t in trading if t.usd is None),
        tokens_traded=len({t.mint for t in trading}),
        active_days=len({t.time.date() for t in trading}),
        trades_per_day=Decimal(len(trading)) / window_days,
        buy_volume_usd=sum((t.usd for t in buys if t.usd is not None), ZERO),
        sell_volume_usd=sell_volume,
        realized_pnl_usd=pnl,
        realized_cost_usd=sum((r.cost for r in realized), ZERO),
        round_trips=len(closed),
        wins=sum(1 for p in trip_pnls if p > 0),
        win_rate=_share(sum(1 for p in trip_pnls if p > 0), len(closed)),
        profit_factor=gains / losses if losses > 0 else None,
        mean_return=_mean(returns),
        median_return=_median(returns),
        clipped_mean_return=_mean(clipped),
        clipped_stdev_return=_stdev(clipped),
        abandoned_round_trips=len(abandoned),
        prudent_round_trips=len(prudent_values),
        prudent_mean_return=_mean(prudent_values),
        prudent_stdev_return=_stdev(prudent_values),
        prudent_mean_without_top3=_mean(sorted(prudent_values, reverse=True)[3:]),
        first_half_mean_return=_mean(first_half),
        second_half_mean_return=_mean(second_half),
        round_trip_pnl_usd=sum(trip_pnls, ZERO),
        pnl_without_best_usd=sum(trip_pnls[1:], ZERO),
        pnl_without_top3_usd=sum(trip_pnls[3:], ZERO),
        top1_profit_share=trip_pnls[0] / gains if gains > 0 else None,
        median_hold_seconds=_median(holds),
        median_seconds_to_first_sell=_median(first_sells),
        quick_exit_share=_share(
            sum(1 for s in first_sells if s < params.quick_exit_seconds), len(first_sells)
        ),
        same_slot_exits=sum(1 for t in opened_here if t.same_slot_exit),
        peak_open_cost_usd=peak_capital,
        max_drawdown_usd=max_dd,
        max_drawdown_pct=max_dd / peak_capital if peak_capital > 0 else None,
        open_positions=len(open_trips),
        open_cost_usd=sum((t.remaining_cost for t in open_trips), ZERO),
        stale_open_cost_usd=stale_cost,
        conservative_pnl_usd=pnl - stale_cost,
        unmatched_sell_usd=unmatched,
        unmatched_sell_share=unmatched / sell_volume if sell_volume > 0 else None,
        free_tokens_sold_usd=sum(
            (v.usd for v in ledger.free_token_sales if in_window(v.time) and v.usd is not None),
            ZERO,
        ),
        trips_ended_by_transfer=sum(
            1
            for t in ledger.round_trips
            if t.ended_by_transfer and t.closed_at and in_window(t.closed_at)
        ),
        launch_checked=len(ages),
        launch_unchecked=unchecked,
        early_buys=early,
        early_buy_share=_share(early, len(ages)),
        median_token_age_seconds=_median(ages),
    )

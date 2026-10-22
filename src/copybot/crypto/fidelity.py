"""Fidelidad: la cartera en vivo debe decidir lo mismo que el backtest.

Cada día se vuelve a pasar el motor del backtest sobre los días en vivo de la cartera,
con las mismas velas y el mismo estado inicial, y se comparan las **decisiones**
(moneda, compra o venta, y motivo). Las diferencias de tamaño por los precios de
ejecución no cuentan aquí: se miden con la brecha de ejecución. Una decisión distinta sin
explicar es un fallo de software (luz en vivo en ROJO) hasta que se anota.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.config import RiskLimits
from copybot.crypto.engine import RunConfig, simulate
from copybot.crypto.panel import Panel
from copybot.crypto.strategy import STRATEGY_V1, DayView, StrategyParams
from copybot.db.models import CryptoPaperDay, CryptoPaperOrder, CryptoPaperRun

Decisions = dict[date, set[tuple[str, str, str]]]


@dataclass(frozen=True)
class FidelityReport:
    days_checked: int
    mismatches: dict[date, tuple[set, set]]  # día → (solo en vivo, solo en la réplica)
    explained: set[date]

    @property
    def unexplained(self) -> list[date]:
        return sorted(d for d in self.mismatches if d not in self.explained)

    @property
    def ok(self) -> bool:
        return not self.unexplained


def replay_decisions(
    panel: Panel,
    views: dict[date, DayView],
    days: list[date],
    limits: RiskLimits,
    params: StrategyParams = STRATEGY_V1,
) -> Decisions:
    """Decisiones que tomaría el backtest con la cartera de 100 $ en esos días."""
    cfg = RunConfig(params, limits, mode="dolares", apply_drawdown_halt=True)
    run = simulate(panel, views, days, cfg, keep_decisions=True)
    out: Decisions = defaultdict(set)
    for d in run.decisions:
        for o in d.orders:
            out[d.day].add((o.symbol, o.side, o.purpose))
    return out


async def live_decisions(sessions: async_sessionmaker[AsyncSession], run_id: int) -> Decisions:
    """Decisiones de la cartera en vivo (todas las órdenes, también las rechazadas: la
    decisión es la misma aunque no se pudiera ejecutar)."""
    async with sessions() as s:
        rows = await s.scalars(
            select(CryptoPaperOrder).where(
                CryptoPaperOrder.run_id == run_id, CryptoPaperOrder.book == "portfolio"
            )
        )
        out: Decisions = defaultdict(set)
        for o in rows:
            if o.purpose == "fin_prueba":
                continue
            out[o.day].add((o.symbol, o.side, o.purpose))
    return out


def compare(live: Decisions, replay: Decisions, days: list[date], explained=()) -> FidelityReport:
    mismatches = {}
    for day in days:
        a, b = live.get(day, set()), replay.get(day, set())
        if a != b:
            mismatches[day] = (a - b, b - a)
    return FidelityReport(len(days), mismatches, set(explained))


async def check_run(
    sessions: async_sessionmaker[AsyncSession],
    run_id: int,
    panel: Panel,
    views: dict[date, DayView],
    params: StrategyParams = STRATEGY_V1,
) -> FidelityReport:
    async with sessions() as s:
        run = await s.get(CryptoPaperRun, run_id)
        days_rows = list(
            await s.scalars(
                select(CryptoPaperDay)
                .where(CryptoPaperDay.run_id == run_id)
                .order_by(CryptoPaperDay.day)
            )
        )
    if run is None:
        raise ValueError(f"no existe la cartera {run_id}")
    days = [d.day for d in days_rows]
    explained = {d.day for d in days_rows if d.notes}
    limits = RiskLimits.model_validate(run.limits)
    replay = replay_decisions(panel, views, days, limits, params)
    report = compare(await live_decisions(sessions, run_id), replay, days, explained)
    async with sessions.begin() as s:
        for row in days_rows:
            fresh = await s.get(CryptoPaperDay, (run_id, row.day))
            if fresh is not None:
                fresh.fidelity_ok = row.day not in report.mismatches or row.day in explained
    return report

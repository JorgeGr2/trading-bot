"""Fidelidad: la cartera en vivo debe decidir lo mismo que el backtest.

Cada día se vuelve a pasar el motor del backtest sobre los días en vivo de la cartera,
con las mismas velas y el mismo estado inicial, y se comparan las **decisiones**
(moneda, compra o venta, y motivo). Las diferencias de tamaño por los precios de
ejecución no cuentan aquí: se miden con la brecha de ejecución. Una decisión distinta sin
explicar es un fallo de software (luz en vivo en ROJO) hasta que se anota.

La réplica recorre el calendario completo desde el inicio de la cartera: un día sin paso
en vivo (perdido, sin datos o que falta) no tiene decisión, pero lo decidido el día
anterior se ejecuta igual, como en vivo. Solo se comparan los días con paso.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.config import RiskLimits
from copybot.crypto.engine import RunConfig, simulate
from copybot.crypto.panel import Panel
from copybot.crypto.strategy import STRATEGY_V1, DayView, StrategyParams, decide
from copybot.db.models import CryptoPaperDay, CryptoPaperEvent, CryptoPaperOrder, CryptoPaperRun

Decisions = dict[date, set[tuple[str, str, str]]]
# Órdenes en vivo que no salen de ``decide()``: el fin de una prueba y las retiradas (7 días
# sin cotizar, que el backtest tampoco cuenta como decisión, o un par que deja de estar en
# TRADING, regla que solo existe en vivo).
NOT_DECISIONS = frozenset({"fin_prueba", "retirada"})


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
    *,
    blocked: dict[date, frozenset[str]] | None = None,
    initial: float = 100.0,
) -> Decisions:
    """Decisiones que tomaría el backtest con la cartera de 100 $ en esos días. Un día sin
    vista no tiene decisión (pero se ejecuta lo pendiente). ``blocked``: las monedas que
    los filtros solo en vivo no dejaron comprar cada día, para decidir igual que en vivo."""
    cfg = RunConfig(params, limits, mode="dolares", apply_drawdown_halt=True, initial=initial)
    chooser = None
    if blocked:

        def chooser(view, d, book):  # ``book``: la cartera que ve la decisión de ese día
            day_blocked = blocked.get(view.day)
            if not day_blocked:
                return d
            bv = replace(book, blocked=day_blocked)
            return decide(view, bv, params, limits, mode=cfg.mode, apply_drawdown_halt=True)

    run = simulate(panel, views, days, cfg, keep_decisions=True, chooser=chooser)
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
            if o.purpose in NOT_DECISIONS:
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


async def live_blocked(
    sessions: async_sessionmaker[AsyncSession], run_id: int
) -> dict[date, frozenset[str]]:
    """Monedas bloqueadas en vivo cada día (eventos «bloqueo» del paso diario)."""
    async with sessions() as s:
        rows = await s.scalars(
            select(CryptoPaperEvent.details).where(
                CryptoPaperEvent.run_id == run_id, CryptoPaperEvent.kind == "bloqueo"
            )
        )
        out: dict[date, frozenset[str]] = {}
        for details in rows:
            day = date.fromisoformat(details["dia"])
            out[day] = out.get(day, frozenset()) | frozenset(details.get("bloqueadas") or {})
    return out


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
    ok_days = [d.day for d in days_rows if d.status == "ok"]
    explained = {d.day for d in days_rows if d.notes}
    limits = RiskLimits.model_validate(run.limits)
    if days_rows:
        first, last = min(run.start_day, days_rows[0].day), days_rows[-1].day
        calendar = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    else:
        calendar = []
    replay = replay_decisions(
        panel,
        {d: views[d] for d in ok_days if d in views},
        calendar,
        limits,
        params,
        blocked=await live_blocked(sessions, run_id),
        initial=float(run.initial_usd),
    )
    if run.kind == "prueba" and run.end_day is not None:
        # El último día de una prueba no se compra: lo vende todo a continuación.
        for day in [d for d in replay if d >= run.end_day]:
            replay[day] = {o for o in replay[day] if o[1] != "buy"}
    report = compare(await live_decisions(sessions, run_id), replay, ok_days, explained)
    async with sessions.begin() as s:
        for row in days_rows:
            if row.status != "ok":
                continue
            fresh = await s.get(CryptoPaperDay, (run_id, row.day))
            if fresh is not None:
                fresh.fidelity_ok = row.day not in report.mismatches or row.day in explained
    return report

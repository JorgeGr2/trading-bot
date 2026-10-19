"""Informe de la simulación: estado, resultados de los dos libros y comparaciones.

Comparaciones (sobre las posiciones ya cerradas del libro de señales, que copia todas
las compras con el mismo tamaño):

* **Copia realista**: lo que de verdad habría pasado, con latencia, slippage, MEV,
  comisiones, renta y órdenes fallidas.
* **Copia teórica**: las mismas operaciones al precio exacto del trader y sin costes.
  La diferencia con la realista es lo que se come la ejecución.
* **No operar**: 0 $.
* **Mantener SOL**: los 100 $ de la cartera en SOL desde el primer registro.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.db.models import (
    BookCash,
    ControlState,
    LedgerEntry,
    PortfolioSnapshot,
    RiskEvent,
    ServiceHeartbeat,
    Signal,
    SimFill,
    SimOrder,
    SimPosition,
)
from copybot.sim.store import PORTFOLIO, SIGNALS

ZERO = Decimal(0)


def _short(address: str) -> str:
    return f"{address[:4]}…{address[-4:]}"


def _usd(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:+,.2f} $"


def reason_group(reason: str) -> str:
    """Agrupa motivos parecidos (sin cifras) para contarlos."""
    first = reason.split(";")[0].strip()
    return re.sub(r"\d[\d.,]*", "N", first)[:90] or "(sin motivo)"


@dataclass
class BookResult:
    book: str
    entries_filled: int = 0
    exits_filled: int = 0
    open_positions: int = 0
    closed_positions: int = 0
    wins: int = 0
    gross_win_usd: Decimal = ZERO
    gross_loss_usd: Decimal = ZERO
    network_fees_usd: Decimal = ZERO  # comisiones de red de compras, ventas y fallidas
    realized_usd: Decimal = ZERO
    unrealized_usd: Decimal = ZERO
    fees_usd: Decimal = ZERO
    failed: Counter[str] = field(default_factory=Counter)
    rejected: Counter[str] = field(default_factory=Counter)

    @property
    def total_usd(self) -> Decimal:
        return self.realized_usd + self.unrealized_usd

    @property
    def profit_factor(self) -> Decimal | None:
        return None if self.gross_loss_usd == 0 else self.gross_win_usd / -self.gross_loss_usd

    @property
    def sample_warning(self) -> str:
        if self.closed_positions < 30:
            return "MUESTRA INSUFICIENTE (menos de 30 operaciones cerradas): no sirve para concluir"
        if self.closed_positions < 100:
            return "muestra pequeña (menos de 100 operaciones cerradas): conclusiones débiles"
        return ""


@dataclass
class Comparison:
    closed: int
    realistic_usd: Decimal
    theoretical_usd: Decimal | None
    theoretical_closed: int
    hold_sol_usd: Decimal | None


@dataclass
class Report:
    state: str
    reason: str
    state_since: datetime | None
    monitor_age_s: float | None
    initial_usd: Decimal | None
    cash_usd: Decimal | None
    reserved_usd: Decimal | None
    positions_value_usd: Decimal
    peak_usd: Decimal | None
    day_start_usd: Decimal | None
    signals: Counter[str]
    books: dict[str, BookResult]
    comparison: Comparison
    open_positions: list[dict[str, Any]]
    traders: list[dict[str, Any]]
    risk_events: list[tuple[datetime, str, dict[str, Any]]]

    @property
    def equity_usd(self) -> Decimal | None:
        if self.cash_usd is None or self.reserved_usd is None:
            return None
        return self.cash_usd + self.reserved_usd + self.positions_value_usd


def _position_value(p: SimPosition) -> Decimal:
    if p.unsellable:
        return ZERO
    return p.last_value_usd if p.last_value_usd is not None else p.cost_usd + p.rent_usd


async def build_report(sessions: async_sessionmaker[AsyncSession], now: datetime) -> Report:
    async with sessions() as s:
        control = await s.get(ControlState, 1)
        beat = await s.scalar(
            select(ServiceHeartbeat.beat_at).where(ServiceHeartbeat.service == "monitor")
        )
        cash = await s.scalar(select(BookCash).where(BookCash.book == PORTFOLIO))
        positions = list(await s.scalars(select(SimPosition).order_by(SimPosition.id)))
        orders = list(await s.scalars(select(SimOrder).order_by(SimOrder.id)))
        signals = Counter(
            dict((await s.execute(select(Signal.side, func.count()).group_by(Signal.side))).all())
        )
        fees = dict(
            (
                await s.execute(
                    select(LedgerEntry.book, func.sum(LedgerEntry.delta_usd))
                    .where(LedgerEntry.account == "fees")
                    .group_by(LedgerEntry.book)
                )
            ).all()
        )
        fill_fees = dict(
            (
                await s.execute(
                    select(SimOrder.book, func.sum(SimFill.fees_usd))
                    .join(SimFill, SimFill.order_id == SimOrder.id)
                    .group_by(SimOrder.book)
                )
            ).all()
        )
        failed_fees = dict(
            (
                await s.execute(
                    select(LedgerEntry.book, func.sum(LedgerEntry.delta_usd))
                    .where(LedgerEntry.account == "fees", LedgerEntry.memo == "orden fallida")
                    .group_by(LedgerEntry.book)
                )
            ).all()
        )
        first = await s.scalar(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.book == PORTFOLIO)
            .order_by(PortfolioSnapshot.at)
            .limit(1)
        )
        last = await s.scalar(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.book == PORTFOLIO)
            .order_by(PortfolioSnapshot.at.desc())
            .limit(1)
        )
        events = [
            (e.at, e.kind, e.details)
            for e in await s.scalars(select(RiskEvent).order_by(RiskEvent.at.desc()).limit(10))
        ]

    books = {b: BookResult(b) for b in (PORTFOLIO, SIGNALS)}
    entry_cost: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for o in orders:
        r = books[o.book]
        if o.status == "rejected":
            r.rejected[reason_group(o.status_reason)] += 1
        elif o.status == "failed":
            r.failed[reason_group(o.status_reason)] += 1
        elif o.status == "filled":
            if o.side == "buy":
                r.entries_filled += 1
                if o.position_id is not None and o.requested_usd is not None:
                    entry_cost[o.position_id] += o.requested_usd
            else:
                r.exits_filled += 1
    for book, value in fees.items():
        books[book].fees_usd = Decimal(value or 0)
    for book, value in fill_fees.items():
        books[book].network_fees_usd = Decimal(value or 0)
    for book, value in failed_fees.items():
        books[book].network_fees_usd += Decimal(value or 0)

    open_rows = []
    closed_signals = []
    for p in positions:
        r = books[p.book]
        r.realized_usd += p.realized_pnl_usd
        if p.status == "open":
            r.open_positions += 1
            value = _position_value(p)
            r.unrealized_usd += value - p.cost_usd - p.rent_usd
            open_rows.append(
                {
                    "libro": p.book,
                    "token": p.mint,
                    "trader": p.wallet,
                    "coste": p.cost_usd + p.rent_usd,
                    "valor": value,
                    "horas": (now - p.opened_at).total_seconds() / 3600,
                    "invendible": p.unsellable,
                    "venta_pendiente": p.exit_pending,
                }
            )
        else:
            r.closed_positions += 1
            if p.realized_pnl_usd > 0:
                r.wins += 1
                r.gross_win_usd += p.realized_pnl_usd
            else:
                r.gross_loss_usd += p.realized_pnl_usd
            if p.book == SIGNALS:
                closed_signals.append(p)

    theoretical = [
        p.theoretical_proceeds_usd - entry_cost[p.id]
        for p in closed_signals
        if p.theoretical_amount is not None and entry_cost[p.id] > 0
    ]
    hold_sol = None
    if first is not None and last is not None and cash is not None:
        start = (first.details or {}).get("sol_usd")
        end = (last.details or {}).get("sol_usd")
        if start and end:
            hold_sol = cash.initial_usd * (Decimal(end) / Decimal(start) - 1)
    comparison = Comparison(
        closed=len(closed_signals),
        realistic_usd=sum((p.realized_pnl_usd for p in closed_signals), ZERO),
        theoretical_usd=sum(theoretical, ZERO) if theoretical else None,
        theoretical_closed=len(theoretical),
        hold_sol_usd=hold_sol,
    )

    per_trader: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "copias": 0,
            "cerradas": 0,
            "resultado": ZERO,
            "abiertas": 0,
            "real_cerradas": ZERO,
            "teorica_cerradas": None,
        }
    )
    for p in positions:
        if p.book != SIGNALS:
            continue
        t = per_trader[p.wallet]
        t["copias"] += 1
        if p.status == "open":
            t["abiertas"] += 1
            t["resultado"] += _position_value(p) - p.cost_usd - p.rent_usd
        else:
            t["cerradas"] += 1
            t["real_cerradas"] += p.realized_pnl_usd
            if p.theoretical_amount is not None and entry_cost[p.id] > 0:
                t["teorica_cerradas"] = (t["teorica_cerradas"] or ZERO) + (
                    p.theoretical_proceeds_usd - entry_cost[p.id]
                )
        t["resultado"] += p.realized_pnl_usd
    traders = sorted(
        ({"trader": w} | v for w, v in per_trader.items()), key=lambda x: x["resultado"]
    )

    return Report(
        state=control.state if control else "SIN INICIAR",
        reason=control.reason if control else "",
        state_since=control.updated_at if control else None,
        monitor_age_s=None if beat is None else (now - beat).total_seconds(),
        initial_usd=None if cash is None else cash.initial_usd,
        cash_usd=None if cash is None else cash.cash_usd,
        reserved_usd=None if cash is None else cash.reserved_usd,
        positions_value_usd=sum(
            (_position_value(p) for p in positions if p.book == PORTFOLIO and p.status == "open"),
            ZERO,
        ),
        peak_usd=None if cash is None else cash.peak_equity_usd,
        day_start_usd=None if cash is None else cash.day_start_equity_usd,
        signals=signals,
        books=books,
        comparison=comparison,
        open_positions=open_rows,
        traders=traders,
        risk_events=events,
    )


def render(report: Report, *, full: bool = True) -> str:
    out = []
    since = report.state_since.strftime("%Y-%m-%d %H:%M UTC") if report.state_since else "—"
    out.append(
        f"Estado: {report.state} desde {since}" + (f" — {report.reason}" if report.reason else "")
    )
    monitor = (
        "sin latidos"
        if report.monitor_age_s is None
        else f"último latido hace {report.monitor_age_s:.0f} s"
    )
    out.append(f"Monitor: {monitor}")
    equity = report.equity_usd
    if equity is not None and report.initial_usd is not None:
        pnl = equity - report.initial_usd
        out.append(
            f"Cartera: {equity:,.2f} $ ({_usd(pnl)}, {pnl / report.initial_usd:+.1%}) — "
            f"efectivo {report.cash_usd:,.2f} $, reservado {report.reserved_usd:,.2f} $, "
            f"posiciones {report.positions_value_usd:,.2f} $"
        )
        if report.peak_usd:
            out.append(
                f"  Máximo {report.peak_usd:,.2f} $ (caída {1 - equity / report.peak_usd:.1%}); "
                f"inicio del día {report.day_start_usd:,.2f} $"
            )
    out.append(
        f"Señales: {report.signals.get('buy', 0)} compras y {report.signals.get('sell', 0)} "
        "ventas de los traders seguidos"
    )
    for name, title in (
        (PORTFOLIO, "Cartera (100 $, con límites)"),
        (SIGNALS, "Libro de señales (10 $ por señal)"),
    ):
        r = report.books[name]
        win = f", {r.wins}/{r.closed_positions} con ganancia" if r.closed_positions else ""
        out.append(f"\n{title}")
        out.append(
            f"  Compras {r.entries_filled}, ventas {r.exits_filled}; abiertas {r.open_positions}, "
            f"cerradas {r.closed_positions}{win}"
        )
        out.append(
            f"  Resultado {_usd(r.total_usd)} (realizado {_usd(r.realized_usd)}, "
            f"sin realizar {_usd(r.unrealized_usd)})"
        )
        pf = "—" if r.profit_factor is None else f"{r.profit_factor:.2f}"
        per_trade = (
            f"{r.network_fees_usd / r.closed_positions:,.3f} $" if r.closed_positions else "—"
        )
        out.append(
            f"  Profit factor {pf}; comisiones de red {r.network_fees_usd:,.2f} $ "
            f"({per_trade} por operación cerrada; sin contar MEV ni impacto, que van en el "
            "precio)"
        )
        if r.sample_warning:
            out.append(f"  AVISO: {r.sample_warning}")
        if full:
            for label, counter in (("Rechazadas", r.rejected), ("Fallidas", r.failed)):
                if counter:
                    out.append(f"  {label} ({sum(counter.values())}):")
                    out += [f"    {n:>4} × {why}" for why, n in counter.most_common(8)]
    c = report.comparison
    out.append(f"\nComparación (posiciones cerradas del libro de señales: {c.closed})")
    out.append(f"  Copia realista:  {_usd(c.realistic_usd)}")
    out.append(
        f"  Copia teórica:   {_usd(c.theoretical_usd)}"
        + (f" (sobre {c.theoretical_closed} con precio del trader)" if c.theoretical_usd else "")
    )
    out.append("  No operar:       +0.00 $")
    out.append(f"  Mantener SOL:    {_usd(c.hold_sol_usd)} (100 $ desde el primer registro)")
    if full and report.open_positions:
        out.append("\nPosiciones abiertas")
        for p in report.open_positions:
            flags = (" INVENDIBLE" if p["invendible"] else "") + (
                " venta pendiente" if p["venta_pendiente"] else ""
            )
            out.append(
                f"  {p['libro']:<9} {_short(p['token'])} de {_short(p['trader'])}: coste "
                f"{p['coste']:,.2f} $, valor {p['valor']:,.2f} $, {p['horas']:.1f} h{flags}"
            )
    if full and report.traders:
        out.append("\nPor trader (libro de señales)")
        for t in report.traders:
            out.append(
                f"  {_short(t['trader'])}: {t['copias']} copias ({t['cerradas']} cerradas, "
                f"{t['abiertas']} abiertas), {_usd(t['resultado'])}"
                + (
                    f"; cerradas: real {_usd(t['real_cerradas'])} frente a teórica "
                    f"{_usd(t['teorica_cerradas'])}"
                    if t["teorica_cerradas"] is not None
                    else ""
                )
            )
    if full and report.risk_events:
        out.append("\nÚltimos eventos de riesgo")
        for at, kind, details in report.risk_events:
            out.append(f"  {at:%Y-%m-%d %H:%M} {kind}: {details}")
    return "\n".join(out)

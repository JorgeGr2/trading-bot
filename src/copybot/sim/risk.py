"""Límites de riesgo de la cartera (funciones puras).

Los límites salen de ``RiskLimits`` (topados en el código y que solo pueden
endurecerse): 10 $ por posición, 5 posiciones, 20 % de reserva, pérdida diaria del 10 %
y caída máxima del 25 %.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from copybot.config import RiskLimits


@dataclass(frozen=True)
class BookState:
    initial_usd: Decimal
    cash_usd: Decimal
    reserved_usd: Decimal
    positions_value_usd: Decimal
    open_positions: int
    open_mints: frozenset[str]
    peak_equity_usd: Decimal
    day_start_equity_usd: Decimal

    @property
    def equity_usd(self) -> Decimal:
        return self.cash_usd + self.reserved_usd + self.positions_value_usd


def entry_size(limits: RiskLimits) -> Decimal:
    return limits.max_position_usd


def entry_rejections(state: BookState, mint: str, size: Decimal, limits: RiskLimits) -> list[str]:
    """Motivos para no abrir una posición (vacío: se puede abrir)."""
    reasons = []
    if state.open_positions >= limits.max_open_positions:
        maximum = limits.max_open_positions
        reasons.append(f"ya hay {state.open_positions} posiciones abiertas (máximo {maximum})")
    if mint in state.open_mints:
        reasons.append("ya hay una posición abierta en este token")
    reserve = state.initial_usd * limits.min_cash_reserve_pct / 100
    if state.cash_usd - size < reserve:
        reasons.append(
            f"no queda efectivo fuera de la reserva: {state.cash_usd:.2f} $ disponibles, "
            f"reserva {reserve:.2f} $, orden {size:.2f} $"
        )
    return reasons


def halt_reasons(state: BookState, limits: RiskLimits) -> list[tuple[str, str]]:
    """Límites superados que obligan a detener las entradas (``HALTED_RISK``)."""
    out = []
    equity = state.equity_usd
    if state.peak_equity_usd > 0:
        drawdown = 1 - equity / state.peak_equity_usd
        if drawdown * 100 >= limits.max_drawdown_pct:
            out.append(
                (
                    "max_drawdown",
                    f"caída del {drawdown:.1%} desde el máximo ({state.peak_equity_usd:.2f} $ → "
                    f"{equity:.2f} $; límite {limits.max_drawdown_pct} %)",
                )
            )
    if state.day_start_equity_usd > 0:
        daily = 1 - equity / state.day_start_equity_usd
        if daily * 100 >= limits.max_daily_loss_pct:
            out.append(
                (
                    "daily_loss",
                    f"pérdida del día del {daily:.1%} ({state.day_start_equity_usd:.2f} $ → "
                    f"{equity:.2f} $; límite {limits.max_daily_loss_pct} %)",
                )
            )
    return out

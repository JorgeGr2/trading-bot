"""La regla v1 (``cripto-v1``) y su decisión diaria.

``decide()`` es una función pura: la usan igual el backtest y la cartera de prueba en vivo.
Recibe lo que se sabe al cierre del día ``t`` (universo, puestos, régimen de mercado y
precios de cierre) y el estado del libro, y devuelve qué vender y qué comprar. La
ejecución (al día siguiente) la hace quien la llama.

Los parámetros están congelados en ``STRATEGY_V1`` y no se pueden cambiar por variables
de entorno; ``params_hash()`` los identifica en el preregistro y en cada informe.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from decimal import Decimal

from copybot.config import RiskLimits
from copybot.crypto.panel import Panel
from copybot.crypto.signals import CoinSignal, SignalParams, ranked_day, sma
from copybot.crypto.universe import Universe, UniverseParams, universe
from copybot.sim.risk import BookState, entry_rejections, halt_reasons

STRATEGY_VERSION = "cripto-v1"
MIN_ORDER_USD = 5.0


@dataclass(frozen=True)
class StrategyParams:
    universe: UniverseParams = field(default_factory=UniverseParams)
    signals: SignalParams = field(default_factory=SignalParams)
    market_symbol: str = "BTCUSDT"
    market_sma: int = 100
    use_market_filter: bool = True
    n_positions: int = 5
    entry_rank: int = 5
    exit_rank: int = 12  # se vende a partir del puesto 13
    entry_trend: float = 2 / 3
    exit_trend: float = 1 / 3
    stop_loss: float = 0.25  # cierre ≤ 0,75 × precio de entrada
    reserve_pct: float = 10.0  # efectivo que no se invierte (porcentaje del capital)
    signals_book_top: int = 5
    signals_book_usd: float = 10.0
    signals_book_days: int = 7

    def params_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()


STRATEGY_V1 = StrategyParams()
# Variantes registradas (solo para comparar; ninguna sustituye a la principal).
VARIANTS = {
    "V1": replace(STRATEGY_V1, n_positions=1, entry_rank=1, exit_rank=1),
    "V2": replace(STRATEGY_V1, use_market_filter=False),
}
VARIANT_NAMES = {
    "V1": "La nº 1 cada día (rota a la primera del ranking)",
    "V2": "Sin filtro de mercado (nunca pasa a USDT)",
}


@dataclass(frozen=True)
class Regime:
    close: float
    sma: float
    favorable: bool


@dataclass(frozen=True)
class DayView:
    """Todo lo que se sabe al cierre del día ``day``."""

    day: date
    universe: Universe
    ranked: list[CoinSignal]  # monedas del universo con puesto, de la primera a la última
    regime: Regime | None  # None: sin datos de BTC ese día
    close: dict[str, float]  # cierre de cada par con vela ese día
    low: dict[str, float]

    @property
    def by_symbol(self) -> dict[str, CoinSignal]:
        return {s.symbol: s for s in self.ranked}


def regime(panel: Panel, day: date, p: StrategyParams) -> Regime | None:
    series = panel.series.get(p.market_symbol)
    i = None if series is None else series.at(day)
    if series is None or i is None:
        return None
    mean = sma(series.close, i, p.market_sma)
    if mean is None:
        return None
    close = series.close[i]
    return Regime(close, mean, close > mean)


def day_view(panel: Panel, day: date, p: StrategyParams = STRATEGY_V1) -> DayView:
    u = universe(panel, day, p.universe)
    ranked = ranked_day(panel, day, [m.symbol for m in u.members], p.signals)
    close, low = {}, {}
    for series in panel.series.values():
        i = series.at(day)
        if i is not None:
            close[series.symbol] = series.close[i]
            low[series.symbol] = series.low[i]
    return DayView(day, u, ranked, regime(panel, day, p), close, low)


@dataclass(frozen=True)
class Holding:
    symbol: str
    qty: float
    entry_price: float
    entry_day: date
    last_close: float  # último cierre conocido (para valorarla si hoy no hay vela)


@dataclass(frozen=True)
class BookView:
    cash: float
    holdings: dict[str, Holding]
    prev_equity: float  # E(t−1)
    peak_equity: float  # máximo de E hasta t−1
    initial: float
    halted_risk: bool = False  # parada por caída del 50 % (solo se reanuda a mano)
    paused: bool = False
    blocked: frozenset[str] = frozenset()  # bloqueadas en vivo (diferencial, estado…)


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str  # buy | sell
    purpose: str  # entrada, regimen, fuera_universo, stop, tendencia, puesto
    reason: str
    usd: float = 0.0  # importe de las compras
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    day: date
    equity: float  # E(t), al cierre
    worst_equity: float  # W(t), con los mínimos del día
    orders: list[Order]
    halts: list[tuple[str, str]]
    market_favorable: bool | None
    size_usd: float  # tamaño de cada compra (0 si no se compra)

    @property
    def sells(self) -> list[Order]:
        return [o for o in self.orders if o.side == "sell"]

    @property
    def buys(self) -> list[Order]:
        return [o for o in self.orders if o.side == "buy"]


DELISTED_HAIRCUT = 0.90  # una posición sin vela se valora al último cierre × 0,90


def valuation(view: DayView, book: BookView) -> tuple[float, float]:
    """(E, W): patrimonio al cierre y con el mínimo del día."""
    equity = worst = book.cash
    for h in book.holdings.values():
        close = view.close.get(h.symbol)
        if close is None:
            value = h.qty * h.last_close * DELISTED_HAIRCUT
            equity += value
            worst += value
        else:
            equity += h.qty * close
            worst += h.qty * view.low.get(h.symbol, close)
    return equity, worst


def _d(x: float) -> Decimal:
    return Decimal(str(round(x, 10)))


def size_usd(equity: float, p: StrategyParams, limits: RiskLimits, mode: str) -> float:
    """Tamaño de cada compra. Modo «dólares» (la cartera de 100 $): el mínimo entre el
    tope por posición y (E − reserva)/N, redondeado al céntimo hacia abajo. Modo
    «porcentaje» (la curva del método): (E − 10 % de E)/N, sin tope en dólares."""
    n = min(p.n_positions, limits.max_open_positions)
    if mode == "porcentaje":
        return equity * (1 - p.reserve_pct / 100) / n
    reserve = float(limits.virtual_capital_usd) * float(limits.min_cash_reserve_pct) / 100
    raw = min(float(limits.max_position_usd), (equity - reserve) / n)
    return math.floor(max(raw, 0.0) * 100) / 100


def decide(
    view: DayView,
    book: BookView,
    p: StrategyParams,
    limits: RiskLimits,
    *,
    mode: str = "dolares",
    apply_drawdown_halt: bool = True,
) -> Decision:
    equity, worst = valuation(view, book)
    positions_value = worst - book.cash
    state = BookState(
        initial_usd=_d(book.initial if mode == "dolares" else equity),
        cash_usd=_d(book.cash),
        reserved_usd=Decimal(0),
        positions_value_usd=_d(positions_value),
        open_positions=len(book.holdings),
        open_mints=frozenset(book.holdings),
        peak_equity_usd=_d(book.peak_equity),
        day_start_equity_usd=_d(book.prev_equity),
    )
    halts = halt_reasons(state, limits)
    if not apply_drawdown_halt:
        halts = [h for h in halts if h[0] != "max_drawdown"]
    reg = view.regime
    favorable = None if reg is None else (reg.favorable or not p.use_market_filter)
    ranked = view.by_symbol
    orders: list[Order] = []
    sold: set[str] = set()

    # --- Ventas: el primer motivo que se cumpla ------------------------------------------
    for h in sorted(book.holdings.values(), key=lambda h: h.symbol):
        close = view.close.get(h.symbol)
        if close is None:
            continue  # sin vela hoy: no se puede vender (lo resuelve la regla de retiradas)
        sig = ranked.get(h.symbol)
        if favorable is False:
            purpose = "regimen"
            reason = f"mercado bajista: BTC cerró por debajo de su media de {p.market_sma} días"
        elif sig is None:
            purpose = "fuera_universo"
            why = view.universe.excluded.get(h.symbol, "sin datos suficientes para puntuarla")
            reason = f"fuera del universo: {why}"
        elif close <= (1 - p.stop_loss) * h.entry_price:
            purpose = "stop"
            reason = f"stop de emergencia: −{p.stop_loss * 100:.0f} % desde la compra"
        elif sig.trend_share <= p.exit_trend + 1e-9:
            purpose = "tendencia"
            reason = "perdió la tendencia (por debajo de 2 de sus 3 medias)"
        elif sig.rank > p.exit_rank:
            purpose = "puesto"
            reason = f"bajó al puesto {sig.rank} (se vende a partir del {p.exit_rank + 1})"
        else:
            continue
        orders.append(Order(h.symbol, "sell", purpose, reason, details=_details(sig, reg)))
        sold.add(h.symbol)

    # --- Compras -----------------------------------------------------------------------
    size = 0.0
    can_buy = favorable is True and not halts and not book.halted_risk and not book.paused
    if can_buy:
        size = size_usd(equity, p, limits, mode)
        n = min(p.n_positions, limits.max_open_positions)
        holes = n - (len(book.holdings) - len(sold))
        cash = book.cash
        open_now = len(book.holdings) - len(sold)
        for sig in view.ranked:
            if holes <= 0 or sig.rank > p.entry_rank:
                break
            if sig.symbol in book.holdings or sig.symbol in sold or sig.symbol in book.blocked:
                continue
            if sig.trend_share + 1e-9 < p.entry_trend or size < MIN_ORDER_USD:
                continue
            check = replace(
                state,
                cash_usd=_d(cash),
                open_positions=open_now,
                open_mints=frozenset(set(book.holdings) - sold),
            )
            if mode == "dolares" and entry_rejections(check, sig.symbol, _d(size), limits):
                continue
            if size > cash + 1e-9:
                continue
            orders.append(
                Order(
                    sig.symbol,
                    "buy",
                    "entrada",
                    f"puesto {sig.rank} con {sig.score:.0f} puntos y tendencia "
                    f"{round(sig.trend_share * 3)} de 3",
                    usd=size,
                    details=_details(sig, reg),
                )
            )
            cash -= size
            open_now += 1
            holes -= 1
    return Decision(view.day, equity, worst, orders, halts, favorable, size)


def _details(sig: CoinSignal | None, reg: Regime | None) -> dict:
    out: dict = {}
    if sig is not None:
        out.update(
            puesto=sig.rank,
            puntos=round(sig.score, 4),
            a={str(k): round(v, 6) for k, v in sig.adjusted.items()},
            m={str(k): round(v, 6) for k, v in sig.momentum.items()},
            sigma=round(sig.vol, 6),
            tendencia=round(sig.trend_share, 4),
        )
    if reg is not None:
        out.update(btc=reg.close, btc_sma=round(reg.sma, 6), favorable=reg.favorable)
    return out

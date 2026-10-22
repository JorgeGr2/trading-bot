"""Motor del backtest de la regla v1: el mismo ``decide()`` que en vivo, día a día.

Cada día ``t`` (con la vela de ``t`` cerrada) se valora la cartera, se decide y las
órdenes se ejecutan en la **apertura** de ``t+1`` (o de ``t+2`` en la prueba de estrés
de un día de retraso), primero las ventas y después las compras, con la comisión y el
coste por tramo de liquidez. Nunca se usa una vela posterior a ``t`` para decidir.

Curvas: A (método: tamaño en porcentaje del patrimonio, parada del 50 % medida pero no
aplicada) y B (la cartera de 100 $ con los topes vigentes y todas las paradas).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from copybot.config import RiskLimits
from copybot.crypto import costs
from copybot.crypto.panel import Panel
from copybot.crypto.strategy import (
    DELISTED_HAIRCUT,
    SELL_COST_ESTIMATE,
    BookView,
    DayView,
    Decision,
    Holding,
    Order,
    StrategyParams,
    day_view,
    decide,
)

DELIST_DAYS = 7  # sin vela durante 7 días seguidos: se da por retirada


def build_views(
    panel: Panel,
    days: Iterable[date],
    p: StrategyParams,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> dict[date, DayView]:
    """Vista de cada día (universo, puestos y régimen). Es lo caro: se calcula una vez y
    la usan la principal, las variantes, las pruebas de estrés y el nulo."""
    days = list(days)
    gaps = panel.global_gaps()
    out = {}
    for k, day in enumerate(days):
        if day not in gaps:
            out[day] = day_view(panel, day, p)
        if progress is not None:
            progress(k + 1, len(days))
    return out


@dataclass(frozen=True)
class RunConfig:
    params: StrategyParams
    limits: RiskLimits
    mode: str = "porcentaje"  # porcentaje (curva A) | dolares (curva B)
    cost_mult: float = 1.0
    exec_delay: int = 1  # se ejecuta en la apertura de t + exec_delay
    apply_drawdown_halt: bool = False  # B: True (y no se reanuda)
    delist_loss: float = 1 - DELISTED_HAIRCUT  # pérdida al cerrar una retirada (10 %)
    initial: float = 100.0


@dataclass
class Trade:
    symbol: str
    entry_day: date  # día de la ejecución
    exit_day: date | None
    qty: float
    entry_price: float
    cost: float  # dólares gastados
    proceeds: float = 0.0
    exit_purpose: str | None = None
    decided_day: date | None = None

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost

    @property
    def ret(self) -> float:
        return self.proceeds / self.cost - 1 if self.cost else 0.0


@dataclass
class RunResult:
    config: RunConfig
    curve: list[tuple[date, float]]  # E(t) al cierre
    worst: list[tuple[date, float]]  # W(t)
    trades: list[Trade]
    decisions: list[Decision] = field(default_factory=list)
    halted_on: date | None = None  # parada del 50 % (solo en la curva B)
    max_dd_reached_50: bool = False
    costs_paid: float = 0.0
    invested_days: int = 0

    @property
    def final(self) -> float:
        return self.curve[-1][1] if self.curve else self.config.initial


def _median_qv(view: DayView, symbol: str) -> float:
    for m in view.universe.members:
        if m.symbol == symbol:
            return m.median_quote_volume
    return 5_000_000.0  # fuera del universo: el tramo más caro


class _Book:
    def __init__(self, cash: float) -> None:
        self.cash = cash
        self.holdings: dict[str, Holding] = {}
        self.open_trades: dict[str, Trade] = {}
        self.last_bar: dict[str, date] = {}
        self.prev = self.peak = cash
        self.halted_risk = False


def simulate(
    panel: Panel,
    views: dict[date, DayView],
    days: list[date],
    cfg: RunConfig,
    *,
    keep_decisions: bool = False,
    chooser: Callable[[DayView, Decision, _Book], Decision] | None = None,
) -> RunResult:
    """Simula la regla entre ``days[0]`` y ``days[-1]``. ``chooser`` permite sustituir
    las compras (el nulo aleatorio) sin tocar el resto."""
    book = _Book(cfg.initial)
    pending: dict[date, Decision] = {}
    result = RunResult(cfg, [], [], [])
    exec_views: dict[date, DayView] = {}
    for day in days:
        # --- 1. Ejecución en la apertura de hoy de lo decidido antes --------------------
        decision = pending.pop(day, None)
        if decision is not None:
            _execute(panel, book, decision, day, cfg, result, exec_views.pop(day, None))
        # --- 2. Retiradas: 7 días seguidos sin vela -------------------------------------
        for symbol in list(book.holdings):
            series = panel.series[symbol]
            i = series.at(day)
            if i is not None and series.traded(i):
                book.last_bar[symbol] = day
                h = book.holdings[symbol]
                book.holdings[symbol] = replace(h, last_close=series.close[i])
            elif (day - book.last_bar.get(symbol, day)).days >= DELIST_DAYS:
                _close_delisted(book, symbol, day, cfg, result)
        view = views.get(day)
        if view is None:  # hueco global: sin decisión ese día
            if result.curve:
                result.curve.append((day, result.curve[-1][1]))
                result.worst.append((day, result.worst[-1][1]))
            continue
        # --- 3. Decisión con la vela de hoy cerrada ------------------------------------
        bv = BookView(
            cash=book.cash,
            holdings=dict(book.holdings),
            prev_equity=book.prev,
            peak_equity=book.peak,
            initial=cfg.initial,
            halted_risk=book.halted_risk,
        )
        d = decide(
            view,
            bv,
            cfg.params,
            cfg.limits,
            mode=cfg.mode,
            apply_drawdown_halt=cfg.apply_drawdown_halt,
        )
        if chooser is not None:
            d = chooser(view, d, book)
        if any(code == "max_drawdown" for code, _ in d.halts) and cfg.apply_drawdown_halt:
            if not book.halted_risk:
                result.halted_on = day
            book.halted_risk = True
        if book.peak > 0 and 1 - d.worst_equity / book.peak >= 0.5:
            result.max_dd_reached_50 = True
        result.curve.append((day, d.equity))
        result.worst.append((day, d.worst_equity))
        if book.holdings:
            result.invested_days += 1
        if keep_decisions:
            result.decisions.append(d)
        book.prev = d.equity
        book.peak = max(book.peak, d.equity)
        if d.orders:
            when = day + timedelta(days=cfg.exec_delay)
            pending[when] = d
            exec_views[when] = view
    # Lo que quede abierto se valora al final (no se vende: la curva ya lo refleja).
    for symbol, trade in book.open_trades.items():
        h = book.holdings[symbol]
        trade.proceeds = h.qty * h.last_close
    result.trades.extend(book.open_trades.values())
    return result


def _execute(
    panel: Panel,
    book: _Book,
    decision: Decision,
    day: date,
    cfg: RunConfig,
    result: RunResult,
    view: DayView | None,
) -> None:
    for order in decision.sells:
        h = book.holdings.get(order.symbol)
        series = panel.series[order.symbol]
        i = series.at(day)
        if h is None or i is None or not series.traded(i):
            continue  # sin vela hoy: no se puede vender; se reintentará
        qv = _median_qv(view, order.symbol) if view else 5_000_000.0
        gross = h.qty * series.open[i]
        net = costs.sell_usd(h.qty, series.open[i], qv, cfg.cost_mult)
        result.costs_paid += gross - net
        book.cash += net
        trade = book.open_trades.pop(order.symbol)
        trade.exit_day, trade.proceeds, trade.exit_purpose = day, net, order.purpose
        result.trades.append(trade)
        del book.holdings[order.symbol]
    for order in decision.buys:
        series = panel.series[order.symbol]
        i = series.at(day)
        if i is None or not series.traded(i) or order.symbol in book.holdings:
            continue
        usd = min(order.usd, book.cash)
        if usd < 5.0:
            continue
        qv = _median_qv(view, order.symbol) if view else 5_000_000.0
        qty = costs.buy_qty(usd, series.open[i], qv, cfg.cost_mult)
        result.costs_paid += usd - qty * series.open[i]
        book.cash -= usd
        book.holdings[order.symbol] = Holding(
            order.symbol, qty, series.open[i], day, series.open[i]
        )
        book.last_bar[order.symbol] = day
        book.open_trades[order.symbol] = Trade(
            order.symbol, day, None, qty, series.open[i], usd, decided_day=decision.day
        )


def _close_delisted(book: _Book, symbol: str, day: date, cfg: RunConfig, result: RunResult):
    h = book.holdings.pop(symbol)
    value = h.qty * h.last_close * (1 - cfg.delist_loss)
    book.cash += value
    trade = book.open_trades.pop(symbol)
    trade.exit_day, trade.proceeds, trade.exit_purpose = day, value, "retirada"
    result.trades.append(trade)


# --- Libro de señales -----------------------------------------------------------------


@dataclass(frozen=True)
class SignalTrade:
    day: date  # día de la decisión
    symbol: str
    rank: int
    favorable: bool | None
    ret: float  # neta de costes
    btc_ret: float

    @property
    def excess(self) -> float:
        return math.log1p(self.ret) - math.log1p(self.btc_ret)


def _open_on(panel: Panel, symbol: str, day: date) -> float | None:
    series = panel.series.get(symbol)
    i = None if series is None else series.at(day)
    if i is None or not series.traded(i):
        return None
    return series.open[i]


def _exit_price(panel: Panel, symbol: str, day: date, delist_loss: float) -> float | None:
    """Apertura de ``day``; si ya no cotiza, el último cierre conocido con la pérdida de
    retirada (y ``None`` si aún no se sabe porque los datos no llegan)."""
    price = _open_on(panel, symbol, day)
    if price is not None:
        return price
    series = panel.series[symbol]
    j = series.index(day)
    if j is None:
        return None
    if series.last_day < day and (day - series.last_day).days >= 1:
        return series.close[j] * (1 - delist_loss)
    return None


def signals_book(
    panel: Panel,
    views: dict[date, DayView],
    days: list[date],
    p: StrategyParams,
    *,
    cost_mult: float = 1.0,
    delist_loss: float = 1 - DELISTED_HAIRCUT,
    market: str = "BTCUSDT",
) -> list[SignalTrade]:
    """Cada día, las 5 primeras con tendencia (sin filtro de mercado): 10 $ en cada una de
    la apertura de t+1 a la de t+8, con costes, frente a BTC en la misma ventana."""
    out = []
    horizon = p.signals_book_days
    for day in days:
        view = views.get(day)
        if view is None:
            continue
        picks = [s for s in view.ranked if s.trend_share + 1e-9 >= p.entry_trend]
        picks = picks[: p.signals_book_top]
        entry_day, exit_day = day + timedelta(days=1), day + timedelta(days=1 + horizon)
        b0 = _open_on(panel, market, entry_day)
        b1 = _open_on(panel, market, exit_day)
        if b0 is None or b1 is None:
            continue
        qv_btc = _median_qv(view, market)
        btc_ret = costs.sell_usd(costs.buy_qty(1.0, b0, qv_btc, cost_mult), b1, qv_btc, cost_mult)
        for sig in picks:
            o0 = _open_on(panel, sig.symbol, entry_day)
            o1 = _exit_price(panel, sig.symbol, exit_day, delist_loss)
            if o0 is None or o1 is None:
                continue
            qv = _median_qv(view, sig.symbol)
            usd = p.signals_book_usd
            back = costs.sell_usd(costs.buy_qty(usd, o0, qv, cost_mult), o1, qv, cost_mult)
            fav = None if view.regime is None else view.regime.favorable
            out.append(SignalTrade(day, sig.symbol, sig.rank, fav, back / usd - 1, btc_ret - 1))
    return out


# --- Nulo aleatorio -------------------------------------------------------------------


def random_chooser(seed: int, durations: list[int], n_positions: int) -> Callable:
    """Compras al azar entre las monedas del universo con tendencia que no están en
    cartera, y salida por puesto sustituida por una duración sacada al azar de las de la
    estrategia principal (misma rotación). El resto de reglas no cambia."""
    rng = random.Random(seed)  # noqa: S311 (estadística, no criptografía)
    hold_until: dict[str, date] = {}

    def choose(view: DayView, d: Decision, book: _Book) -> Decision:
        orders = [o for o in d.sells if o.purpose != "puesto"]
        sold = {o.symbol for o in orders}
        for symbol, until in sorted(hold_until.items()):
            due = symbol in book.holdings and symbol not in sold and view.day >= until
            if due and symbol in view.close:
                orders.append(Order(symbol, "sell", "puesto", "duración al azar"))
                sold.add(symbol)
        size = d.size_usd
        if size >= 5.0:
            cash = book.cash + sum(
                book.holdings[s].qty * view.close[s] * (1 - SELL_COST_ESTIMATE) for s in sold
            )
            room = int(max(0.0, cash - 0.1 * d.equity) // size)
            slots = min(n_positions - (len(book.holdings) - len(sold)), room)
            pool = sorted(
                s.symbol
                for s in view.ranked
                if s.trend_share + 1e-9 >= 2 / 3
                and s.symbol not in book.holdings
                and s.symbol not in sold
            )
            for symbol in rng.sample(pool, max(0, min(slots, len(pool)))):
                orders.append(Order(symbol, "buy", "entrada", "al azar", usd=size))
                hold_until[symbol] = view.day + timedelta(days=rng.choice(durations or [7]))
        return replace(d, orders=orders)

    return choose

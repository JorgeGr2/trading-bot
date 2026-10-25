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
    MIN_ORDER_USD,
    BookView,
    DayView,
    Decision,
    Holding,
    Order,
    StrategyParams,
    buy_room,
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
    chooser: Callable[[DayView, Decision, BookView], Decision] | None = None,
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
        # Con un día de retraso, lo decidido ayer aún no se ha ejecutado, pero ya cuenta:
        # sus compras ocupan hueco y efectivo, y sus ventas ya no son cartera.
        waiting = [o for when in sorted(pending) for o in pending[when].orders]
        bv = BookView(
            cash=book.cash,
            holdings=dict(book.holdings),
            prev_equity=book.prev,
            peak_equity=book.peak,
            initial=cfg.initial,
            halted_risk=book.halted_risk,
            pending_buys={o.symbol: o.usd for o in waiting if o.side == "buy"},
            pending_sells=frozenset(o.symbol for o in waiting if o.side == "sell"),
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
            d = chooser(view, d, bv)
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
    n = min(cfg.params.n_positions, cfg.limits.max_open_positions)
    for order in decision.buys:
        series = panel.series[order.symbol]
        i = series.at(day)
        if i is None or not series.traded(i) or order.symbol in book.holdings:
            continue
        # Como en vivo (``paper._buy``): si una venta de hoy no se pudo hacer, la compra no
        # cabe (N posiciones o efectivo insuficiente) y se rechaza; nunca se compra a medias.
        usd = order.usd
        if len(book.holdings) >= n or book.cash + 1e-9 < usd or usd < MIN_ORDER_USD:
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
    btc_ret: float  # BTC en la misma ventana (de t+1 al día de salida)
    exit_day: date | None = None  # t+8, o más tarde si ese día no se negoció

    @property
    def excess(self) -> float:
        return math.log1p(self.ret) - math.log1p(self.btc_ret)


def _open_on(panel: Panel, symbol: str, day: date) -> float | None:
    series = panel.series.get(symbol)
    i = None if series is None else series.at(day)
    if i is None or not series.traded(i):
        return None
    return series.open[i]


def data_end(panel: Panel) -> date | None:
    """Último día con datos: la última vela de BTC (la referencia). Después no se sabe
    nada, ni siquiera si una moneda ha dejado de cotizar."""
    ref = panel.series.get(panel.reference)
    return ref.days[-1] if ref is not None and ref.days else None


def exit_at(
    panel: Panel, symbol: str, day: date, delist_loss: float, end: date | None
) -> tuple[float, date] | None:
    """Precio y día de salida de una posición que toca vender en la apertura de ``day``
    (apartados 6.6 y 7), igual que en el motor:

    * la primera apertura **con negociación** desde ``day``;
    * si antes se cumplen 7 días seguidos sin negociar, se da por retirada ese día, al
      último cierre negociado con la pérdida de retirada;
    * ``None`` solo si los datos (``end``) terminan antes de saberlo.

    Nunca mira cuándo termina la serie de la moneda: eso sería usar el futuro."""
    series = panel.series.get(symbol)
    if series is None or end is None:
        return None
    last = series.index(day - timedelta(days=1))
    while last is not None and last >= 0 and not series.traded(last):
        last -= 1
    if last is None or last < 0:
        return None  # nunca se negoció antes: no hay posición que cerrar
    probe = day
    while probe <= end:
        i = series.at(probe)
        if i is not None and series.traded(i):
            return series.open[i], probe
        if (probe - series.days[last]).days >= DELIST_DAYS:
            return series.close[last] * (1 - delist_loss), probe
        probe += timedelta(days=1)
    return None


def window_return(
    panel: Panel,
    symbol: str,
    entry_day: date,
    exit_day: date,
    median_qv: float | None,
    *,
    end: date | None,
    cost_mult: float = 1.0,
    delist_loss: float = 1 - DELISTED_HAIRCUT,
) -> tuple[float, date] | None:
    """Rentabilidad neta de costes de comprar en la apertura de ``entry_day`` y vender con
    la regla de :func:`exit_at` desde ``exit_day``; y el día en que se vende."""
    o0 = _open_on(panel, symbol, entry_day)
    out = None if o0 is None else exit_at(panel, symbol, exit_day, delist_loss, end)
    if out is None:
        return None
    o1, sold_on = out
    qty = costs.buy_qty(1.0, o0, median_qv, cost_mult)
    return costs.sell_usd(qty, o1, median_qv, cost_mult) - 1, sold_on


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
    la apertura de t+1 a la de t+8 (o la primera negociada después, o su retirada), con
    costes, frente a BTC en la misma ventana."""
    out = []
    horizon = p.signals_book_days
    kw = {"end": data_end(panel), "cost_mult": cost_mult, "delist_loss": delist_loss}
    for day in days:
        view = views.get(day)
        if view is None:
            continue
        picks = [s for s in view.ranked if s.trend_share + 1e-9 >= p.entry_trend]
        picks = picks[: p.signals_book_top]
        entry_day, exit_day = day + timedelta(days=1), day + timedelta(days=1 + horizon)
        if _open_on(panel, market, entry_day) is None:
            continue
        qv_btc = _median_qv(view, market)
        fav = None if view.regime is None else view.regime.favorable
        btc: dict[date, tuple[float, date] | None] = {}
        for sig in picks:
            leg = window_return(
                panel, sig.symbol, entry_day, exit_day, _median_qv(view, sig.symbol), **kw
            )
            if leg is None:
                continue
            ret, sold_on = leg
            if sold_on not in btc:
                btc[sold_on] = window_return(panel, market, entry_day, sold_on, qv_btc, **kw)
            ref = btc[sold_on]
            if ref is None:
                continue
            out.append(SignalTrade(day, sig.symbol, sig.rank, fav, ret, ref[0], sold_on))
    return out


# --- Nulo aleatorio -------------------------------------------------------------------


def random_chooser(
    seed: int,
    durations: list[int],
    params: StrategyParams,
    limits: RiskLimits,
    mode: str = "porcentaje",
) -> Callable:
    """Compras al azar entre las monedas del universo con tendencia que no están en
    cartera, y salida por puesto sustituida por una duración sacada al azar de las de la
    estrategia principal (misma rotación). El resto de reglas no cambia: los huecos y la
    reserva salen de la misma ``buy_room()`` que usa ``decide()``."""
    rng = random.Random(seed)  # noqa: S311 (estadística, no criptografía)
    hold_until: dict[str, date] = {}

    def choose(view: DayView, d: Decision, book: BookView) -> Decision:
        orders = [o for o in d.sells if o.purpose != "puesto"]
        sold = {o.symbol for o in orders}
        for symbol, until in sorted(hold_until.items()):
            due = symbol in book.holdings and symbol not in sold and view.day >= until
            if due and symbol in view.close and symbol not in book.pending_sells:
                orders.append(Order(symbol, "sell", "puesto", "duración al azar"))
                sold.add(symbol)
        size = d.size_usd
        if size >= MIN_ORDER_USD:
            room = buy_room(view, book, sold, d.equity, params, limits, mode)
            taken = sold | set(book.holdings) | set(book.pending_buys)
            pool = sorted(
                s.symbol
                for s in view.ranked
                if s.trend_share + 1e-9 >= params.entry_trend and s.symbol not in taken
            )
            for symbol in rng.sample(pool, min(room.fits(size), len(pool))):
                orders.append(Order(symbol, "buy", "entrada", "al azar", usd=size))
                hold_until[symbol] = view.day + timedelta(days=rng.choice(durations or [7]))
        return replace(d, orders=orders)

    return choose

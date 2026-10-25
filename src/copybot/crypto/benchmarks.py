"""Referencias del backtest: mismo capital, misma hora de ejecución y mismos costes.

* USDT (0 %).
* BTC comprar y mantener (100 %): la referencia acordada.
* BTC con el mismo filtro: 90 % en BTC cuando BTC cierra por encima de su media de 100
  días y USDT si no. Separa el valor de *salir a tiempo* del de *elegir moneda*.
* 90 % BTC + 10 % USDT: la misma exposición máxima que la cartera.
* Cesta de las 10 más líquidas del universo, con el mismo peso, reequilibrada cada 28
  días, con costes y retiradas.

Todas devuelven una curva diaria de patrimonio al cierre, en las mismas fechas que la
estrategia. Las órdenes decididas al cierre de ``t`` se ejecutan en la apertura de ``t+1``.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date, timedelta

from copybot.crypto import costs
from copybot.crypto.panel import Panel
from copybot.crypto.strategy import DELISTED_HAIRCUT, DayView

Curve = list[tuple[date, float]]
BTC = "BTCUSDT"
BTC_QV = 1e9  # BTC siempre está en el tramo más barato


@dataclass(frozen=True)
class Benchmark:
    key: str
    name: str
    curve: Curve

    @property
    def final(self) -> float:
        return self.curve[-1][1]


def usdt(days: list[date], initial: float = 100.0) -> Benchmark:
    return Benchmark("usdt", "Dejarlo en USDT", [(d, initial) for d in days])


def _close(panel: Panel, symbol: str, day: date) -> float | None:
    s = panel.series.get(symbol)
    i = None if s is None else s.index(day)
    return None if i is None else s.close[i]


def _open(panel: Panel, symbol: str, day: date) -> float | None:
    s = panel.series.get(symbol)
    i = None if s is None else s.at(day)
    return None if i is None or not s.traded(i) else s.open[i]


def btc_hold(
    panel: Panel, days: list[date], *, share: float = 1.0, initial: float = 100.0, mult=1.0
) -> Benchmark:
    """Compra ``share`` del capital en BTC en la apertura siguiente al primer día y lo
    mantiene (el resto en USDT)."""
    cash, qty = initial, 0.0
    curve = []
    bought = False
    for k, day in enumerate(days):
        if not bought and k > 0:
            price = _open(panel, BTC, day)
            if price is not None:
                usd = initial * share
                qty = costs.buy_qty(usd, price, BTC_QV, mult)
                cash -= usd
                bought = True
        close = _close(panel, BTC, day) or 0.0
        curve.append((day, cash + qty * close))
    if share >= 1:
        return Benchmark("btc", "BTC comprar y mantener", curve)
    return Benchmark("btc90", "90 % BTC + 10 % USDT", curve)


def btc_filtered(
    panel: Panel,
    views: dict[date, DayView],
    days: list[date],
    *,
    share: float = 0.9,
    initial: float = 100.0,
    mult: float = 1.0,
) -> Benchmark:
    """90 % en BTC cuando el régimen es favorable al cierre de t (ejecución en t+1)."""
    cash, qty = initial, 0.0
    want: bool | None = None
    curve = []
    for day in days:
        price = _open(panel, BTC, day)
        if want is not None and price is not None:
            if want and qty == 0:
                usd = (cash + qty * price) * share
                qty = costs.buy_qty(usd, price, BTC_QV, mult)
                cash -= usd
            elif not want and qty > 0:
                cash += costs.sell_usd(qty, price, BTC_QV, mult)
                qty = 0.0
        close = _close(panel, BTC, day) or 0.0
        curve.append((day, cash + qty * close))
        view = views.get(day)
        if view is not None and view.regime is not None:
            want = view.regime.favorable
    return Benchmark("btc_filtro", "BTC con el mismo filtro", curve)


def top10_basket(
    panel: Panel,
    views: dict[date, DayView],
    days: list[date],
    *,
    initial: float = 100.0,
    every: int = 28,
    mult: float = 1.0,
) -> Benchmark:
    """Las 10 más líquidas del universo, a partes iguales, reequilibradas cada ``every``
    días (decisión al cierre, ejecución en la apertura siguiente). Se reequilibra por
    diferencia (solo se vende lo que sobra y se compra lo que falta) y cada moneda paga
    el coste del tramo de su propia mediana de volumen."""
    cash = initial
    held: dict[str, float] = {}
    last_close: dict[str, float] = {}
    last_seen: dict[str, date] = {}
    pending: tuple[list[str], dict[str, float]] | None = None
    curve = []
    for k, day in enumerate(days):
        if pending is not None:
            cash = _rebalance(panel, day, held, cash, last_close, *pending, mult)
            for symbol in held:
                last_close.setdefault(symbol, _open(panel, symbol, day) or 0.0)
                last_seen.setdefault(symbol, day)
            pending = None
        for symbol, qty in list(held.items()):
            series = panel.series[symbol]
            i = series.at(day)
            if i is not None and series.traded(i):
                last_close[symbol], last_seen[symbol] = series.close[i], day
            elif (day - last_seen.get(symbol, day)).days >= 7:  # retirada
                cash += qty * last_close.get(symbol, 0.0) * DELISTED_HAIRCUT
                del held[symbol]
        value = cash + sum(q * last_close.get(s, 0.0) for s, q in held.items())
        curve.append((day, value))
        view = views.get(day)
        if view is not None and k % every == 0:
            members = view.universe.members
            qv = {m.symbol: m.median_quote_volume for m in members}
            pending = ([m.symbol for m in members[:10]], qv)
    return Benchmark("cesta10", "Cesta de las 10 más negociadas", curve)


def _rebalance(
    panel: Panel,
    day: date,
    held: dict[str, float],
    cash: float,
    last_close: dict[str, float],
    targets: list[str],
    qv: dict[str, float],
    mult: float,
) -> float:
    """Lleva la cesta al mismo peso en la apertura de ``day`` vendiendo solo lo que sobra y
    comprando lo que falta. Una moneda sin negociación ese día no se toca (cuenta en el
    total a su último cierre). Devuelve el efectivo; ``held`` se actualiza en el sitio."""
    opens = {s: _open(panel, s, day) for s in sorted(set(held) | set(targets))}
    prices = {s: p for s, p in opens.items() if p is not None}
    value = cash + sum(q * prices.get(s, last_close.get(s, 0.0)) for s, q in held.items())
    goal = value / max(len(targets), 1)
    for symbol in sorted(held):  # primero las ventas: lo que sale y lo que sobra
        price = prices.get(symbol)
        if price is None:
            continue
        extra = held[symbol] - (goal / price if symbol in targets else 0.0)
        if extra > 1e-12:
            cash += costs.sell_usd(extra, price, qv.get(symbol), mult)
            held[symbol] -= extra
            if symbol not in targets:
                del held[symbol]
    # Después las compras: lo que falta a cada una (con los costes de las ventas no llega
    # del todo: se reparte en proporción).
    wants = {s: goal - held.get(s, 0.0) * prices[s] for s in targets if s in prices}
    wants = {s: w for s, w in wants.items() if w > 1e-9}
    need = sum(wants.values())
    scale = min(1.0, cash / need) if need > 0 else 0.0
    for symbol in sorted(wants):
        usd = wants[symbol] * scale
        if usd > 0:
            qty = costs.buy_qty(usd, prices[symbol], qv.get(symbol), mult)
            held[symbol] = held.get(symbol, 0.0) + qty
            cash -= usd
    return cash


def daily_returns(curve: Curve) -> list[float]:
    return [b / a - 1 for (_, a), (_, b) in itertools.pairwise(curve) if a > 0]


def forward_window(day: date, horizon: int) -> tuple[date, date]:
    return day + timedelta(days=1), day + timedelta(days=1 + horizon)

"""Estrategias de referencia con el mismo capital y en el mismo periodo que el copy-trading:
comprar y mantener, compra semanal (DCA) y seguimiento de tendencia, sobre SOL, BTC, ETH y
el S&P 500 tokenizado (SPYx). Usan los precios horarios de los pools (``poolprice``).

Costes por operación: comisión del pool más deslizamiento (``cost_pct``) y la comisión de
red con prioridad. Al final se valora todo como si se vendiera (con sus costes), igual que
una posición que hay que cerrar.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from copybot.backtest.stats import max_drawdown_pct

ZERO = Decimal(0)
ONE = Decimal(1)
HOUR = 3600
Series = Sequence[tuple[int, Decimal]]  # (hora, precio en USD), en orden


@dataclass(frozen=True)
class RefParams:
    capital_usd: Decimal = Decimal(100)
    cost_pct: Decimal = Decimal("0.3")  # comisión del pool + deslizamiento, por operación
    tx_fee_usd: Decimal = Decimal("0.02")  # ~0,000105 SOL de comisión de red con prioridad
    dca_every_s: int = 7 * 86400
    trend_hours: int = 24  # media móvil de las últimas 24 horas
    trend_min_samples: int = 20  # horas con precio necesarias para calcular la media
    cost_multiplier: Decimal = ONE

    @property
    def cost(self) -> Decimal:
        return self.cost_pct / 100 * self.cost_multiplier

    @property
    def fee_usd(self) -> Decimal:
        return self.tx_fee_usd * self.cost_multiplier


@dataclass(frozen=True)
class RefResult:
    strategy: str
    asset: str
    final_usd: Decimal
    trades: int
    curve: list[tuple[int, Decimal]] = field(repr=False)
    initial_usd: Decimal = Decimal(100)

    @property
    def return_pct(self) -> Decimal:
        return (self.final_usd / self.initial_usd - 1) * 100

    @property
    def max_drawdown_pct(self) -> Decimal:
        return max_drawdown_pct(self.curve)


def _buy(usd: Decimal, price: Decimal, p: RefParams) -> Decimal:
    """Unidades que se reciben al gastar ``usd``."""
    net = usd - p.fee_usd
    return ZERO if net <= 0 else net * (ONE - p.cost) / price


def _sell(units: Decimal, price: Decimal, p: RefParams) -> Decimal:
    """Dólares que se reciben al vender ``units``."""
    return max(ZERO, units * price * (ONE - p.cost) - p.fee_usd) if units else ZERO


def _in_period(series: Series, start: int, end: int) -> list[tuple[int, Decimal]]:
    return [(t, price) for t, price in series if start <= t <= end]


def buy_and_hold(asset: str, series: Series, start: int, end: int, p: RefParams) -> RefResult:
    period = _in_period(series, start, end)
    if not period:
        raise ValueError(f"sin precios de {asset} en el periodo")
    units = _buy(p.capital_usd, period[0][1], p)
    curve = [(t, units * price) for t, price in period]
    final = _sell(units, period[-1][1], p)
    return RefResult("comprar y mantener", asset, final, 2, curve, p.capital_usd)


def dca(asset: str, series: Series, start: int, end: int, p: RefParams) -> RefResult:
    """El capital se reparte en compras iguales cada ``dca_every_s`` dentro de
    ``[start, end)`` (la primera al empezar); lo que aún no se ha invertido espera en USDC
    sin rendimiento."""
    period = _in_period(series, start, end)
    if not period:
        raise ValueError(f"sin precios de {asset} en el periodo")
    schedule = list(range(start, end, p.dca_every_s))
    amount = p.capital_usd / len(schedule)
    cash, units, done = p.capital_usd, ZERO, 0
    curve = []
    for t, price in period:
        while done < len(schedule) and schedule[done] <= t:
            units += _buy(amount, price, p)
            cash -= amount
            done += 1
        curve.append((t, cash + units * price))
    final = cash + _sell(units, period[-1][1], p)
    return RefResult("compra semanal (DCA)", asset, final, done + 1, curve, p.capital_usd)


def trend(asset: str, series: Series, start: int, end: int, p: RefParams) -> RefResult:
    """Dentro mientras el precio está por encima de su media de ``trend_hours`` horas y
    fuera (en USDC) cuando cae por debajo. La media es la de los precios de las últimas
    ``trend_hours`` horas (al menos ``trend_min_samples``). La decisión se toma con el
    precio de una hora y se ejecuta al siguiente precio: nunca con información del futuro.
    ``series`` debe traer también las horas previas a ``start`` para la primera media."""
    cash, units, trades = p.capital_usd, ZERO, 0
    pending: str | None = None
    curve = []
    last_price = None
    for i, (t, price) in enumerate(series):
        if t > end:
            break
        if t < start:
            continue
        if pending == "comprar":
            units, cash, trades = _buy(cash, price, p), ZERO, trades + 1
        elif pending == "vender":
            cash, units, trades = _sell(units, price, p), ZERO, trades + 1
        pending = None
        since = t - p.trend_hours * HOUR
        window = [q for t2, q in series[max(0, i - p.trend_hours + 1) : i + 1] if t2 > since]
        if len(window) >= p.trend_min_samples:
            above = price > sum(window, ZERO) / len(window)
            if above and not units:
                pending = "comprar"
            elif not above and units:
                pending = "vender"
        curve.append((t, cash + units * price))
        last_price = price
    if last_price is None:
        raise ValueError(f"sin precios de {asset} en el periodo")
    if units:
        cash, trades = _sell(units, last_price, p), trades + 1
    return RefResult(
        f"tendencia (media {p.trend_hours} h)", asset, cash, trades, curve, p.capital_usd
    )


def references(asset: str, series: Series, start: int, end: int, p: RefParams) -> list[RefResult]:
    return [
        buy_and_hold(asset, series, start, end, p),
        dca(asset, series, start, end, p),
        trend(asset, series, start, end, p),
    ]

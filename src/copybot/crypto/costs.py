"""Costes de Binance spot y ejecución simulada (apartado 6.5 del diseño).

- **Comisión** `FEE`: 0,10 % por lado (*taker*, sin descuento con BNB).
- **Medio diferencial más deslizamiento** `c` por lado, según la mediana de volumen de 30 días
  del par a fecha t (tramos de `COST_TIERS`).

El backtest trabaja en `float` y ejecuta a la apertura de la vela siguiente. La prueba en vivo
trabaja en `Decimal`, al ask (compra) o al bid (venta) de un `bookTicker`, con el `stepSize` y
el `minNotional` del par. Aquí no se envía nada a ningún sitio: solo se calcula lo que habría
pasado.

La calibración (`calibrate`) mide en fotos del libro si la tabla se queda corta. Si un tramo
medido supera al de la tabla, la tabla se sube **a mano** antes de registrar v1; por eso la
tabla es una constante con huella (`cost_table_hash`) y no un parámetro.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

FEE = 0.001  # comisión por lado: 0,10 % (taker, sin descuento con BNB)

# (mediana mínima de volumen de 30 días en USDT, c por lado), de más a menos líquido.
# Por debajo del último límite (5 M$) se aplica el último tramo, el más caro.
COST_TIERS: tuple[tuple[float, float], ...] = (
    (200_000_000.0, 0.0002),  # ≥ 200 M$: 0,02 %
    (50_000_000.0, 0.0004),  # 50-200 M$: 0,04 %
    (10_000_000.0, 0.0008),  # 10-50 M$: 0,08 %
    (5_000_000.0, 0.0015),  # 5-10 M$: 0,15 %
)

MIN_ORDER_USD = Decimal(5)  # mínimo propio de cualquier orden, aunque minNotional sea menor
THIN_BOOK_SLIPPAGE = Decimal("0.005")  # 0,5 % extra si askQty·ask no cubre el importe
CALIBRATION_CUSHION = 0.0002  # colchón del 0,02 % sobre el p75 del medio diferencial
CALIBRATION_PERCENTILE = 0.75
THIN_BOOK_USD = 25.0  # una foto con askQty·ask por debajo de esto cuenta como libro fino
THIN_BOOK_COMMON = 0.25  # más de esta fracción de fotos finas: el libro fino es «habitual»

_FEE_DEC = Decimal(str(FEE))


# --- Tabla de costes ------------------------------------------------------------------------


def _tier_index(median_qv: float) -> int:
    for i, (min_volume, _) in enumerate(COST_TIERS):
        if median_qv >= min_volume:
            return i
    return len(COST_TIERS) - 1


def cost_per_side(median_qv: float, multiplier: float = 1.0) -> float:
    """Medio diferencial más deslizamiento `c` por lado, en tanto por uno.

    Se elige el tramo con la mediana de volumen de 30 días a fecha t; por debajo de 5 M$ se
    aplica el tramo de 5-10 M$. `multiplier` es la prueba de estrés de costes (×2 obligatoria,
    ×3 informativa).
    """
    if multiplier < 0:
        raise ValueError("el multiplicador de costes no puede ser negativo")
    return COST_TIERS[_tier_index(float(median_qv))][1] * multiplier


def round_trip_cost(median_qv: float, multiplier: float = 1.0) -> float:
    """Coste aproximado de ida y vuelta, 2·(f + c), en tanto por uno (columna de la tabla)."""
    return 2 * (FEE * multiplier + cost_per_side(median_qv, multiplier))


def cost_table_hash(tiers: Sequence[tuple[float, float]] = COST_TIERS, fee: float = FEE) -> str:
    """sha256 (hexadecimal) de la tabla de costes en forma canónica: comisión y tramos.

    Forma parte de la huella del informe del backtest: si la tabla cambia, cambia la huella.
    """
    canonical = json.dumps(
        {"fee": float(fee), "tiers": [[float(v), float(c)] for v, c in tiers]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Backtest (float, a la apertura de la vela siguiente) -----------------------------------


def buy_qty(usd: float, open_price: float, median_qv: float, mult: float = 1.0) -> float:
    """Cantidad que se compra con `usd` a la apertura `O(t+1)`.

    `qty = usd / (O·(1 + c)·(1 + f))`. Con la prueba de estrés, `mult` multiplica tanto `c`
    como la comisión: costes ×2 es el doble del coste total.
    """
    if open_price <= 0:
        raise ValueError("precio de apertura no válido")
    c = cost_per_side(median_qv, mult)
    return usd / (open_price * (1 + c) * (1 + FEE * mult))


def sell_usd(qty: float, price: float, median_qv: float, mult: float = 1.0) -> float:
    """Efectivo que entra al vender `qty` a la apertura `O(t+1)`.

    `qty·O·(1 − c)·(1 − f)`, con `mult` aplicado a `c` y a la comisión, igual que en la compra.
    """
    c = cost_per_side(median_qv, mult)
    return qty * price * (1 - c) * (1 - FEE * mult)


# --- En vivo (Decimal, al ask o al bid de un bookTicker) ------------------------------------


@dataclass(frozen=True)
class LiveFill:
    """Ejecución simulada en vivo.

    `net_usd` es lo que sale del efectivo en una compra (bruto + comisión) y lo que entra en
    una venta (bruto − comisión).
    """

    qty: Decimal
    price: Decimal  # precio de ejecución (con el 0,5 % extra si el libro es fino)
    fee_usd: Decimal
    gross_usd: Decimal  # qty·precio
    net_usd: Decimal
    extra_slippage: bool = False
    note: str = ""


def round_qty(qty: Decimal, step: Decimal | None) -> Decimal:
    """Redondea la cantidad **a la baja** al múltiplo de `step` (`stepSize` del par).

    Sin `step` (None o 0) la cantidad no cambia.
    """
    if not step:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step


def min_notional_ok(qty: Decimal, price: Decimal, min_notional: Decimal | None) -> bool:
    """La orden es válida si `qty·precio ≥ max(minNotional, 5 $)`."""
    floor = max(min_notional or Decimal(0), MIN_ORDER_USD)
    return qty * price >= floor


def _usd(value: Decimal) -> str:
    """Importe en formato español: 1.234,56 $."""
    text = f"{value:,.2f}".translate(str.maketrans(",.", ".,"))
    return f"{text} $"


def _qty(value: Decimal) -> str:
    return format(value, "f").replace(".", ",")


def live_buy(
    usd: Decimal,
    ask: Decimal,
    ask_qty: Decimal,
    step: Decimal | None,
    min_notional: Decimal | None,
) -> LiveFill | None:
    """Compra simulada al ask con un importe máximo `usd` (comisión incluida).

    - Si `askQty·ask < usd` (libro fino), el precio sube un 0,5 % y se anota.
    - `qty = usd / (precio·(1 + f))`, redondeada a la baja al `stepSize`: nunca se gasta más
      de `usd`.
    - Devuelve None (no se compra) si `qty·precio < max(minNotional, 5 $)`.
    """
    if ask <= 0:
        raise ValueError("ask no válido: sin libro no se puede comprar")
    thin = ask_qty * ask < usd
    price = ask * (1 + THIN_BOOK_SLIPPAGE) if thin else ask
    qty = round_qty(usd / (price * (1 + _FEE_DEC)), step)
    if qty <= 0 or not min_notional_ok(qty, price, min_notional):
        return None
    gross = qty * price
    fee = gross * _FEE_DEC
    note = ""
    if thin:
        note = (
            f"libro fino: askQty·ask = {_usd(ask_qty * ask)} < {_usd(usd)}; "
            "se aplica un 0,5 % extra al precio"
        )
    return LiveFill(
        qty=qty,
        price=price,
        fee_usd=fee,
        gross_usd=gross,
        net_usd=gross + fee,
        extra_slippage=thin,
        note=note,
    )


def live_sell(qty: Decimal, bid: Decimal, step: Decimal | None) -> LiveFill:
    """Venta simulada al bid, con la cantidad redondeada a la baja al `stepSize`.

    Si el redondeo deja un resto sin vender, se anota. La comprobación de `minNotional` queda
    en manos del llamador (`min_notional_ok`).
    """
    if bid <= 0:
        raise ValueError("bid no válido: sin libro no se puede vender")
    sold = round_qty(qty, step)
    gross = sold * bid
    fee = gross * _FEE_DEC
    note = ""
    if sold != qty:
        note = f"se venden {_qty(sold)} de {_qty(qty)}: el resto no llega al stepSize"
    return LiveFill(
        qty=sold, price=bid, fee_usd=fee, gross_usd=gross, net_usd=gross - fee, note=note
    )


# --- Calibración con fotos del libro --------------------------------------------------------

# (símbolo, bid, ask, askQty, mediana de volumen de 30 días en USDT)
Snapshot = tuple[str, Decimal | float, Decimal | float, Decimal | float, Decimal | float]


def _percentile(values: Sequence[float], q: float) -> float:
    """Percentil con interpolación lineal entre los puntos ordenados (como numpy por defecto)."""
    xs = sorted(values)
    pos = q * (len(xs) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _pct(x: float) -> str:
    text = f"{x * 100:.4f}".rstrip("0").rstrip(".")
    return f"{text.replace('.', ',')} %"


def tier_label(index: int) -> str:
    """Nombre del tramo en la tabla: «≥ 200 M$», «50-200 M$»…"""
    low = COST_TIERS[index][0] / 1e6
    if index == 0:
        return f"≥ {low:g} M$"
    high = COST_TIERS[index - 1][0] / 1e6
    return f"{low:g}-{high:g} M$"


@dataclass(frozen=True)
class TierCalibration:
    index: int  # posición en COST_TIERS
    min_volume: float
    table_cost: float  # c de la tabla
    samples: int
    measured_cost: float | None  # p75 de (ask − bid)/(2·mid) + 0,02 %; None sin fotos
    thin_share: float | None  # fracción de fotos con askQty·ask < 25 $

    @property
    def label(self) -> str:
        return tier_label(self.index)

    @property
    def exceeds_table(self) -> bool:
        return self.measured_cost is not None and self.measured_cost > self.table_cost

    @property
    def thin_book_common(self) -> bool:
        return self.thin_share is not None and self.thin_share > THIN_BOOK_COMMON


@dataclass(frozen=True)
class Calibration:
    tiers: tuple[TierCalibration, ...]
    invalid: int = 0  # fotos sin bid/ask válidos (cero o cruzados)
    below_min: int = 0  # pares por debajo del último tramo: no entran en el universo

    @property
    def exceeded(self) -> list[TierCalibration]:
        """Tramos cuyo coste medido supera al de la tabla: hay que subir la tabla."""
        return [t for t in self.tiers if t.exceeds_table]

    @property
    def thin_common(self) -> list[TierCalibration]:
        """Tramos con libro fino habitual: pasan al siguiente nivel de coste."""
        return [t for t in self.tiers if t.thin_book_common]

    @property
    def table_ok(self) -> bool:
        return not self.exceeded and not self.thin_common

    def warnings(self) -> list[str]:
        out = [
            f"Tramo {t.label}: el coste medido ({_pct(t.measured_cost or 0.0)}) supera al de la "
            f"tabla ({_pct(t.table_cost)}); hay que subir la tabla antes de registrar v1."
            for t in self.exceeded
        ]
        out += [
            f"Tramo {t.label}: libro fino (askQty·ask < 25 $) en el "
            f"{(t.thin_share or 0.0) * 100:.0f} % de las fotos; pasa al siguiente nivel de coste."
            for t in self.thin_common
        ]
        return out


def calibrate(snapshots: Iterable[Snapshot]) -> Calibration:
    """Mide el coste real por tramo con fotos de `bookTicker` + `ticker/24hr`.

    Para cada tramo: percentil 75 del medio diferencial (ask − bid)/(2·mid) más un colchón del
    0,02 %, y la fracción de fotos con `askQty·ask < 25 $`. Las fotos con precios no válidos y
    las de pares por debajo del último tramo (fuera del universo) se cuentan pero no se usan.
    """
    spreads: list[list[float]] = [[] for _ in COST_TIERS]
    thin = [0] * len(COST_TIERS)
    invalid = below_min = 0
    lowest = COST_TIERS[-1][0]
    for _symbol, bid, ask, ask_qty, median_qv in snapshots:
        b, a, depth, volume = float(bid), float(ask), float(ask_qty), float(median_qv)
        if not 0 < b <= a:
            invalid += 1
            continue
        if not volume >= lowest:
            below_min += 1
            continue
        i = _tier_index(volume)
        mid = (a + b) / 2
        spreads[i].append((a - b) / (2 * mid))
        if depth * a < THIN_BOOK_USD:
            thin[i] += 1
    tiers = tuple(
        TierCalibration(
            index=i,
            min_volume=min_volume,
            table_cost=table_cost,
            samples=len(values),
            measured_cost=(
                _percentile(values, CALIBRATION_PERCENTILE) + CALIBRATION_CUSHION
                if values
                else None
            ),
            thin_share=thin[i] / len(values) if values else None,
        )
        for i, ((min_volume, table_cost), values) in enumerate(
            zip(COST_TIERS, spreads, strict=True)
        )
    )
    return Calibration(tiers=tiers, invalid=invalid, below_min=below_min)

"""Plantillas fijas del informe diario en castellano llano (apartados 10.2 y 10.3 del diseño).

Todo es **puro e informativo**: formatea cifras, fechas y frases, y calcula métricas de
contexto de cada moneda (rentabilidades, peso del mejor día, semana mala, nivel de riesgo,
escenario de desplome). **Nada de lo que hay aquí decide compras ni ventas**: eso lo hace
``strategy.decide()``.

Reglas de redacción:
- formato español: coma decimal, punto de miles, signo y unidad («+12,3 %», «1.234,50 $»);
- la hora que se enseña es la de Madrid (``zoneinfo("Europe/Madrid")``); por dentro, UTC;
- ninguna plantilla ni texto generado puede prometer nada: ``check_text()`` lo comprueba.
"""

from __future__ import annotations

import math
import statistics
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")
MINUS = "−"  # signo menos tipográfico, como en la maqueta del informe
NO_DATA = "—"

WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MONTHS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
MONTHS_SHORT = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

# --- Métricas informativas (apartado 10.2) ----------------------------------------------
BEST_DAY_WINDOW = 30  # días de la ventana del «peso del mejor día»
BEST_DAY_WARNING = 0.6  # por encima: aviso de «subida de un solo día»
BEST_DAY_MIN_RISE = 0.10  # por debajo de un +10 % en 30 días el peso no se calcula
BEST_DAY_SPREAD = 0.5  # hasta aquí se puede decir que «la subida está repartida»
YEAR_DAYS = 365  # velas necesarias para medir la «semana mala»
WEEK_DAYS = 7

# Riesgo: Alto si se cumple cualquiera de estas; Bajo si σ < 3 % y ninguna; Medio el resto.
RISK_HIGH_VOL = 0.06  # σ diaria
RISK_LOW_VOL = 0.03
RISK_MIN_EPISODE_DAYS = 180
RISK_MIN_MEDIAN_QV = 20_000_000.0  # USDT al día (mediana de 30 días)
RISK_MAX_SPREAD_PCT = 0.2  # en porcentaje, como ``BookTicker.spread_pct`` (0,2 = 0,2 %)
RISK_LEVELS = ("Bajo", "Medio", "Alto")

# Escenario de desplome: BTC y ETH −30 %, el resto −50 %, sobre las posiciones actuales.
CRASH_MAJORS = frozenset({"BTC", "ETH"})
CRASH_MAJOR_DROP = 0.30
CRASH_OTHER_DROP = 0.50
QUOTE = "USDT"

# Si una compra pesa más que esta fracción del volumen diario, ya no se dice que «no mueve
# el precio» (con 18 $ solo pasa por debajo de 180.000 $ al día).
PRICE_IMPACT_SHARE = 0.0001

# --- Confianza en la regla (una por informe; nunca una probabilidad por moneda) ----------
CONFIDENCE_LEVELS = ("SIN DATOS", "MUY BAJA", "BAJA", "MEDIA", "ALTA")
CONFIDENCE_TEXT = {
    "SIN DATOS": "Aún no hay backtest de esta versión de la regla: no se sabe cómo le habría ido.",
    "MUY BAJA": "En el pasado, esta regla lo hizo peor que comprar BTC y esperar.",
    "BAJA": "En el pasado ganó a BTC de media, pero el margen de error incluye perder.",
    "MEDIA": (
        "En el pasado ganó a BTC por un margen que no parece suerte, pero la prueba en vivo "
        "aún no lo confirma."
    ),
    "ALTA": (
        "El backtest y la prueba en vivo ganan a BTC por un margen que no parece suerte. "
        "Aun así, sin garantía: nadie sabe qué pasará."
    ),
}

# --- Motivos de venta (apartado 6.1, paso 3; mismas frases que ``strategy.decide()``) ----
SELL_PURPOSES = (
    "regimen",
    "fuera_universo",
    "stop",
    "tendencia",
    "puesto",
    "retirada",
    "fin_prueba",
    "horizonte_7d",
)
UNKNOWN_UNIVERSE_REASON = "sin datos suficientes para puntuarla"

# --- Reglas de redacción (apartado 10.3) --------------------------------------------------
FORBIDDEN = (
    "garantiz",
    "seguro que",
    "va a subir",
    "sin riesgo",
    "asegur",
    "la más rentable",
    "beneficio asegurado",
)
# Avisos fijos del diseño que contienen una expresión prohibida **negada** (maqueta 10.1 y
# JSON del semáforo 9.3). Solo se admiten tal cual, completos; cualquier otro uso falla.
ALLOWED_DISCLAIMERS = (
    "lo pasado no garantiza lo futuro",
    "un semáforo en verde no garantiza beneficios futuros",
)


# =========================================================================================
# Números
# =========================================================================================


def _finite(x: float | int | Decimal | None) -> float | None:
    """``x`` como ``float`` si es un número finito; ``None`` si falta, es NaN o infinito."""
    if x is None:
        return None
    try:
        value = float(x)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _decimal(x: float | int | Decimal) -> Decimal | None:
    if isinstance(x, Decimal):
        value = x
    elif isinstance(x, int):
        value = Decimal(x)
    else:
        if not math.isfinite(x):
            return None
        value = Decimal(repr(float(x)))
    return value if value.is_finite() else None


def _group_thousands(digits: str) -> str:
    head = len(digits) % 3 or 3
    groups = [digits[:head]] + [digits[i : i + 3] for i in range(head, len(digits), 3)]
    return ".".join(groups)


def fmt_num(x: float | int | Decimal | None, decimals: int = 0, *, sign: bool = False) -> str:
    """Número en formato español: «1.234,50». Redondeo al más cercano (los medios hacia
    fuera), signo menos tipográfico y nunca «−0». Con ``sign`` se añade «+» a los
    positivos. ``None`` o un valor no finito → «—»."""
    if x is None:
        return NO_DATA
    value = _decimal(x)
    if value is None:
        return NO_DATA
    with localcontext() as ctx:  # sin límite práctico de cifras: nunca falla por tamaño
        ctx.prec = max(ctx.prec, value.adjusted() + decimals + 3)
        q = value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    int_part, _, frac = f"{q.copy_abs():f}".partition(".")
    text = _group_thousands(int_part) + (f",{frac}" if decimals > 0 else "")
    if q < 0:
        return MINUS + text
    if sign and q > 0:
        return "+" + text
    return text


def fmt_pct(x: float | Decimal | None, decimals: int = 1, sign: bool = True) -> str:
    """Una fracción como porcentaje: 0,123 → «+12,3 %»."""
    if x is None:
        return NO_DATA
    value = _decimal(x)
    if value is None:
        return NO_DATA
    return f"{fmt_num(value * 100, decimals, sign=sign)} %"


def fmt_usd(x: float | Decimal | None, decimals: int = 2, *, sign: bool = False) -> str:
    """Dólares: 1234,5 → «1.234,50 $»."""
    text = fmt_num(x, decimals, sign=sign)
    return NO_DATA if text == NO_DATA else f"{text} $"


def fmt_musd(x: float | Decimal | None) -> str:
    """Volumen en millones de dólares: 2.100.000.000 → «2.100 M$». Por debajo de 10 M$
    (ya redondeado), con un decimal («1,2 M$»; 9,96 M$ → «10 M$»)."""
    if x is None:
        return NO_DATA
    value = _decimal(x)
    if value is None:
        return NO_DATA
    millions = value / 1_000_000
    return f"{fmt_num(millions, 1 if abs(millions) < Decimal('9.95') else 0)} M$"


def fmt_points(score: float | None) -> str:
    """Puntuación de 0 a 100 sin decimales: 93,6 → «94»."""
    return fmt_num(score, 0)


def fmt_pp(diff: float | None) -> str:
    """Diferencia de rentabilidades (fracción) en puntos: 0,09 → «+9 puntos». Es la
    columna «Frente a BTC (30 d)»: ``m_{i,30} − m_{BTC,30}``."""
    value = None if diff is None else _decimal(diff)
    if value is None:
        return NO_DATA
    text = fmt_num(value * 100, 0, sign=True)
    return f"{text} {'punto' if text.lstrip('+' + MINUS) == '1' else 'puntos'}"


def fmt_vol(sigma: float | None, decimals: int = 1) -> str:
    """«Se mueve al día»: σ diaria como «±3,9 %»."""
    if _finite(sigma) is None:
        return NO_DATA
    return f"±{fmt_pct(sigma, decimals, sign=False)}"


def fmt_trend(trend_share: float | None) -> str:
    """Tendencia: fracción de medias (20, 50 y 100) por encima de las que cierra → «2 de 3»."""
    share = _finite(trend_share)
    if share is None:
        return NO_DATA
    return f"{round(share * 3)} de 3"


# =========================================================================================
# Fechas y horas (se enseñan en hora de Madrid; por dentro, UTC)
# =========================================================================================


def madrid_time(dt_utc: datetime) -> datetime:
    """Pasa un instante a hora de Madrid. Un ``datetime`` sin zona se toma como UTC."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=UTC)
    return dt_utc.astimezone(MADRID)


def _local_date(d: date | datetime) -> date:
    return madrid_time(d).date() if isinstance(d, datetime) else d


def fmt_date_long(d: date | datetime) -> str:
    """«miércoles 21 de octubre de 2026». Un ``datetime`` se pasa antes a hora de Madrid."""
    d = _local_date(d)
    return f"{WEEKDAYS[d.weekday()]} {d.day} de {MONTHS[d.month - 1]} de {d.year}"


def fmt_date_short(d: date | datetime, *, year: bool = False) -> str:
    """«14-oct» o, con el año, «23-oct-2026»."""
    d = _local_date(d)
    text = f"{d.day}-{MONTHS_SHORT[d.month - 1]}"
    return f"{text}-{d.year}" if year else text


def fmt_weekday_day(d: date | datetime) -> str:
    """«martes 20»."""
    d = _local_date(d)
    return f"{WEEKDAYS[d.weekday()]} {d.day}"


def fmt_clock(dt_utc: datetime) -> str:
    """Hora de Madrid de un instante: «02:00»."""
    return madrid_time(dt_utc).strftime("%H:%M")


def cutoff_text(day: date) -> str:
    """Hasta cuándo llegan los datos: la vela diaria de ``day`` cierra a las 00:00 UTC del
    día siguiente (las 02:00 en verano y la 01:00 en invierno, en Madrid)."""
    close = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
    return (
        f"Datos de Binance hasta el cierre del {fmt_weekday_day(day)} "
        f"({fmt_clock(close)}, hora de Madrid)"
    )


# =========================================================================================
# Métricas informativas de cada moneda (cierres diarios del episodio, el último es C(t))
# =========================================================================================


def ret(closes: Sequence[float], days: int) -> float | None:
    """Rentabilidad simple a ``days`` días: ``C(t)/C(t−L) − 1``. ``None`` sin historia."""
    if days <= 0 or len(closes) <= days:
        return None
    base, last = _finite(closes[-1 - days]), _finite(closes[-1])
    if base is None or last is None or base <= 0 or last <= 0:
        return None
    return last / base - 1


def _window_ok(closes: Sequence[float], n: int) -> bool:
    """Hay ``n`` cierres al final y todos son números finitos y positivos."""
    return len(closes) >= n and all((_finite(c) or 0.0) > 0 for c in closes[-n:])


def best_day(closes: Sequence[float]) -> tuple[int, float] | None:
    """Mejor día de los últimos 30: (índice en ``closes``, rentabilidad simple de ese día).
    Con empate, el primero."""
    if not _window_ok(closes, BEST_DAY_WINDOW + 1):
        return None
    start = len(closes) - BEST_DAY_WINDOW
    k = max(range(start, len(closes)), key=lambda j: math.log(closes[j] / closes[j - 1]))
    return k, closes[k] / closes[k - 1] - 1


def best_day_share(closes: Sequence[float]) -> float | None:
    """Peso del mejor día: ``máx r_i`` de los últimos 30 días ÷ ``ln(1 + R30)``, con
    rendimientos logarítmicos. ``None`` si la subida de 30 días no llega al
    ``BEST_DAY_MIN_RISE`` (con subidas pequeñas el cociente se dispara y no dice nada) o
    sin historia. Puede pasar de 1 si el resto de días restaron."""
    if not _window_ok(closes, BEST_DAY_WINDOW + 1):
        return None
    total = math.log(closes[-1] / closes[-1 - BEST_DAY_WINDOW])
    if total <= math.log1p(BEST_DAY_MIN_RISE):
        return None
    best = max(
        math.log(closes[j] / closes[j - 1])
        for j in range(len(closes) - BEST_DAY_WINDOW, len(closes))
    )
    return best / total


def p5_week(closes: Sequence[float]) -> float | None:
    """Semana mala: percentil 5 (interpolación lineal) de las rentabilidades a 7 días
    (solapadas) dentro de las 365 últimas velas. ``None`` con menos de 365 velas."""
    if not _window_ok(closes, YEAR_DAYS):
        return None
    window = closes[-YEAR_DAYS:]
    weekly = [window[k] / window[k - WEEK_DAYS] - 1 for k in range(WEEK_DAYS, len(window))]
    return statistics.quantiles(weekly, n=20, method="inclusive")[0]


def momentum_percentile(value: float, population: Sequence[float]) -> float | None:
    """Posición de ``value`` dentro de ``population`` (que incluye a la propia moneda),
    con la fórmula del apartado 5.1: ``(#menores + ½·(#iguales − 1)) / (n − 1)``.

    Si ``value`` no aparece en ``population``, se cuenta como un miembro más (así el
    resultado sigue entre 0 y 1). Los valores no finitos no cuentan. ``None`` si no hay
    con quién comparar o ``value`` no es un número finito."""
    v = _finite(value)
    if v is None:
        return None
    pop = [x for x in map(_finite, population) if x is not None]
    lower = sum(1 for x in pop if x < v)
    equal = sum(1 for x in pop if x == v)
    n = len(pop)
    if equal == 0:
        equal, n = 1, n + 1
    if n < 2:
        return None
    return (lower + 0.5 * (equal - 1)) / (n - 1)


def breadth(above_sma50: Iterable[bool]) -> float | None:
    """Amplitud: fracción del universo que cierra por encima de su SMA50. Contexto, no
    decide nada."""
    flags = list(above_sma50)
    return sum(flags) / len(flags) if flags else None


def breadth_text(share: float | None) -> str:
    if share is None:
        return "Amplitud: sin datos."
    return (
        f"Amplitud: el {fmt_pct(share, 0, sign=False)} de las monedas analizadas cierra por "
        "encima de su media de 50 días (solo contexto: no decide nada)."
    )


def risk_reasons(
    vol_daily: float | None,
    episode_days: int | None,
    median_qv: float | Decimal | None,
    best_day_share: float | None,
    spread_pct: float | Decimal | None = None,
) -> list[str]:
    """Motivos que hacen que una moneda sea de riesgo Alto (vacía si no hay ninguno).

    Lo dudoso se resuelve en contra: si falta σ, la antigüedad o el volumen (``None``, NaN
    o infinito), eso ya es un motivo. ``spread_pct`` es ``None`` fuera de la prueba en vivo;
    un ``best_day_share`` que falta significa que no hubo subida (no es motivo)."""
    reasons = []
    vol = _finite(vol_daily)
    if vol is None:
        reasons.append("sin datos de cuánto se mueve al día")
    elif vol > RISK_HIGH_VOL:
        reasons.append(f"se mueve más de un {fmt_pct(RISK_HIGH_VOL, 0, sign=False)} al día")
    age = _finite(episode_days)
    if age is None:
        reasons.append("sin datos de cuánto tiempo lleva cotizando")
    elif age < RISK_MIN_EPISODE_DAYS:
        reasons.append(f"lleva menos de {RISK_MIN_EPISODE_DAYS} días cotizando")
    qv = _finite(median_qv)
    if qv is None:
        reasons.append("sin datos de volumen")
    elif qv < RISK_MIN_MEDIAN_QV:
        reasons.append(f"se negocian menos de {fmt_musd(RISK_MIN_MEDIAN_QV)} al día")
    share = _finite(best_day_share)
    if share is not None and share > BEST_DAY_WARNING:
        reasons.append(
            f"más del {fmt_pct(BEST_DAY_WARNING, 0, sign=False)} de la subida vino de un solo día"
        )
    if spread_pct is not None:
        spread = _finite(spread_pct)
        if spread is None:
            reasons.append("sin datos del diferencial entre compra y venta")
        elif spread > RISK_MAX_SPREAD_PCT:
            reasons.append(
                f"diferencial entre compra y venta mayor del {fmt_num(RISK_MAX_SPREAD_PCT, 1)} %"
            )
    return reasons


def risk_level(
    vol_daily: float | None,
    episode_days: int | None,
    median_qv: float | Decimal | None,
    best_day_share: float | None,
    spread_pct: float | Decimal | None = None,
) -> str:
    """Nivel de riesgo de la tabla 10.2. **Alto** si σ > 6 %, episodio < 180 días, volumen
    < 20 M$, peso del mejor día > 0,6 o (en vivo) diferencial > 0,2 %. **Bajo** si σ < 3 % y
    no se cumple ninguna. **Medio** en el resto. ``spread_pct`` va en porcentaje. Un dato
    que falta cuenta como Alto (``risk_reasons``)."""
    if risk_reasons(vol_daily, episode_days, median_qv, best_day_share, spread_pct):
        return "Alto"
    vol = _finite(vol_daily)
    return "Bajo" if vol is not None and vol < RISK_LOW_VOL else "Medio"


# =========================================================================================
# Escenario de desplome
# =========================================================================================


def _base(symbol: str) -> str:
    return symbol.removesuffix(QUOTE) if symbol.endswith(QUOTE) and symbol != QUOTE else symbol


def crash_scenario(
    positions: Mapping[str, float | Decimal], cash: float | Decimal = 0.0
) -> tuple[float, float]:
    """Pérdida si BTC y ETH cayeran un 30 % y el resto un 50 %: (dólares, fracción del
    patrimonio = posiciones + efectivo). Solo informativo."""
    loss = 0.0
    invested = 0.0
    for symbol, value in positions.items():
        v = float(value)
        invested += v
        drop = CRASH_MAJOR_DROP if _base(symbol) in CRASH_MAJORS else CRASH_OTHER_DROP
        loss += v * drop
    total = invested + float(cash)
    return loss, (loss / total if total > 0 else 0.0)


def crash_text(positions: Mapping[str, float | Decimal], cash: float | Decimal = 0.0) -> str:
    if not positions:
        return "Sin posiciones abiertas: un desplome no afectaría a la cartera de prueba."
    loss, share = crash_scenario(positions, cash)
    return (
        f"Si mañana BTC y ETH cayeran un {fmt_pct(CRASH_MAJOR_DROP, 0, sign=False)} y el resto "
        f"un {fmt_pct(CRASH_OTHER_DROP, 0, sign=False)}, perderías unos {fmt_usd(loss)} "
        f"({fmt_pct(share, 0, sign=False)})."
    )


# =========================================================================================
# Motivos de compra y venta
# =========================================================================================


def buy_reason(rank: int, score: float, trend_share: float) -> str:
    """Motivo de una compra, igual que en ``strategy.decide()``."""
    return check_text(
        f"puesto {rank} con {score:.0f} puntos y tendencia {round(trend_share * 3)} de 3"
    )


def sell_reason(
    purpose: str,
    *,
    universe_reason: str | None = None,
    rank: int | None = None,
    exit_rank: int = 12,
    stop_loss: float = 0.25,
    market_sma: int = 100,
    renamed_to: str | None = None,
    status: str | None = None,
    gap_days: int = 7,
    haircut: float = 0.10,
    test_days: int | None = None,
    horizon_days: int = 7,
) -> str:
    """Frase fija del motivo de una venta (``purpose`` de ``crypto_paper_orders``).

    Las cinco de la regla son las del apartado 6.1, paso 3:
    - ``regimen``: «mercado bajista: BTC cerró por debajo de su media de 100 días»;
    - ``fuera_universo``: «fuera del universo: <motivo de universe.py>»;
    - ``stop``: «stop de emergencia: −25 % desde la compra»;
    - ``tendencia``: «perdió la tendencia (por debajo de 2 de sus 3 medias)»;
    - ``puesto``: «bajó al puesto k (se vende a partir del 13)».
    Las demás cubren retiradas (6.6), el fin de una prueba (11) y el libro de señales (7).
    """
    if purpose == "regimen":
        text = f"mercado bajista: BTC cerró por debajo de su media de {market_sma} días"
    elif purpose == "fuera_universo":
        text = f"fuera del universo: {universe_reason or UNKNOWN_UNIVERSE_REASON}"
    elif purpose == "stop":
        text = f"stop de emergencia: {fmt_pct(-stop_loss, 0)} desde la compra"
    elif purpose == "tendencia":
        text = "perdió la tendencia (por debajo de 2 de sus 3 medias)"
    elif purpose == "puesto":
        if rank is None:
            raise ValueError("el motivo «puesto» necesita el puesto de la moneda")
        text = f"bajó al puesto {rank} (se vende a partir del {exit_rank + 1})"
    elif purpose == "retirada":
        if renamed_to:
            text = f"cambio de nombre a {renamed_to}: se cierra al último precio, sin penalización"
        elif status:
            text = (
                f"dejó de cotizar en Binance (estado {status}): se vende en la siguiente ejecución"
            )
        else:
            text = (
                f"retirada de Binance: {gap_days} días seguidos sin cotizar; se cierra al "
                f"último precio menos un {fmt_pct(haircut, 0, sign=False)}"
            )
    elif purpose == "fin_prueba":
        span = f" de {test_days} días" if test_days else ""
        text = f"fin de la prueba{span}: se vende al precio de venta del momento, menos la comisión"
    elif purpose == "horizonte_7d":
        text = f"libro de señales: se cierra a los {horizon_days} días de la entrada"
    else:
        raise ValueError(f"motivo de venta desconocido: {purpose!r}")
    return check_text(text)


def coin_status(
    *,
    exclusion_reason: str | None = None,
    rank: int | None = None,
    n: int | None = None,
    score: float | None = None,
    trend_share: float | None = None,
) -> str:
    """Situación de una moneda para ``crypto coin`` (apartado 10.3): «Fuera del universo:
    poco volumen…» o «puesto 23 de 84; puntos 61; tendencia 2 de 3»."""
    if exclusion_reason:
        return check_text(f"Fuera del universo: {exclusion_reason}")
    if rank is None:
        return check_text(f"Fuera del universo: {UNKNOWN_UNIVERSE_REASON}")
    where = f"puesto {rank} de {n}" if n else f"puesto {rank}"
    return check_text(f"{where}; puntos {fmt_points(score)}; tendencia {fmt_trend(trend_share)}")


# =========================================================================================
# «POR QUÉ» de cada moneda
# =========================================================================================


def _pct_words(x: float) -> str:
    """Porcentaje sin signo para una frase: sin decimales desde el 10 %, con uno debajo."""
    return fmt_pct(abs(x), 0 if abs(x) >= 0.1 else 1, sign=False)


def _beats_phrase(pct: float, rising: bool) -> str:
    """«más que N de cada 100 monedas analizadas» (o «mejor que…» si no sube)."""
    if pct >= 1:
        return f"{'más' if rising else 'mejor'} que todas las demás monedas analizadas"
    if pct <= 0:
        return f"{'menos' if rising else 'peor'} que todas las demás monedas analizadas"
    n = int(Decimal(repr(pct * 100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    n = min(99, max(1, n))
    return f"{'más' if rising else 'mejor'} que {n} de cada 100 monedas analizadas"


def _join_days(days: Sequence[int]) -> str:
    items = [str(d) for d in days]
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} y {items[-1]}"


def sma_text(above_sma: Mapping[int, bool]) -> str:
    """«está por encima de sus medias de 20, 50 y 100 días», «está por encima de 2 de sus 3
    medias (las de 20 y 50 días)»…"""
    days = sorted(above_sma)
    above = [d for d in days if above_sma[d]]
    total = len(days)
    if not days:
        return "no tiene historia suficiente para sus medias"
    if len(above) == total:
        return f"está por encima de sus medias de {_join_days(days)} días"
    if not above:
        return f"está por debajo de sus medias de {_join_days(days)} días"
    which = f"la de {above[0]}" if len(above) == 1 else f"las de {_join_days(above)}"
    return f"está por encima de {len(above)} de sus {total} medias ({which} días)"


def bad_week_text(p5: float | None) -> str:
    """Frase de la «semana mala» a partir del percentil 5 de las semanas del último año.
    ``None`` es lo que da ``p5_week`` con menos de 365 velas."""
    if p5 is None:
        return "Aún no se puede medir una semana mala: tiene menos de un año en Binance."
    value = _finite(p5)
    if value is None:
        return "No se puede medir una semana mala: faltan datos."
    if value < 0:
        return f"En el último año, 1 de cada 20 semanas cayó más de un {_pct_words(value)}."
    return "En el último año, al menos 19 de cada 20 semanas terminaron sin pérdidas."


def single_day_warning(
    share: float | None, day: date | None = None, day_return: float | None = None
) -> str | None:
    """Aviso de «subida de un solo día» si el peso del mejor día pasa de 0,6."""
    value = _finite(share)
    if value is None or value <= BEST_DAY_WARNING:
        return None
    detail = ""
    if day is not None and _finite(day_return) is not None:
        detail = f" ({fmt_date_short(day)}, {fmt_pct(day_return, 0)})"
    elif day is not None:
        detail = f" ({fmt_date_short(day)})"
    return f"Aviso: más de la mitad de la subida vino de un solo día{detail}."


def liquidity_text(median_qv: float | Decimal, order_usd: float) -> str:
    """«Se negocian 2.100 M$ al día: comprar o vender 18 $ no mueve el precio»."""
    size = fmt_usd(order_usd, 0 if float(order_usd).is_integer() else 2)
    small = float(order_usd) < float(median_qv) * PRICE_IMPACT_SHARE
    effect = "no mueve el precio" if small else "puede mover el precio"
    return f"Se negocian {fmt_musd(median_qv)} al día: comprar o vender {size} {effect}."


def why_text(
    name: str,
    *,
    ret_30d: float | None,
    momentum_pct: float | None,
    above_sma: Mapping[int, bool],
    median_qv: float | Decimal,
    order_usd: float = 18.0,
    best_share: float | None = None,
    p5: float | None = None,
    best_day_info: tuple[date, float] | None = None,
) -> str:
    """Párrafo «POR QUÉ» de una moneda (maqueta 10.1).

    - ``ret_30d``: ``C(t)/C(t−30) − 1``;
    - ``momentum_pct``: percentil de ``m_{i,30}`` en U(t) (``momentum_percentile``), que se
      lee «más que N de cada 100 monedas analizadas»;
    - ``above_sma``: media → cierre por encima (``CoinSignal.above_sma``);
    - ``median_qv``: mediana de volumen de 30 días; ``order_usd``: tamaño de una compra;
    - ``best_share``: ``best_day_share()``; ``p5``: ``p5_week()``;
    - ``best_day_info``: (día, rentabilidad) del mejor día, para el aviso.

    Un dato que falta (``None``, NaN o infinito) se omite de la frase. Las frases del mejor
    día solo salen si la moneda sube en 30 días (o no se sabe).
    """
    ret_30d, momentum_pct = _finite(ret_30d), _finite(momentum_pct)
    best_share = _finite(best_share)
    clauses = []
    if ret_30d is not None:
        if _pct_words(ret_30d) == fmt_pct(0, 1, sign=False):  # se leería «sube un 0,0 %»
            rising = False
            clauses.append("apenas cambia en 30 días")
        else:
            rising = ret_30d > 0
            verb = "sube" if rising else "baja"
            clauses.append(f"{verb} un {_pct_words(ret_30d)} en 30 días")
        if momentum_pct is not None:
            clauses.append(_beats_phrase(momentum_pct, rising))
    if ret_30d is not None and ret_30d <= 0:
        best_share = None  # sin subida no hay «mejor día» que repartir ni que avisar
    sma = sma_text(above_sma)
    if not clauses:
        first = sma
    elif len(clauses) == 1:
        first = f"{clauses[0]} y {sma}"
    else:
        first = f"{', '.join(clauses)}, y {sma}"
    sentences = [f"{name}: {first}."]
    if best_share is not None and best_share <= BEST_DAY_WARNING:
        share_text = fmt_pct(max(best_share, 0.0), 0, sign=False)
        if best_share <= BEST_DAY_SPREAD:
            sentences.append(f"La subida está repartida: su mejor día aportó el {share_text}.")
        else:  # entre la mitad y el umbral del aviso: no se dice que esté «repartida»
            sentences.append(f"Su mejor día aportó el {share_text} de la subida.")
    sentences.append(liquidity_text(median_qv, order_usd))
    sentences.append(bad_week_text(p5))
    day, day_ret = best_day_info if best_day_info is not None else (None, None)
    warning = single_day_warning(best_share, day, day_ret)
    if warning:
        sentences.append(warning)
    return check_text(" ".join(sentences))


# =========================================================================================
# Confianza en la regla
# =========================================================================================


def _is_green(color: object) -> bool:
    value = getattr(color, "value", color)
    return isinstance(value, str) and value.strip().lower() == "verde"


def confidence(
    backtest_exists: bool,
    mean_excess: float | None,
    bt_color: object,
    live_color: object,
) -> str:
    """Confianza en la regla (tabla 10.2): SIN DATOS, MUY BAJA, BAJA, MEDIA o ALTA.

    Los colores son los del semáforo («verde», «ambar», «rojo», «gris»; vale también un
    ``Enum`` con ese ``value``). Lo dudoso se resuelve en contra: un exceso desconocido
    (``None``, NaN o infinito) cuenta como sin datos."""
    excess = _finite(mean_excess)
    if not backtest_exists or excess is None:
        return "SIN DATOS"
    if excess <= 0:
        return "MUY BAJA"
    if not _is_green(bt_color):
        return "BAJA"
    if not _is_green(live_color):
        return "MEDIA"
    return "ALTA"


def confidence_text(level: str) -> str:
    """«Confianza en la regla: BAJA. En el pasado ganó a BTC de media, pero…»."""
    if level not in CONFIDENCE_TEXT:
        raise ValueError(f"nivel de confianza desconocido: {level!r}")
    return check_text(f"Confianza en la regla: {level}. {CONFIDENCE_TEXT[level]}")


# =========================================================================================
# Reglas de redacción
# =========================================================================================


def _fold(text: str) -> str:
    """Texto para buscar expresiones: minúsculas, sin tildes y con un solo espacio entre
    palabras (así una tilde o un salto de línea no esconden una expresión prohibida)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(plain.split())


def check_text(text: str, *, allowed: Iterable[str] = ALLOWED_DISCLAIMERS) -> str:
    """Devuelve ``text`` si no contiene ninguna expresión prohibida (sin distinguir
    mayúsculas, tildes ni saltos de línea); si la contiene, lanza ``ValueError``. Solo se
    admiten, completos, los avisos fijos de ``allowed``."""
    folded = _fold(text)
    for phrase in allowed:
        target = _fold(phrase)
        if target:
            folded = folded.replace(target, " ")
    folded = " ".join(folded.split())
    for word in FORBIDDEN:
        if _fold(word) in folded:
            raise ValueError(f"expresión prohibida «{word}» en el texto: {text!r}")
    return text

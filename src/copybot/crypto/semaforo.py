"""Semáforo «¿Es rentable?» (apartado 9 del diseño): dos luces y una conclusión.

Función pura: recibe lo que ya midieron el backtest de la versión (curva A, pseudo-fuera de
muestra) y la cartera continua en vivo, y devuelve el color de cada luz, los criterios con
su valor y su umbral en castellano llano, los mensajes fijos y la salida JSON para la
interfaz (``crypto semaforo --json``). No lee la base de datos ni la red.

* Colores: ``verde``, ``ambar``, ``rojo`` y ``gris`` (faltan datos para juzgar).
* **Lo dudoso se resuelve en contra de la estrategia.** Un dato que falta (``None``) o que no
  es un número finito (NaN o infinito) nunca cumple un criterio, así que nunca da el verde.
  Tampoco pone una luz en rojo: el rojo exige un fallo medido.
* Para el verde tienen que cumplirse **todos** los criterios de la luz a la vez (los de datos
  suficientes, los que la pondrían en rojo y los propios del verde): es un contraste de
  intersección-unión y no infla el error de tipo I.
* La luz en vivo solo mira el intervalo en las **revisiones** (cada 28 días, como mucho 13,
  con z = 2,89). Entre dos revisiones se usa el resultado de la última.

Ningún color es una promesa: el aviso ``AVISO`` acompaña siempre al resultado.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

VERDE = "verde"
AMBAR = "ambar"
ROJO = "rojo"
GRIS = "gris"
COLORS = (VERDE, AMBAR, ROJO, GRIS)
COLOR_LABELS = {VERDE: "VERDE", AMBAR: "ÁMBAR", ROJO: "ROJO", GRIS: "GRIS"}

# --- Umbrales de la luz del backtest (apartado 9.1) -----------------------------------------
MIN_YEARS = 3.0  # años evaluables
SMALL_UNIVERSE = 20  # un día con |U| por debajo tiene pocas monedas para elegir
MAX_SHARE_SMALL_UNIVERSE = 0.05  # como mucho el 5 % de los días con |U| < 20
MIN_COVERAGE = 0.98
CI_BLOCKS = (7, 28, 56)  # longitudes de bloque L del bootstrap estacionario
MIN_DSR = 0.95
# B5: el valor final supera el percentil 95 del nulo. Con ``null_percentile`` = % de carteras
# al azar **estrictamente** por debajo, eso es ≥ 95 (lo mismo que dice el informe).
MIN_NULL_PERCENTILE = 95.0
GREEN_MAX_DRAWDOWN = 0.25  # caída máxima para el verde (decisión aprobada); 25-50 %: ámbar
RED_DRAWDOWN = 0.50  # la cartera de 100 $ se habría parado

# --- Umbrales de la luz en vivo (apartado 9.2) ----------------------------------------------
MIN_LIVE_DAYS = 28
MIN_SIGNALS_TRADES = 100  # operaciones cerradas del libro de señales
MIN_PORTFOLIO_TRADES = 30  # operaciones cerradas de la cartera
MAX_EXEC_GAP = 2.0  # brecha de ejecución media, en veces el coste modelado
MIN_AVAILABILITY = 0.99
REVIEW_EVERY_DAYS = 28
MAX_REVIEWS = 13  # alrededor de un año
# Bonferroni: α unilateral de 0,025/13 por revisión → z = Φ⁻¹(1 − 0,025/13) ≈ 2,89.
SEQUENTIAL_Z = 2.89

# --- Textos fijos ---------------------------------------------------------------------------
AVISO = "Un semáforo en verde no garantiza beneficios futuros."
MSG_PEOR_QUE_BTC = "En el pasado, esta regla lo hizo peor que comprar BTC y esperar."
MSG_ELEGIR_NO_APORTA = (
    "Elegir moneda no aporta: lo que funciona es salir a USDT a tiempo. "
    "La versión simple (BTC con filtro) es mejor."
)
MSG_SIMPLE_GANA = "La versión simple gana."
MSG_SIN_VENTAJA = (
    f"Tras {MAX_REVIEWS} revisiones: ventaja no demostrada en un año. La versión sigue "
    "funcionando a modo informativo, pero su luz en vivo ya no puede ponerse verde."
)

SIN_DATOS = "sin datos"
MINUS = "−"  # signo menos tipográfico (−)
_ES_SEPARATORS = str.maketrans({",": ".", ".": ","})  # 1,234.5 → 1.234,5


# --- Entradas -------------------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestEvidence:
    """Lo que midió el backtest de la versión (curva A) para la luz del backtest.

    Los excesos son frente a **BTC comprar y mantener**, anuales y en tanto por uno
    (0,09 = +9 % al año), como ``annualized_excess`` de ``backtest/stats.py``. ``None``
    significa «no disponible» y cuenta en contra de la estrategia.
    """

    years_evaluable: float  # años desde el primer día con |U| ≥ 20
    share_days_small_universe: float  # fracción de días con |U| < 20 (0,03 = 3 %)
    coverage: float  # fracción de velas esperadas que están (0,99 = 99 %)
    delisted_pairs_in_universe: int  # pares retirados después que estuvieron en U(t)
    prereg_hash_ok: bool  # el hash del preregistro coincide con el del código
    excess_annual: float | None  # con los costes normales
    excess_annual_costs_x2: float | None
    excess_annual_delay_1d: float | None  # ejecución con un día de retraso, a O(t+2)
    excess_without_top3: float | None  # sin las 3 monedas que más aportaron
    excess_first_half: float | None
    excess_second_half: float | None
    excess_2023_on: float | None
    # L → límite inferior del intervalo del exceso anual (hacen falta L = 7, 28 y 56).
    ci_low_by_block: dict[int, float]
    dsr: float | None  # Sharpe deflactado con el K registrado
    # 0-100: % de las carteras al azar que acaban **estrictamente** por debajo.
    null_percentile: float | None
    signals_book_mean_excess: float | None  # libro de señales: exceso medio por operación
    signals_book_ci_low: float | None  # límite inferior (L = 28) de ese exceso medio
    max_drawdown: float | None  # caída máxima, 0-1 (0,22 = 22 %)
    archive_imported: bool  # archivo de pares retirados de data.binance.vision
    btc_filter_beats_strategy: bool  # «BTC con el mismo filtro» acaba por encima
    # L → límite superior del mismo intervalo. Opcional: solo sirve para mostrar el margen
    # de error completo («−6 % a +25 %»); el criterio B1 mira los límites inferiores.
    ci_high_by_block: dict[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveEvidence:
    """Lo que midió la cartera continua en vivo de la versión para la luz en vivo.

    **Revisiones.** El intervalo del exceso acumulado solo se mira los días 28, 56, …, 364
    (``review_due``). ``review_number`` cuenta las revisiones hechas, **incluida la de hoy**
    si ``is_review_day``. Los límites inferiores ``cumulative_excess_ci_low_*`` (calculados
    por quien llama con z = 2,89) son los de la **última revisión hecha**: entre dos
    revisiones se pasan los de la última. ``None`` si no se pudieron calcular.
    """

    days: int  # días en vivo de la versión (todos, también los perdidos)
    signals_closed_trades: int
    portfolio_closed_trades: int
    limits_match: bool  # límites de riesgo iguales a los registrados
    halted_50: bool  # ha tocado la parada por caída del 50 %
    fidelity_unexplained_days: int  # días con decisiones distintas de la réplica, sin nota
    mean_exec_gap_over_model: float | None  # brecha de ejecución media / coste modelado
    availability: float | None  # fracción de días con el paso diario hecho (0-1)
    review_number: int  # revisiones hechas (como mucho 13)
    is_review_day: bool  # hoy toca revisión
    cumulative_excess_ci_low_portfolio: float | None  # cartera: exceso anual (0,05 = +5 %)
    cumulative_excess_ci_low_signals: float | None  # libro: exceso por operación (0,004)
    max_drawdown: float | None  # 0-1
    excess_without_top3: float | None  # sin las 3 mejores operaciones
    excess_first_half: float | None
    excess_second_half: float | None
    duplicate_orders: int
    # En alguna revisión, el exceso acumulado quedó por debajo del percentil 5 de los
    # arranques de la misma duración (curva C). Una vez rota, la regla sigue rota.
    below_p5_of_starts: bool
    min_trl_days: int | None  # días necesarios (MinTRL con z = 2,89 y el SR̂ del backtest)


# --- Salida ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Criterion:
    """Un criterio del semáforo, explicado para cualquiera."""

    id: str
    texto: str  # qué se comprueba, en castellano llano
    valor: str  # lo medido, con formato español
    umbral: str  # lo que hace falta
    cumple: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "texto": self.texto,
            "valor": self.valor,
            "umbral": self.umbral,
            "cumple": self.cumple,
        }


@dataclass(frozen=True)
class Luces:
    backtest: str
    en_vivo: str


@dataclass(frozen=True)
class Revision:
    hechas: int
    maximo: int
    proxima: date | None  # día de la próxima revisión; None si ya no quedan


@dataclass(frozen=True)
class Semaforo:
    semaforo: str  # conclusión (apartado 9.3)
    luces: Luces
    version: str  # «cripto-v1:<hash>»
    huella: str  # sha256 de 16 caracteres del informe
    criterios: tuple[Criterion, ...]
    mensajes: tuple[str, ...]
    dias_necesarios: int | None
    revision: Revision
    aviso: str = AVISO

    def as_dict(self) -> dict[str, object]:
        """Diccionario con la forma del JSON del apartado 9.3 (más los mensajes fijos)."""
        proxima = self.revision.proxima
        return {
            "semaforo": self.semaforo,
            "luces": {"backtest": self.luces.backtest, "en_vivo": self.luces.en_vivo},
            "version": self.version,
            "huella": self.huella,
            "criterios": [c.as_dict() for c in self.criterios],
            "mensajes": list(self.mensajes),
            "dias_necesarios": self.dias_necesarios,
            "revision": {
                "hechas": self.revision.hechas,
                "maximo": self.revision.maximo,
                "proxima": None if proxima is None else proxima.isoformat(),
            },
            "aviso": self.aviso,
        }

    def to_json(self) -> str:
        """JSON compacto, con tildes tal cual (``crypto semaforo --json``)."""
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))


# --- Formato y comparaciones prudentes ------------------------------------------------------


def _clean(x: float | None) -> float | None:
    """``None`` también para NaN e infinitos: un dato que no es un número finito cuenta como
    que falta (como en ``backtest/stats.py``, es un error de datos)."""
    return None if x is None or not math.isfinite(x) else x


def _gt(x: float | None, threshold: float) -> bool:
    """``x > threshold``; un dato que falta no cumple (y lo mismo en las demás)."""
    v = _clean(x)
    return v is not None and v > threshold


def _ge(x: float | None, threshold: float) -> bool:
    v = _clean(x)
    return v is not None and v >= threshold


def _lt(x: float | None, threshold: float) -> bool:
    v = _clean(x)
    return v is not None and v < threshold


def _le(x: float | None, threshold: float) -> bool:
    v = _clean(x)
    return v is not None and v <= threshold


def _num(x: float, decimals: int = 0, *, signed: bool = False) -> str:
    """Número en formato español: punto de miles, coma decimal y signo menos tipográfico.

    El signo es el del valor **sin redondear**, para que el texto no contradiga la
    comparación con 0 del criterio: −0,001 con dos decimales es «−0,00», no «0,00».
    """
    body = f"{abs(round(x, decimals)):,.{decimals}f}".translate(_ES_SEPARATORS)
    if x < 0:
        return MINUS + body
    return f"+{body}" if signed and x > 0 else body


def _trim(text: str) -> str:
    """Quita un decimal nulo: «97,0» → «97»."""
    return text.removesuffix(",0")


def _amount(x: float | None, decimals: int = 0, unit: str = "") -> str:
    """Cantidad con formato español y unidad, o «sin datos»."""
    v = _clean(x)
    return SIN_DATOS if v is None else _trim(_num(v, decimals)) + unit


def _pct(x: float | None, *, signed: bool = False, unit: str = " %") -> str:
    """Tanto por uno en porcentaje con un decimal como mucho: 0,092 → «+9,2 %» (con
    ``signed``), 0,09 → «9 %», o «sin datos». Un valor distinto de 0 que con un decimal se
    vería como 0 lleva dos: 0,0004 → «+0,04 %», para no aparentar un 0 exacto."""
    v = _clean(x)
    if v is None:
        return SIN_DATOS
    value = v * 100
    decimals = 2 if 0 < abs(value) < 0.05 else 1
    return _trim(_num(value, decimals, signed=signed)) + unit


def _yearly(x: float | None) -> str:
    """Exceso anual: 0,09 → «+9 %/año»."""
    return _pct(x, signed=True, unit=" %/año")


def _from(text: str, suffix: str = "") -> str:
    """«desde +0,5 %» para un límite inferior medido; «sin datos» si falta."""
    return text if text == SIN_DATOS else f"desde {text}{suffix}"


def _measured(prefix: str, text: str, suffix: str = "") -> str:
    """``prefix + text + suffix`` si el dato está medido; si falta, solo «sin datos» (para
    no escribir «caída máxima del sin datos»)."""
    return text if text == SIN_DATOS else f"{prefix}{text}{suffix}"


def _yes_no(flag: bool) -> str:
    return "sí" if flag else "no"


# --- Calendario de revisiones ---------------------------------------------------------------


def reviews_due(days: int) -> int:
    """Revisiones que tocan con ``days`` días en vivo: una cada 28 días, como mucho 13."""
    return min(max(days, 0) // REVIEW_EVERY_DAYS, MAX_REVIEWS)


def review_due(days: int) -> bool:
    """Hoy (el día en vivo número ``days``) toca revisión: días 28, 56, …, 364."""
    return days > 0 and days % REVIEW_EVERY_DAYS == 0 and days // REVIEW_EVERY_DAYS <= MAX_REVIEWS


def next_review_date(first_day: date, days: int) -> date | None:
    """Fecha de la próxima revisión después del día en vivo número ``days``.

    ``first_day`` es el día en vivo número 1, así que la revisión k cae en
    ``first_day + (28·k − 1)`` días. ``None`` si ya se hicieron las 13.
    """
    k = max(days, 0) // REVIEW_EVERY_DAYS + 1
    if k > MAX_REVIEWS:
        return None
    return first_day + timedelta(days=REVIEW_EVERY_DAYS * k - 1)


def _sequential_ok(live: LiveEvidence) -> bool:
    """El intervalo de la última revisión válida queda por encima de 0 en los dos libros.

    Una revisión solo vale si es una de las 13 y no hay más de las que tocan por los días.
    """
    return (
        1 <= live.review_number <= reviews_due(live.days)
        and _gt(live.cumulative_excess_ci_low_portfolio, 0)
        and _gt(live.cumulative_excess_ci_low_signals, 0)
    )


def reviews_exhausted(live: LiveEvidence) -> bool:
    """Tras la revisión 13 sin el intervalo por encima de 0: la luz en vivo ya no puede ser
    verde («ventaja no demostrada en un año»)."""
    return live.review_number >= MAX_REVIEWS and not _sequential_ok(live)


# --- Luz del backtest (apartado 9.1) --------------------------------------------------------


def _backtest_data_checks(bt: BacktestEvidence) -> list[Criterion]:
    return [
        Criterion(
            "BG1",
            "Años de historia que se pueden evaluar",
            _amount(bt.years_evaluable, 1, " años"),
            f"≥ {_num(MIN_YEARS)} años",
            _ge(bt.years_evaluable, MIN_YEARS),
        ),
        Criterion(
            "BG2",
            f"Días con menos de {SMALL_UNIVERSE} monedas que se puedan evaluar",
            f"{_pct(bt.share_days_small_universe)} de los días",
            f"≤ {_pct(MAX_SHARE_SMALL_UNIVERSE)} de los días",
            _le(bt.share_days_small_universe, MAX_SHARE_SMALL_UNIVERSE),
        ),
        Criterion(
            "BG3",
            "Datos completos (velas que están de las que deberían estar)",
            _pct(bt.coverage),
            f"≥ {_pct(MIN_COVERAGE)}",
            _ge(bt.coverage, MIN_COVERAGE),
        ),
        Criterion(
            "BG4",
            "Incluye monedas que Binance retiró después (no solo las que sobrevivieron)",
            f"{_num(bt.delisted_pairs_in_universe)} pares retirados",
            "al menos 1",
            bt.delisted_pairs_in_universe >= 1,
        ),
        Criterion(
            "BG5",
            "La regla es la registrada antes de ver resultados",
            "el hash coincide" if bt.prereg_hash_ok else "el hash no coincide",
            "el hash coincide",
            bt.prereg_hash_ok,
        ),
    ]


def _backtest_red_checks(bt: BacktestEvidence) -> list[tuple[Criterion, bool]]:
    """Criterios que ponen la luz en rojo, con ``True`` si el fallo está medido."""
    return [
        (
            Criterion(
                "BR1",
                "Gana a BTC con los costes normales",
                _yearly(bt.excess_annual),
                "> 0 (si no, ROJO)",
                _gt(bt.excess_annual, 0),
            ),
            _le(bt.excess_annual, 0),
        ),
        (
            Criterion(
                "BR2",
                "Gana a BTC con el doble de costes",
                _yearly(bt.excess_annual_costs_x2),
                "> 0 (si no, ROJO)",
                _gt(bt.excess_annual_costs_x2, 0),
            ),
            _le(bt.excess_annual_costs_x2, 0),
        ),
        (
            Criterion(
                "BR3",
                "La cartera de 100 $ no se habría parado por una caída del 50 %",
                _measured("caída máxima del ", _pct(bt.max_drawdown)),
                f"< {_pct(RED_DRAWDOWN)} (si no, ROJO)",
                _lt(bt.max_drawdown, RED_DRAWDOWN),
            ),
            _ge(bt.max_drawdown, RED_DRAWDOWN),
        ),
    ]


def _b1(bt: BacktestEvidence) -> Criterion:
    """B1: el límite inferior es > 0 con cada L. El texto muestra el intervalo que abarca
    los tres (el límite inferior más bajo y el superior más alto), como el informe del
    backtest; sin los límites superiores, solo desde dónde empieza."""
    lows = {block: _clean(bt.ci_low_by_block.get(block)) for block in CI_BLOCKS}
    highs = [_clean(bt.ci_high_by_block.get(block)) for block in CI_BLOCKS]
    known = [low for low in lows.values() if low is not None]
    known_highs = [high for high in highs if high is not None]
    missing = [str(block) for block, low in lows.items() if low is None]
    if missing:
        detail = f"falta el intervalo con L = {', '.join(missing)}"
    elif len(known_highs) == len(CI_BLOCKS):
        detail = (
            f"margen de error: {_pct(min(known), signed=True)} a "
            f"{_pct(max(known_highs), signed=True)}"
        )
    else:
        detail = f"margen de error {_from(_pct(min(known), signed=True))}"
    blocks = f"{', '.join(str(b) for b in CI_BLOCKS[:-1])} y {CI_BLOCKS[-1]}"
    return Criterion(
        "B1",
        "Gana a BTC por un margen que no parece suerte",
        f"{_yearly(bt.excess_annual)} ({detail})",
        f"límite inferior > 0 con L = {blocks}",
        not missing and all(low > 0 for low in known),
    )


def _backtest_green_checks(bt: BacktestEvidence) -> list[Criterion]:
    robust = (
        ("sin las 3 monedas que más aportaron", bt.excess_without_top3),
        ("1.ª mitad", bt.excess_first_half),
        ("2.ª mitad", bt.excess_second_half),
        ("2023-hoy", bt.excess_2023_on),
    )
    percentile = _clean(bt.null_percentile)
    null_text = (
        SIN_DATOS
        if percentile is None
        else f"mejor que el {_amount(percentile, 1)} % de las carteras al azar"
    )
    return [
        _b1(bt),
        Criterion(
            "B2",
            "La ventaja resiste haber probado varias versiones (Sharpe deflactado)",
            _amount(bt.dsr, 2),
            f"≥ {_num(MIN_DSR, 2)}",
            _ge(bt.dsr, MIN_DSR),
        ),
        Criterion(
            "B3",
            "Sigue ganando a BTC con el doble de costes y con un día de retraso",
            f"costes ×2: {_yearly(bt.excess_annual_costs_x2)}; "
            f"un día de retraso: {_yearly(bt.excess_annual_delay_1d)}",
            "> 0 en los dos casos",
            _gt(bt.excess_annual_costs_x2, 0) and _gt(bt.excess_annual_delay_1d, 0),
        ),
        Criterion(
            "B4",
            "No depende de 3 monedas ni de una sola época",
            "; ".join(f"{label}: {_yearly(value)}" for label, value in robust),
            "> 0 en los cuatro casos",
            all(_gt(value, 0) for _, value in robust),
        ),
        Criterion(
            "B5",
            "Elegir moneda aporta algo frente a elegir al azar",
            null_text,
            f"≥ {_num(MIN_NULL_PERCENTILE)} % (supera el percentil {_num(MIN_NULL_PERCENTILE)} "
            "del azar)",
            _ge(bt.null_percentile, MIN_NULL_PERCENTILE),
        ),
        Criterion(
            "B6",
            "Libro de señales: las elegidas ganan a BTC en los 7 días siguientes",
            _measured(
                "",
                _pct(bt.signals_book_mean_excess, signed=True),
                f" por operación (margen de error "
                f"{_from(_pct(bt.signals_book_ci_low, signed=True))})",
            ),
            "media > 0 y límite inferior (L = 28) > 0",
            _gt(bt.signals_book_mean_excess, 0) and _gt(bt.signals_book_ci_low, 0),
        ),
        Criterion(
            "B7",
            "La caída máxima es moderada",
            _pct(bt.max_drawdown),
            f"< {_pct(GREEN_MAX_DRAWDOWN)} (entre {_num(GREEN_MAX_DRAWDOWN * 100)} y "
            f"{_pct(RED_DRAWDOWN)}: ÁMBAR)",
            _lt(bt.max_drawdown, GREEN_MAX_DRAWDOWN),
        ),
        Criterion(
            "B8",
            "Incluye el archivo de pares que Binance retiró",
            "importado" if bt.archive_imported else "no importado",
            "importado (sin él, como mucho ÁMBAR)",
            bt.archive_imported,
        ),
    ]


def backtest_light(bt: BacktestEvidence | None) -> tuple[str, list[Criterion], list[str]]:
    """Luz del backtest: ``(color, criterios, mensajes)``.

    GRIS si faltan datos (o no hay backtest); con datos suficientes, ROJO si pierde frente a
    BTC con los costes normales o con el doble, o si la caída llega al 50 %; VERDE si se
    cumplen B1-B8 y nada está en duda; ÁMBAR en el resto. Los mensajes son los fijos del
    apartado 9.1 y la cabecera del informe cuando el pasado no ganó a BTC.
    """
    if bt is None:
        missing = Criterion("BG0", "Hay un backtest de esta versión", "no", "sí", False)
        return GRIS, [missing], []
    data = _backtest_data_checks(bt)
    red = _backtest_red_checks(bt)
    green = _backtest_green_checks(bt)
    criterios = [*data, *(c for c, _ in red), *green]
    if not all(c.cumple for c in data):
        color = GRIS
    elif any(is_red for _, is_red in red):
        color = ROJO
    elif all(c.cumple for c in criterios):
        color = VERDE
    else:
        color = AMBAR

    mensajes = []
    if _le(bt.excess_annual, 0):
        mensajes.append(MSG_PEOR_QUE_BTC)
    by_id = {c.id: c for c in green}
    if (
        all(by_id[i].cumple for i in ("B1", "B2", "B3", "B4"))
        and _clean(bt.null_percentile) is not None
        and not by_id["B5"].cumple
    ):
        mensajes.append(MSG_ELEGIR_NO_APORTA)
    if bt.btc_filter_beats_strategy:
        mensajes.append(MSG_SIMPLE_GANA)
    return color, criterios, mensajes


# --- Luz en vivo (apartado 9.2) -------------------------------------------------------------


def _live_data_checks(live: LiveEvidence) -> list[Criterion]:
    return [
        Criterion(
            "VG1",
            "Días en vivo",
            f"{_num(live.days)} de {MIN_LIVE_DAYS}"
            if live.days < MIN_LIVE_DAYS
            else f"{_num(live.days)} días",
            f"≥ {MIN_LIVE_DAYS} días",
            live.days >= MIN_LIVE_DAYS,
        ),
        Criterion(
            "VG2",
            "Operaciones cerradas del libro de señales",
            _num(live.signals_closed_trades),
            f"≥ {MIN_SIGNALS_TRADES}",
            live.signals_closed_trades >= MIN_SIGNALS_TRADES,
        ),
        Criterion(
            "VG3",
            "Operaciones cerradas de la cartera de prueba",
            _num(live.portfolio_closed_trades),
            f"≥ {MIN_PORTFOLIO_TRADES}",
            live.portfolio_closed_trades >= MIN_PORTFOLIO_TRADES,
        ),
        Criterion(
            "VG4",
            "Los límites de riesgo son los registrados",
            _yes_no(live.limits_match),
            "sí",
            live.limits_match,
        ),
    ]


def _live_red_checks(live: LiveEvidence) -> list[tuple[Criterion, bool]]:
    """Criterios que ponen la luz en rojo, con ``True`` si el fallo está medido."""
    gap = live.mean_exec_gap_over_model
    if live.review_number < 1:
        starts = "aún no hay revisiones"
    elif live.below_p5_of_starts:
        starts = "por debajo: la regla se ha roto"
    else:
        starts = "por encima"
    return [
        (
            Criterion(
                "VR1",
                "La cartera no ha tocado la parada por caída del 50 %",
                "la ha tocado" if live.halted_50 else "no la ha tocado",
                "sin tocarla (si no, ROJO)",
                not live.halted_50,
            ),
            live.halted_50,
        ),
        (
            Criterion(
                "VR2",
                "Decide lo mismo que el backtest sobre los mismos días (fidelidad)",
                f"{_num(live.fidelity_unexplained_days)} días con diferencias sin explicar",
                "0 días (si no, ROJO)",
                live.fidelity_unexplained_days == 0,
            ),
            live.fidelity_unexplained_days > 0,
        ),
        (
            Criterion(
                "VR3",
                "Los precios reales de compra y venta se parecen a los previstos",
                _amount(gap, 1, " veces el coste previsto"),
                f"≤ {_num(MAX_EXEC_GAP)} veces (si no, ROJO)",
                _le(gap, MAX_EXEC_GAP),
            ),
            _gt(gap, MAX_EXEC_GAP),
        ),
        (
            Criterion(
                "VR4",
                "El bot ha hecho su trabajo todos los días (disponibilidad)",
                _pct(live.availability),
                f"≥ {_pct(MIN_AVAILABILITY)} (por debajo, ROJO a partir del día {MIN_LIVE_DAYS})",
                _ge(live.availability, MIN_AVAILABILITY),
            ),
            live.days >= MIN_LIVE_DAYS and _lt(live.availability, MIN_AVAILABILITY),
        ),
        (
            Criterion(
                "VR5",
                "En las revisiones no queda entre el 5 % de peores arranques del pasado de la "
                "misma duración",
                starts,
                "por encima del percentil 5 (si no, ROJO)",
                not live.below_p5_of_starts,
            ),
            live.below_p5_of_starts and live.review_number >= 1,
        ),
    ]


def _sequential_text(live: LiveEvidence) -> str:
    n = live.review_number
    if n < 1:
        return f"aún no ha habido ninguna revisión (la primera, el día {REVIEW_EVERY_DAYS})"
    if n > MAX_REVIEWS:
        return f"revisión {n}: después de la {MAX_REVIEWS} ya no cuentan"
    if n > reviews_due(live.days):
        return f"{n} revisiones con {_num(live.days)} días: más de las que tocan, no cuentan"
    when = "revisión de hoy" if live.is_review_day else "última revisión"
    portfolio = _pct(live.cumulative_excess_ci_low_portfolio, signed=True, unit=" %/año")
    signals = _pct(live.cumulative_excess_ci_low_signals, signed=True)
    return (
        f"{when} ({n} de {MAX_REVIEWS}): cartera {_from(portfolio)}; "
        f"libro de señales {_from(signals, ' por operación')}"
    )


def _live_green_checks(live: LiveEvidence, backtest_color: str) -> list[Criterion]:
    robust = (
        ("sin las 3 mejores operaciones", live.excess_without_top3),
        ("1.ª mitad", live.excess_first_half),
        ("2.ª mitad", live.excess_second_half),
    )
    z = _num(SEQUENTIAL_Z, 2)
    return [
        Criterion(
            "V1",
            "El backtest está en VERDE",
            COLOR_LABELS[backtest_color],
            COLOR_LABELS[VERDE],
            backtest_color == VERDE,
        ),
        Criterion(
            "V2",
            "En vivo gana a BTC por un margen que no parece suerte (cartera y libro de señales)",
            _sequential_text(live),
            f"límite inferior > 0 en los dos (z = {z}; se mira cada {REVIEW_EVERY_DAYS} días, "
            f"como mucho {MAX_REVIEWS} veces)",
            _sequential_ok(live),
        ),
        Criterion(
            "V3",
            "La caída máxima en vivo es moderada",
            _pct(live.max_drawdown),
            f"< {_pct(GREEN_MAX_DRAWDOWN)}",
            _lt(live.max_drawdown, GREEN_MAX_DRAWDOWN),
        ),
        Criterion(
            "V4",
            "En vivo no depende de 3 operaciones ni de una sola época",
            "; ".join(f"{label}: {_pct(value, signed=True)}" for label, value in robust),
            "> 0 en los tres casos",
            all(_gt(value, 0) for _, value in robust),
        ),
        Criterion(
            "V5",
            "Ninguna orden duplicada",
            _num(live.duplicate_orders),
            "0",
            live.duplicate_orders == 0,
        ),
    ]


def _check_color(color: str) -> None:
    if color not in COLORS:
        raise ValueError(f"color desconocido: {color!r} (válidos: {', '.join(COLORS)})")


def live_light(live: LiveEvidence | None, backtest_color: str) -> tuple[str, list[Criterion]]:
    """Luz en vivo: ``(color, criterios)``.

    ROJO (con prioridad sobre el gris) si se ha roto algo medido; GRIS si faltan días,
    operaciones o los límites no son los registrados; VERDE si se cumple todo, incluido el
    backtest en VERDE y el intervalo de la última revisión por encima de 0; ÁMBAR en el
    resto. Tras la revisión 13 sin ese intervalo, ya no puede ser VERDE.
    """
    _check_color(backtest_color)
    if live is None:
        missing = Criterion("VG0", "Hay datos de la cartera de prueba en vivo", "no", "sí", False)
        return GRIS, [missing]
    data = _live_data_checks(live)
    red = _live_red_checks(live)
    green = _live_green_checks(live, backtest_color)
    criterios = [*data, *(c for c, _ in red), *green]
    if any(is_red for _, is_red in red):
        color = ROJO
    elif not all(c.cumple for c in data):
        color = GRIS
    elif all(c.cumple for c in criterios):  # tras 13 revisiones sin verde, V2 no se cumple
        color = VERDE
    else:
        color = AMBAR
    return color, criterios


# --- Conclusión (apartado 9.3) --------------------------------------------------------------


def conclusion(light_bt: str, light_live: str) -> str:
    """ROJO si alguna luz está en rojo; si no, GRIS si alguna está en gris; si no, VERDE si
    las dos lo están; si no, ÁMBAR."""
    _check_color(light_bt)
    _check_color(light_live)
    lights = (light_bt, light_live)
    if ROJO in lights:
        return ROJO
    if GRIS in lights:
        return GRIS
    if lights == (VERDE, VERDE):
        return VERDE
    return AMBAR


def evaluate(
    bt: BacktestEvidence | None,
    live: LiveEvidence | None,
    *,
    version: str,
    huella: str,
    next_review_day: date | None = None,
) -> Semaforo:
    """Semáforo completo: las dos luces, la conclusión, los criterios y los mensajes.

    ``next_review_day`` es la fecha de la próxima revisión (``next_review_date``); se omite
    cuando ya se hicieron las 13. Los días necesarios son el MinTRL que trae ``live``,
    redondeado hacia arriba (``None`` si falta o no es un número finito).
    """
    color_bt, criterios_bt, mensajes = backtest_light(bt)
    color_live, criterios_live = live_light(live, color_bt)
    trl = None if live is None else _clean(live.min_trl_days)
    if live is not None and reviews_exhausted(live):
        mensajes = [*mensajes, MSG_SIN_VENTAJA]
    hechas = 0 if live is None else min(max(live.review_number, 0), MAX_REVIEWS)
    return Semaforo(
        semaforo=conclusion(color_bt, color_live),
        luces=Luces(backtest=color_bt, en_vivo=color_live),
        version=version,
        huella=huella,
        criterios=(*criterios_bt, *criterios_live),
        mensajes=tuple(mensajes),
        dias_necesarios=None if trl is None else math.ceil(trl),  # MinTRL, en días enteros
        revision=Revision(
            hechas=hechas,
            maximo=MAX_REVIEWS,
            proxima=next_review_day if hechas < MAX_REVIEWS else None,
        ),
    )

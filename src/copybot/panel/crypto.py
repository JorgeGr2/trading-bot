"""Estado del bot de cripto para el panel (``/api/estado``). Solo lee la base de datos.

* Cartera de prueba de 100 $ (la continua; si no hay, la prueba activa o la última):
  saldo, resultado con comisiones, aciertos, caída máxima, curva frente a BTC comprado el
  mismo día y registro de órdenes con su motivo y su resultado.
* Análisis del día: decisión, las 10 mejor puntuadas con su «por qué» y el informe.
* Umbrales de la regla (``STRATEGY_V1`` y los límites de riesgo de la cartera) y si la
  nº 1 de hoy los cumple.
* Los seis pasos del día («agentes»), el latido del servicio diario y el semáforo.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.config import RiskLimits
from copybot.crypto import explain as ex
from copybot.crypto import semaforo
from copybot.crypto.daily import NO_STEP, RETRY_EVERY, SERVICE
from copybot.crypto.paper import ACTIVE, MAX_SPREAD_PCT, PORTFOLIO, SIGNALS
from copybot.crypto.strategy import MIN_ORDER_USD, STRATEGY_V1, STRATEGY_VERSION, size_usd
from copybot.crypto.sync import EXCHANGE
from copybot.db.models import (
    CryptoAnalysis,
    CryptoAnalysisRow,
    CryptoBacktestRun,
    CryptoCostSnapshot,
    CryptoPaperBook,
    CryptoPaperDay,
    CryptoPaperEvent,
    CryptoPaperOrder,
    CryptoPaperPosition,
    CryptoPaperRun,
    CryptoPaperSnapshot,
    MarketBar,
    MarketSymbol,
    ServiceHeartbeat,
)
from copybot.panel.common import (
    SIN_DATOS,
    age_seconds,
    agent_state,
    base_of,
    iso,
    max_drawdown,
    num,
    reason_group,
)

VERSION = STRATEGY_VERSION
PARAMS = STRATEGY_V1
MINUS = ex.MINUS
# El paso diario late una vez al día (00:10 UTC, con reintentos hasta las 02:00): sin
# latido en 26 horas, el servicio no está funcionando.
LIVE_MAX_AGE = timedelta(hours=26)
LOG_ROWS = 40
PULSE_DAYS = 30
TOP = 10
RUN_STATUS = {
    "RUNNING": "en marcha",
    "PAUSED": "en pausa",
    "HALTED_DAY": "parada por hoy (pérdida del día)",
    "HALTED_RISK": "parada por caída (solo se reanuda a mano)",
    "STOPPED": "parada a mano",
    "FINISHED": "terminada",
}
PURPOSES = {
    "entrada": "entrada",
    "regimen": "mercado bajista",
    "fuera_universo": "fuera del universo",
    "stop": "stop",
    "tendencia": "pierde la tendencia",
    "puesto": "baja de puesto",
    "retirada": "retirada",
    "fin_prueba": "fin de la prueba",
}
EVENT_TEXT = {
    "inicio": "Arranca la cartera con {inicial} ficticios.",
    "perdido": "Día perdido: el paso llegó tarde y no se operó.",
    "sin_datos": "Día sin datos: no se decidió nada.",
}


@dataclass
class _Book:
    """Lo que hace falta de una cartera de prueba (libro de 100 $)."""

    run: CryptoPaperRun
    cash: Decimal | None = None
    snaps: list[CryptoPaperSnapshot] = field(default_factory=list)
    orders: list[CryptoPaperOrder] = field(default_factory=list)  # las más recientes
    filled: int = 0
    fees: Decimal = Decimal(0)
    positions: list[CryptoPaperPosition] = field(default_factory=list)
    events: list[CryptoPaperEvent] = field(default_factory=list)
    days: list[CryptoPaperDay] = field(default_factory=list)
    signals_closed: int = 0
    signals_wins: int = 0
    signals_filled_today: int = 0
    last_fill: datetime | None = None

    @property
    def initial(self) -> float:
        return float(self.run.initial_usd)

    @property
    def open_positions(self) -> list[CryptoPaperPosition]:
        return [p for p in self.positions if p.status == "open"]

    @property
    def equity(self) -> float | None:
        return float(self.snaps[-1].equity_usd) if self.snaps else None

    @property
    def last_step(self) -> datetime | None:
        done = [d.finished_at for d in self.days if d.finished_at is not None]
        return max(done) if done else None

    def limits(self) -> RiskLimits:
        try:
            return RiskLimits.model_validate(self.run.limits)
        except ValueError:
            return RiskLimits.model_construct()


def pick_run(runs: list[CryptoPaperRun], run_id: int | None = None) -> CryptoPaperRun | None:
    """La cartera que enseña el panel: la pedida; si no, la continua activa de la versión,
    otra activa o la última que hubo."""
    if run_id is not None:
        return next((r for r in runs if r.id == run_id), None)
    active = [r for r in runs if r.status in ACTIVE]
    for r in active:
        if r.kind == "continua" and r.strategy_version == VERSION:
            return r
    if active:
        return max(active, key=lambda r: r.id)
    return max(runs, key=lambda r: r.id) if runs else None


async def _load_book(s: AsyncSession, run: CryptoPaperRun, now: datetime) -> _Book:
    b = _Book(run)
    mine = (CryptoPaperOrder.run_id == run.id, CryptoPaperOrder.book == PORTFOLIO)
    book = await s.get(CryptoPaperBook, (run.id, PORTFOLIO))
    b.cash = None if book is None else book.cash_usd
    b.snaps = list(
        await s.scalars(
            select(CryptoPaperSnapshot)
            .where(CryptoPaperSnapshot.run_id == run.id, CryptoPaperSnapshot.book == PORTFOLIO)
            .order_by(CryptoPaperSnapshot.day)
        )
    )
    b.orders = list(
        await s.scalars(
            select(CryptoPaperOrder)
            .where(*mine)
            .order_by(CryptoPaperOrder.day.desc(), CryptoPaperOrder.id.desc())
            .limit(LOG_ROWS)
        )
    )
    filled, fees = (
        await s.execute(
            select(func.count(), func.coalesce(func.sum(CryptoPaperOrder.fee_usd), 0)).where(
                *mine, CryptoPaperOrder.status == "filled"
            )
        )
    ).one()
    b.filled, b.fees = int(filled), Decimal(fees)
    b.last_fill = await s.scalar(
        select(func.max(CryptoPaperOrder.filled_at)).where(CryptoPaperOrder.run_id == run.id)
    )
    b.positions = list(
        await s.scalars(
            select(CryptoPaperPosition)
            .where(CryptoPaperPosition.run_id == run.id, CryptoPaperPosition.book == PORTFOLIO)
            .order_by(CryptoPaperPosition.id)
        )
    )
    b.events = list(
        await s.scalars(
            select(CryptoPaperEvent)
            .where(CryptoPaperEvent.run_id == run.id)
            .order_by(CryptoPaperEvent.at.desc(), CryptoPaperEvent.id.desc())
            .limit(LOG_ROWS)
        )
    )
    b.days = list(
        await s.scalars(
            select(CryptoPaperDay)
            .where(CryptoPaperDay.run_id == run.id)
            .order_by(CryptoPaperDay.day)
        )
    )
    closed, wins = (
        await s.execute(
            select(
                func.count(),
                func.count().filter(CryptoPaperPosition.realized_pnl_usd > 0),
            ).where(
                CryptoPaperPosition.run_id == run.id,
                CryptoPaperPosition.book == SIGNALS,
                CryptoPaperPosition.status == "closed",
            )
        )
    ).one()
    b.signals_closed, b.signals_wins = int(closed), int(wins)
    start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    b.signals_filled_today = int(
        await s.scalar(
            select(func.count()).where(
                CryptoPaperOrder.run_id == run.id,
                CryptoPaperOrder.book == SIGNALS,
                CryptoPaperOrder.status == "filled",
                CryptoPaperOrder.filled_at >= start,
            )
        )
        or 0
    )
    return b


@dataclass
class _Market:
    bars: int = 0
    last_bar: datetime | None = None
    bars_last_day: int = 0
    pairs: int = 0
    trading: int = 0
    last_sync: datetime | None = None
    cost_photos: int = 0
    cost_last: datetime | None = None
    cost_pairs: int = 0


async def _load_market(s: AsyncSession) -> _Market:
    m = _Market()
    daily = (MarketBar.exchange == EXCHANGE, MarketBar.interval == "1d")
    m.bars, m.last_bar = (
        await s.execute(select(func.count(), func.max(MarketBar.open_time)).where(*daily))
    ).one()
    if m.last_bar is not None:
        m.bars_last_day = int(
            await s.scalar(select(func.count()).where(*daily, MarketBar.open_time == m.last_bar))
            or 0
        )
    pairs, trading, m.last_sync = (
        await s.execute(
            select(
                func.count(),
                func.count().filter(MarketSymbol.status == "TRADING"),
                func.max(MarketSymbol.last_seen_at),
            ).where(MarketSymbol.exchange == EXCHANGE, MarketSymbol.quote == "USDT")
        )
    ).one()
    m.pairs, m.trading = int(pairs), int(trading)
    photos, m.cost_last = (
        await s.execute(
            select(
                func.count(func.distinct(CryptoCostSnapshot.taken_at)),
                func.max(CryptoCostSnapshot.taken_at),
            )
        )
    ).one()
    m.cost_photos = int(photos)
    if m.cost_last is not None:
        m.cost_pairs = int(
            await s.scalar(select(func.count()).where(CryptoCostSnapshot.taken_at == m.cost_last))
            or 0
        )
    m.bars = int(m.bars)
    return m


async def crypto_state(
    sessions: async_sessionmaker[AsyncSession], now: datetime, *, run_id: int | None = None
) -> dict[str, Any]:
    """Todo lo que enseña la pestaña del bot de cripto (JSON)."""
    async with sessions() as s:
        beat = await s.get(ServiceHeartbeat, SERVICE)
        runs = list(await s.scalars(select(CryptoPaperRun).order_by(CryptoPaperRun.id.desc())))
        run = pick_run(runs, run_id)
        book = None if run is None else await _load_book(s, run, now)
        analysis = await s.scalar(
            select(CryptoAnalysis)
            .where(CryptoAnalysis.strategy_version == VERSION)
            .order_by(CryptoAnalysis.day.desc())
            .limit(1)
        )
        rows = (
            []
            if analysis is None
            else list(
                await s.scalars(
                    select(CryptoAnalysisRow).where(CryptoAnalysisRow.analysis_id == analysis.id)
                )
            )
        )
        bt = await s.scalar(
            select(CryptoBacktestRun)
            .where(
                CryptoBacktestRun.strategy_version == VERSION,
                CryptoBacktestRun.role == "principal",
            )
            .order_by(CryptoBacktestRun.id.desc())
            .limit(1)
        )
        market = await _load_market(s)
        first_pulse = now.date() - timedelta(days=PULSE_DAYS)
        analyses = list(
            (
                await s.execute(
                    select(
                        CryptoAnalysis.day, CryptoAnalysis.computed_live, CryptoAnalysis.status
                    ).where(
                        CryptoAnalysis.strategy_version == VERSION,
                        CryptoAnalysis.day >= first_pulse,
                    )
                )
            ).all()
        )

    ranked = sorted((r for r in rows if r.rank is not None), key=lambda r: r.rank or 0)
    top = ranked[0] if ranked else None
    size = _order_size(book)
    heartbeat = _heartbeat(beat, now)
    return {
        "generado": iso(now),
        "version": VERSION,
        "aviso": "Solo simulación: dinero ficticio, sin claves ni órdenes reales.",
        "latido": heartbeat,
        "carteras": [
            {"id": r.id, "tipo": r.kind, "estado": r.status, "desde": iso(r.start_day)}
            for r in runs
        ],
        "cartera": None if book is None else _run_info(book),
        "tarjetas": _cards(book, bt, analysis),
        "curva": [] if book is None else _curve(book),
        "actividad": [] if book is None else _activity(book),
        "hoy": None if analysis is None else _today(analysis, rows, ranked, book, size),
        "umbrales": _thresholds(top, analysis, book, size),
        "agentes": _agents(now, heartbeat, market, analysis, rows, ranked, book),
        "pulso": _pulse(now.date(), book, analyses),
        "backtest": None if bt is None else _backtest(bt),
        "mercado": {
            "velas": market.bars,
            "ultima_vela": iso(market.last_bar),
            "pares_usdt": market.pairs,
            "en_trading": market.trading,
            "ultima_descarga": iso(market.last_sync),
            "fotos_coste": market.cost_photos,
            "ultima_foto_coste": iso(market.cost_last),
        },
    }


# --- Bloques -------------------------------------------------------------------------------


def _heartbeat(beat: ServiceHeartbeat | None, now: datetime) -> dict[str, Any]:
    age = age_seconds(None if beat is None else beat.beat_at, now)
    details = {} if beat is None else (beat.details or {})
    return {
        "servicio": SERVICE,
        "ultimo": None if beat is None else iso(beat.beat_at),
        "edad_s": age,
        "en_vivo": age is not None and age <= LIVE_MAX_AGE.total_seconds(),
        "max_edad_h": LIVE_MAX_AGE.total_seconds() / 3600,
        "estado": details.get("estado"),
        "dia": details.get("dia"),
    }


def _run_info(b: _Book) -> dict[str, Any]:
    r = b.run
    return {
        "id": r.id,
        "tipo": r.kind,
        "estado": r.status,
        "estado_texto": RUN_STATUS.get(r.status, r.status),
        "motivo": r.status_reason,
        "desde": iso(r.start_day),
        "hasta": iso(r.end_day),
        "inicial": num(r.initial_usd, 2),
        "efectivo": num(b.cash, 2),
        "veredicto": r.verdict,
        "posiciones": [
            {
                "moneda": base_of(p.symbol),
                "desde": iso(p.opened_day),
                "coste": num(p.cost_usd, 2),
                "valor": num(p.last_value_usd, 2),
                "resultado": None
                if p.last_value_usd is None
                else num(p.last_value_usd - p.cost_usd, 2),
                "invendible": p.unsellable,
            }
            for p in b.open_positions
        ],
    }


def _light(value: object) -> str | None:
    return value if isinstance(value, str) and value in semaforo.COLORS else None


def _live_days(b: _Book | None) -> int:
    if b is None or not b.days:
        return 0
    return (b.days[-1].day - b.run.start_day).days + 1


def _cards(
    b: _Book | None, bt: CryptoBacktestRun | None, analysis: CryptoAnalysis | None
) -> dict[str, Any]:
    metrics = {} if bt is None else (bt.metrics or {})
    light_bt = semaforo.GRIS if bt is None else _light(metrics.get("luz"))
    continua = b is not None and b.run.kind == "continua"
    days = _live_days(b) if continua else 0
    # La luz en vivo no puede pasar de gris hasta tener 28 días de la cartera continua; a
    # partir de ahí la calcula el semáforo completo, que el panel no recalcula.
    light_live = semaforo.GRIS if days < semaforo.MIN_LIVE_DAYS else None
    if light_bt is not None and light_live is not None:
        overall = semaforo.conclusion(light_bt, light_live)
    else:
        overall = semaforo.ROJO if light_bt == semaforo.ROJO else None
    sem = {
        "conclusion": overall,
        "backtest": light_bt,
        "en_vivo": light_live,
        "dias_en_vivo": days,
        "dias_necesarios": semaforo.MIN_LIVE_DAYS,
        "hay_backtest": bt is not None,
        "exceso_anual": num(metrics.get("exceso_anual")),
        "aviso": semaforo.AVISO,
    }
    if b is None or not b.snaps:
        return {
            "saldo": None,
            "resultado": None,
            "aciertos": None,
            "caida": None,
            "semaforo": sem,
            "inicial": None if b is None else b.initial,
        }
    equity = b.equity or 0.0
    closed = [p for p in b.positions if p.status == "closed" and p.realized_pnl_usd is not None]
    wins = sum(1 for p in closed if p.realized_pnl_usd > 0)
    curve = [(float(x.equity_usd), float(x.worst_equity_usd)) for x in b.snaps]
    peak = max([b.initial, *(c for c, _ in curve)])
    return {
        "inicial": b.initial,
        "saldo": {"valor": num(equity, 2), "dia": iso(b.snaps[-1].day)},
        "resultado": {
            "usd": num(equity - b.initial, 2),
            "pct": num(equity / b.initial - 1) if b.initial else None,
            "comisiones": num(b.fees, 4),
            "operaciones": b.filled,
        },
        "aciertos": {
            "ganadas": wins,
            "perdidas": len(closed) - wins,
            "abiertas": len(b.open_positions),
            "senales_cerradas": b.signals_closed,
            "senales_ganadas": b.signals_wins,
        },
        "caida": {
            "maxima": num(max_drawdown(b.initial, curve)),
            "actual": num(1 - equity / peak) if peak else None,
            "parada": num(float(b.limits().max_drawdown_pct) / 100),
        },
        "semaforo": sem,
    }


def _curve(b: _Book) -> list[dict[str, Any]]:
    return [
        {
            "dia": iso(x.day),
            "valor": num(x.equity_usd, 4),
            "btc": num(x.btc_benchmark_usd, 4),
            "peor": num(x.worst_equity_usd, 4),
            "efectivo": num(x.cash_usd, 4),
            "posiciones": x.open_positions,
        }
        for x in b.snaps
    ]


def _when(day: date, at: datetime | None) -> datetime:
    """Momento de una fila del registro: su hora, o el final de su vela si no la tiene."""
    if at is not None:
        return at
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)


def _activity(b: _Book) -> list[dict[str, Any]]:
    opened = {(p.symbol, p.opened_day): p for p in b.positions}
    closed = {(p.symbol, p.closed_day): p for p in b.positions if p.closed_day is not None}
    out: list[tuple[datetime, dict[str, Any]]] = []
    for o in b.orders:
        filled = o.status == "filled"
        gross = None if not filled or o.qty is None or o.price is None else o.qty * o.price
        result = None
        if o.side == "sell" and filled:
            p = closed.get((o.symbol, o.day))
            if p is not None and p.realized_pnl_usd is not None:
                pnl = p.realized_pnl_usd
                result = {
                    "usd": num(pnl, 2),
                    "pct": num(pnl / p.cost_usd) if p.cost_usd else None,
                    "gana": pnl > 0,
                    "abierta": False,
                }
        elif o.side == "buy" and filled:
            p = opened.get((o.symbol, o.day))
            if p is not None and p.status == "open" and p.last_value_usd is not None:
                pnl = p.last_value_usd - p.cost_usd
                result = {
                    "usd": num(pnl, 2),
                    "pct": num(pnl / p.cost_usd) if p.cost_usd else None,
                    "gana": pnl > 0,
                    "abierta": True,
                }
            elif p is not None and p.realized_pnl_usd is not None and p.status == "closed":
                pnl = p.realized_pnl_usd
                result = {
                    "usd": num(pnl, 2),
                    "pct": num(pnl / p.cost_usd) if p.cost_usd else None,
                    "gana": pnl > 0,
                    "abierta": False,
                }
        when = _when(o.day, o.filled_at)
        out.append(
            (
                when,
                {
                    "tipo": "orden",
                    "cuando": iso(when),
                    "dia": iso(o.day),
                    "lado": "compra" if o.side == "buy" else "venta",
                    "moneda": base_of(o.symbol),
                    "usd": num(gross if gross is not None else o.requested_usd, 2),
                    "comision": num(o.fee_usd, 4),
                    "precio": num(o.price, 8),
                    "motivo": o.reason or PURPOSES.get(o.purpose, o.purpose),
                    "proposito": PURPOSES.get(o.purpose, o.purpose),
                    "estado": o.status,
                    "resultado": result,
                },
            )
        )
    for e in b.events:
        text = _event_text(e, b)
        if text is None:
            continue
        out.append((e.at, {"tipo": "evento", "cuando": iso(e.at), "evento": e.kind, "texto": text}))
    out.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in out[:LOG_ROWS]]


def _event_text(e: CryptoPaperEvent, b: _Book) -> str | None:
    d = e.details or {}
    if e.kind == "delist":
        return None  # ya sale como venta «retirada»
    if e.kind == "inicio":
        return EVENT_TEXT["inicio"].format(inicial=ex.fmt_usd(b.initial))
    if e.kind == "bloqueo":
        blocked = d.get("bloqueadas") or {}
        names = ", ".join(f"{base_of(sym)} ({why})" for sym, why in sorted(blocked.items()))
        return f"No se pudo comprar: {names}." if names else None
    if e.kind == "fin":
        return f"Fin de la prueba: valor final {d.get('valor_final', '—')} $."
    if e.kind in EVENT_TEXT:
        return EVENT_TEXT[e.kind]
    motive = d.get("motivo")
    if e.kind == "parada":
        return f"Parada de riesgo: {motive}." if motive else "Parada de riesgo."
    return f"{e.kind}: {motive}" if motive else e.kind


def _order_size(b: _Book | None) -> float:
    """Tamaño de cada compra hoy (el de la regla con el último valor de la cartera)."""
    limits = RiskLimits.model_construct() if b is None else b.limits()
    equity = float(limits.virtual_capital_usd) if b is None or b.equity is None else b.equity
    return size_usd(equity, PARAMS, limits, "dolares")


def _medias(row: CryptoAnalysisRow) -> dict[int, bool]:
    medias = (row.components or {}).get("medias") or {}
    return {int(k): bool(v) for k, v in medias.items()}


def _trend(row: CryptoAnalysisRow | None) -> float | None:
    if row is None:
        return None
    value = (row.components or {}).get("tendencia")
    return None if value is None else float(value)


def exclusion_label(reason: str | None) -> str:
    """Motivo de exclusión del universo agrupado para contarlo («poco volumen», …)."""
    u = PARAMS.universe
    if not reason:
        return "sin negociación ese día"
    if reason.startswith("solo lleva"):
        return f"lleva menos de {u.min_age_days} días en Binance"
    if reason.startswith("poco volumen"):
        return f"poco volumen (menos de {ex.fmt_musd(u.min_median_quote_volume)} al día)"
    if reason.startswith("fuera de las"):
        return f"fuera de las {u.max_rank_by_liquidity} más líquidas"
    if reason.startswith("precio que se multiplica"):
        return "salto de precio raro (posible cambio de denominación)"
    return reason_group(reason)


def _why(row: CryptoAnalysisRow, size: float) -> str:
    return ex.why_text(
        base_of(row.symbol),
        ret_30d=num(row.ret_30d),
        momentum_pct=None,
        above_sma=_medias(row),
        median_qv=row.median_qv_30d or Decimal(0),
        order_usd=size if size > 0 else float(MIN_ORDER_USD),
        best_share=num(row.best_day_share),
        p5=num(row.p5_week),
    )


def _today(
    a: CryptoAnalysis,
    rows: list[CryptoAnalysisRow],
    ranked: list[CryptoAnalysisRow],
    b: _Book | None,
    size: float,
) -> dict[str, Any]:
    regime = a.regime or {}
    favorable = regime.get("favorable")
    held = {} if b is None else {p.symbol: p for p in b.open_positions}
    eligible = [r for r in ranked if r.entry_eligible]
    if a.status != "ok":
        decision = "Datos incompletos: hoy la regla no decide nada."
    elif favorable is None:
        decision = "Sin vela de BTC: hoy la regla no decide nada."
    elif not favorable:
        decision = "Hoy ninguna: quédate en USDT. BTC cerró por debajo de su media de 100 días."
    elif eligible:
        best = eligible[0]
        decision = (
            f"La mejor puntuada hoy según la regla: {base_of(best.symbol)} "
            f"({ex.fmt_points(num(best.score))} de 100)."
        )
    else:
        decision = "Hoy ninguna de las primeras cumple la tendencia: quédate en USDT."
    orders = [] if b is None else [o for o in b.orders if o.day == a.day and o.status == "filled"]
    day_row = None if b is None else next((d for d in b.days if d.day == a.day), None)
    excluded: Counter[str] = Counter()
    for r in rows:
        if not r.included:
            excluded[exclusion_label(r.exclusion_reason)] += 1
        elif r.rank is None:
            excluded["sin datos suficientes para puntuarla"] += 1
    return {
        "dia": iso(a.day),
        "en_vivo": a.computed_live,
        "estado": a.status,
        "confianza": a.confidence,
        "evaluadas": a.universe_size,
        "pares": len(rows),
        "mercado": {
            "favorable": favorable,
            "btc": num(regime.get("btc"), 2),
            "media100": num(regime.get("sma100"), 2),
        },
        "decision": decision,
        "tiene": [base_of(sym) for sym in sorted(held)],
        "efectivo": None if b is None else num(b.cash, 2),
        "compras": [
            {"moneda": base_of(o.symbol), "usd": num(o.qty * o.price, 2)}
            for o in orders
            if o.side == "buy" and o.qty is not None and o.price is not None
        ],
        "ventas": [
            {"moneda": base_of(o.symbol), "motivo": o.reason} for o in orders if o.side == "sell"
        ],
        "nota": None if day_row is None else NO_STEP.get(day_row.status),
        "tamano_compra": None if b is None else num(size, 2),
        "mejores": [
            {
                "puesto": r.rank,
                "moneda": base_of(r.symbol),
                "puntos": num(r.score, 2),
                "tendencia": _trend(r),
                "ret_7d": num(r.ret_7d),
                "ret_30d": num(r.ret_30d),
                "ret_90d": num(r.ret_90d),
                "vol": num(r.vol_daily),
                "volumen": num(r.median_qv_30d, 0),
                "riesgo": r.risk_level,
                "compra": bool(r.entry_eligible),
                "en_cartera": r.symbol in held,
                "bloqueo": r.live_block_reason,
                "avisos": r.warnings or [],
                "por_que": _why(r, size),
            }
            for r in ranked[:TOP]
        ],
        "excluidas": [{"motivo": k, "n": n} for k, n in excluded.most_common(6)],
        "excluidas_total": sum(excluded.values()),
        "informe": a.report_md,
    }


def _check(ok: bool | None) -> bool | None:
    return None if ok is None else bool(ok)


def _thresholds(
    top: CryptoAnalysisRow | None,
    a: CryptoAnalysis | None,
    b: _Book | None,
    size: float,
) -> dict[str, Any]:
    """Cada número en el que cree la regla, con lo que mide hoy la nº 1 y si lo cumple
    (``None``: no aplica o no hay dato)."""
    u, sig, p = PARAMS.universe, PARAMS.signals, PARAMS
    limits = RiskLimits.model_construct() if b is None else b.limits()
    trend = _trend(top)
    rank = None if top is None else top.rank
    inside = None if top is None else top.included
    qv = None if top is None else num(top.median_qv_30d)
    vol = None if top is None else num(top.vol_daily)
    regime = {} if a is None else (a.regime or {})
    favorable = regime.get("favorable")
    held = None
    if top is not None and b is not None:
        held = next((x for x in b.open_positions if x.symbol == top.symbol), None)
    drop = None
    if held is not None and held.last_value_usd is not None and held.qty and held.entry_price:
        drop = float(held.last_value_usd / held.qty / held.entry_price) - 1
    curve = [] if b is None else b.snaps
    day_loss = dd_now = None
    if curve:
        prev = float(curve[-2].equity_usd) if len(curve) > 1 else b.initial
        worst = float(curve[-1].worst_equity_usd)
        peak = max([b.initial, *(float(x.equity_usd) for x in curve[:-1])])
        day_loss = max(0.0, 1 - worst / prev) if prev else None
        dd_now = max(0.0, 1 - worst / peak) if peak else None
    spread = None if top is None or top.spread_pct is None else float(top.spread_pct)
    n_open = None if b is None else len(b.open_positions)
    reserve = float(limits.virtual_capital_usd) * float(limits.min_cash_reserve_pct) / 100
    cash = None if b is None or b.cash is None else float(b.cash)
    in_universe = "está en el universo" if inside else ("fuera del universo" if top else None)

    def line(name, text, value, measured, ok, scope="moneda"):
        return {
            "nombre": name,
            "texto": text,
            "valor": value,
            "medido": measured,
            "cumple": _check(ok),
            "ambito": scope,
        }

    groups = [
        {
            "grupo": "universo",
            "titulo": "Qué monedas se pueden evaluar",
            "lineas": [
                line(
                    "min_age_days",
                    "días mínimos cotizando en Binance",
                    f"{u.min_age_days} días",
                    in_universe,
                    inside,
                ),
                line(
                    "min_median_quote_volume",
                    f"volumen mínimo (mediana de {u.liquidity_days} días)",
                    ex.fmt_musd(u.min_median_quote_volume),
                    None if qv is None else ex.fmt_musd(qv),
                    None if qv is None else qv >= u.min_median_quote_volume,
                ),
                line(
                    "max_rank_by_liquidity",
                    "entre las más líquidas",
                    f"las {u.max_rank_by_liquidity} primeras",
                    None
                    if top is None or top.liquidity_rank is None
                    else f"puesto {top.liquidity_rank}",
                    None
                    if top is None or top.liquidity_rank is None
                    else top.liquidity_rank <= u.max_rank_by_liquidity,
                ),
                line(
                    "quality_days",
                    "días seguidos sin huecos",
                    f"{u.quality_days} días",
                    in_universe,
                    inside,
                ),
                line(
                    "max_jump",
                    "salto máximo de un día (redenominación o error)",
                    f"×{ex.fmt_num(u.max_jump)}",
                    in_universe,
                    inside,
                ),
                line(
                    "min_daily_vol",
                    "movimiento diario mínimo (no es estable)",
                    ex.fmt_pct(u.min_daily_vol, 1, sign=False),
                    None if vol is None else ex.fmt_vol(vol),
                    inside,
                ),
            ],
        },
        {
            "grupo": "senales",
            "titulo": "Cómo puntúa",
            "lineas": [
                line(
                    "horizons",
                    "horizontes del momentum (mismo peso)",
                    " · ".join(f"{h} d" for h in sig.horizons),
                    None if top is None else f"{ex.fmt_points(num(top.score))} puntos",
                    None,
                ),
                line("skip_days", "días que se saltan", f"{sig.skip_days} día", None, None),
                line(
                    "vol_floor",
                    "suelo de la volatilidad",
                    ex.fmt_pct(sig.vol_floor, 0, sign=False),
                    None if vol is None else ex.fmt_vol(vol),
                    None,
                ),
                line(
                    "sma_days",
                    "medias de la tendencia",
                    " · ".join(f"{d} d" for d in sig.sma_days),
                    None if trend is None else f"por encima de {ex.fmt_trend(trend)}",
                    None,
                ),
            ],
        },
        {
            "grupo": "regla",
            "titulo": "Cuándo compra y cuándo vende",
            "lineas": [
                line(
                    "market_sma",
                    "filtro de mercado: BTC por encima de su media",
                    f"{p.market_sma} días",
                    None
                    if favorable is None
                    else f"BTC {ex.fmt_usd(regime.get('btc'), 0)} / media "
                    f"{ex.fmt_usd(regime.get('sma100'), 0)}",
                    favorable,
                    "mercado",
                ),
                line(
                    "entry_rank",
                    "compra si está entre las primeras",
                    f"puesto ≤ {p.entry_rank}",
                    None if rank is None else f"puesto {rank}",
                    None if rank is None else rank <= p.entry_rank,
                ),
                line(
                    "entry_trend",
                    "tendencia para comprar",
                    f"≥ {ex.fmt_trend(p.entry_trend)}",
                    None if trend is None else ex.fmt_trend(trend),
                    None if trend is None else trend + 1e-9 >= p.entry_trend,
                ),
                line(
                    "exit_rank",
                    "vende si baja del puesto",
                    f"puesto > {p.exit_rank}",
                    None if rank is None else f"puesto {rank}",
                    None if rank is None else rank <= p.exit_rank,
                ),
                line(
                    "exit_trend",
                    "vende si pierde la tendencia",
                    f"≤ {ex.fmt_trend(p.exit_trend)}",
                    None if trend is None else ex.fmt_trend(trend),
                    None if trend is None else trend > p.exit_trend + 1e-9,
                ),
                line(
                    "stop_loss",
                    "stop de emergencia desde la compra",
                    f"{MINUS}{ex.fmt_pct(p.stop_loss, 0, sign=False)}",
                    "no está en cartera"
                    if held is None and top is not None
                    else (None if drop is None else ex.fmt_pct(drop)),
                    None if drop is None else drop > -p.stop_loss,
                ),
                line(
                    "n_positions",
                    "posiciones como mucho",
                    f"{min(p.n_positions, limits.max_open_positions)}",
                    None if n_open is None else f"{n_open} abiertas",
                    None
                    if n_open is None
                    else n_open < min(p.n_positions, limits.max_open_positions),
                    "cartera",
                ),
                line(
                    "min_cash_reserve_pct",
                    "efectivo que no se invierte",
                    ex.fmt_usd(reserve),
                    None if cash is None else f"{ex.fmt_usd(cash)} en USDT",
                    None if cash is None else cash >= reserve,
                    "cartera",
                ),
                line(
                    "MIN_ORDER_USD",
                    "compra mínima",
                    ex.fmt_usd(MIN_ORDER_USD, 0),
                    None if b is None else f"{ex.fmt_usd(size)} por compra",
                    None if b is None else size >= MIN_ORDER_USD,
                    "cartera",
                ),
            ],
        },
        {
            "grupo": "riesgo",
            "titulo": "Límites de riesgo de la cartera de 100 $",
            "lineas": [
                line(
                    "max_position_usd",
                    "tope por posición",
                    ex.fmt_usd(limits.max_position_usd, 0),
                    None if b is None else f"{ex.fmt_usd(size)} por compra",
                    None if b is None else size <= float(limits.max_position_usd),
                    "cartera",
                ),
                line(
                    "max_daily_loss_pct",
                    "pérdida del día que para las compras",
                    ex.fmt_pct(float(limits.max_daily_loss_pct) / 100, 0, sign=False),
                    None if day_loss is None else ex.fmt_pct(day_loss, 1, sign=False),
                    None if day_loss is None else day_loss < float(limits.max_daily_loss_pct) / 100,
                    "cartera",
                ),
                line(
                    "max_drawdown_pct",
                    "caída desde el máximo que para la cartera",
                    ex.fmt_pct(float(limits.max_drawdown_pct) / 100, 0, sign=False),
                    None if dd_now is None else ex.fmt_pct(dd_now, 1, sign=False),
                    None if dd_now is None else dd_now < float(limits.max_drawdown_pct) / 100,
                    "cartera",
                ),
                line(
                    "MAX_SPREAD_PCT",
                    "diferencial máximo para comprar (solo en vivo)",
                    ex.fmt_pct(MAX_SPREAD_PCT / 100, 1, sign=False),
                    None if spread is None else ex.fmt_pct(spread / 100, 2, sign=False),
                    None if spread is None else spread <= float(MAX_SPREAD_PCT),
                ),
            ],
        },
    ]
    return {
        "moneda": None if top is None else base_of(top.symbol),
        "puesto": rank,
        "dia": None if a is None else iso(a.day),
        "grupos": groups,
    }


def _agents(
    now: datetime,
    heartbeat: dict[str, Any],
    m: _Market,
    a: CryptoAnalysis | None,
    rows: list[CryptoAnalysisRow],
    ranked: list[CryptoAnalysisRow],
    b: _Book | None,
) -> list[dict[str, Any]]:
    """Los seis pasos reales del paso diario, con su estado y una línea."""
    age = heartbeat.get("edad_s")
    retrying = (
        heartbeat.get("estado") == "reintentar"
        and age is not None
        and age <= (RETRY_EVERY + timedelta(minutes=5)).total_seconds()
    )
    analysed = None if a is None else a.created_at
    out = []

    def agent(code, step, name, last, line, detail, running=False):
        out.append(
            {
                "id": code,
                "paso": step,
                "nombre": name,
                "estado": agent_state(last, now, running=running),
                "ultima": iso(last),
                "linea": line,
                "detalle": detail,
            }
        )

    if m.bars:
        line = f"{ex.fmt_num(m.bars)} velas · {m.trading} pares en TRADING"
        detail = (
            f"última vela: {ex.fmt_date_short(m.last_bar, year=True)} ({m.bars_last_day} pares)"
        )
    else:
        line, detail = SIN_DATOS, "aún no se ha descargado nada de Binance"
    agent("01", "DESCARGA", "VELA", m.last_sync, line, detail, retrying)

    if a is not None:
        out_of = sum(1 for r in rows if not r.included)
        reasons = Counter(exclusion_label(r.exclusion_reason) for r in rows if not r.included)
        main = reasons.most_common(1)
        line = f"{a.universe_size} evaluables hoy · {out_of} fuera"
        detail = f"motivo más común: {main[0][0]} ({main[0][1]})" if main else "ninguna fuera"
    else:
        line, detail = SIN_DATOS, "aún no hay análisis del día"
    agent("02", "FILTRO", "CRIBA", analysed, line, detail, retrying)

    if ranked:
        first = ranked[0]
        line = f"nº 1: {base_of(first.symbol)} ({ex.fmt_points(num(first.score))} puntos)"
        detail = f"{len(ranked)} monedas puntuadas el {ex.fmt_date_short(a.day, year=True)}"
    else:
        line, detail = SIN_DATOS, "aún no hay ranking"
    agent("03", "PUNTÚA", "NOTA", analysed, line, detail, retrying)

    regime = {} if a is None else (a.regime or {})
    favorable = regime.get("favorable")
    market_text = (
        "mercado sin datos"
        if favorable is None
        else ("mercado favorable" if favorable else "mercado bajista: todo a USDT")
    )
    if b is not None and b.snaps:
        curve = [(float(x.equity_usd), float(x.worst_equity_usd)) for x in b.snaps]
        dd = max_drawdown(b.initial, curve) or 0.0
        line = f"{market_text} · caída máx. {ex.fmt_pct(dd, 1, sign=False)}"
        status = RUN_STATUS.get(b.run.status, b.run.status)
        detail = f"cartera {status}" + (f": {b.run.status_reason}" if b.run.status_reason else "")
    elif a is not None:
        line, detail = market_text, "sin cartera de prueba todavía"
    else:
        line, detail = SIN_DATOS, "sin análisis ni cartera"
    agent("04", "RIESGO", "FRENO", None if b is None else b.last_step, line, detail)

    if b is not None:
        start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        today = [
            o
            for o in b.orders
            if o.status == "filled" and o.filled_at is not None and o.filled_at >= start
        ]
        buys = sum(1 for o in today if o.side == "buy")
        sells = len(today) - buys
        line = (
            f"hoy: {buys} compra{'s' if buys != 1 else ''}, "
            f"{sells} venta{'s' if sells != 1 else ''}"
            if today
            else "hoy ninguna orden"
        )
        last = b.last_fill
        detail = f"libro de señales hoy: {b.signals_filled_today}" + (
            "" if last is None else f" · última: {ex.fmt_date_short(last, year=True)}"
        )
        if m.cost_last is not None:
            detail += f" · {m.cost_photos} fotos del libro"
        agent("05", "EJECUTA", "CHISPA", last, line, detail)
    else:
        agent("05", "EJECUTA", "CHISPA", None, SIN_DATOS, "sin cartera de prueba todavía")

    if b is not None and b.snaps:
        n = len(b.open_positions)
        line = (
            f"{n} posici{'ones' if n != 1 else 'ón'} · {ex.fmt_usd(b.cash)} en USDT"
            if b.cash is not None
            else f"{n} posiciones"
        )
        detail = f"valor {ex.fmt_usd(b.equity)} el {ex.fmt_date_short(b.snaps[-1].day)}"
    elif b is not None:
        line, detail = SIN_DATOS, "la cartera aún no ha dado ningún paso"
    else:
        line, detail = SIN_DATOS, "sin cartera de prueba todavía"
    agent("06", "LIBRO", "TOMO", None if b is None else b.last_step, line, detail)
    return out


def _pulse(today: date, b: _Book | None, analyses: list) -> list[dict[str, Any]]:
    """Un latido por día: el paso de la cartera (ok, perdido, sin datos) o, si no hay
    cartera, si hubo análisis en vivo."""
    steps = {} if b is None else {d.day: d.status for d in b.days}
    seen = {row.day: (row.computed_live, row.status) for row in analyses}
    out = []
    for k in range(PULSE_DAYS, 0, -1):
        day = today - timedelta(days=k)
        live, status = seen.get(day, (None, None))
        out.append(
            {
                "dia": iso(day),
                "paso": steps.get(day),
                "analisis": None if live is None else ("en_vivo" if live else "recalculado"),
                "completo": None if status is None else status == "ok",
            }
        )
    return out


def _backtest(bt: CryptoBacktestRun) -> dict[str, Any]:
    m = bt.metrics or {}
    ci = m.get("margen") or [None, None]
    periods = []
    for row in m.get("periodos") or []:
        if len(row) >= 5:
            periods.append(
                {
                    "nombre": row[0],
                    "regla": num(row[1]),
                    "btc": num(row[2]),
                    "btc_filtro": num(row[3]),
                    "cesta10": num(row[4]),
                }
            )
    return {
        "fecha": iso(bt.created_at),
        "desde": iso(bt.period_from),
        "hasta": iso(bt.period_to),
        "dias": bt.n_days,
        "luz": _light(m.get("luz")),
        "exceso_anual": num(m.get("exceso_anual")),
        "margen": [num(ci[0]), num(ci[1])] if len(ci) == 2 else [None, None],
        "nulo_percentil": num(m.get("nulo_percentil")),
        "dsr": num(m.get("dsr")),
        "k": m.get("k"),
        "final": num(m.get("final"), 2),
        "periodos": periods,
    }

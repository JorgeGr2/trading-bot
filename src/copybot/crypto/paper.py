"""Cartera de prueba en vivo (simulada) que sigue la regla v1 cada día.

* La **continua** empieza al registrar la versión y no se reinicia nunca: es la evidencia
  de la luz en vivo. Las **pruebas** las lanzas tú, con la duración que elijas.
* Cada paso diario usa el **mismo** ``decide()`` que el backtest con la vela de ``t``
  cerrada, y ejecuta al **ask** (compras) o al **bid** (ventas) de un ``bookTicker``
  recién pedido, con la comisión, el paso de cantidad y el nocional mínimo del par.
* Filtros solo en vivo (apartado 4 del diseño): no se compra un par que no está en
  ``TRADING``, que no se puede negociar al contado, sin precio o con un diferencial de más
  del 0,5 %. Solo bloquean compras: el universo y el ranking no cambian.
* Retiradas (apartado 6.6): 7 días seguidos sin vela con negociación cierran la posición
  al último cierre × 0,90, como en el backtest; en vivo, un par que deja de estar en
  ``TRADING`` se vende al bid en el paso siguiente.
* El paso de un día es **una sola transacción**: si el proceso muere a mitad, no queda
  nada a medias y repetirlo no duplica nada (además, cada orden tiene una clave única).
* Solo simulación: no hay claves, ni firmas, ni órdenes reales.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.config import RiskLimits
from copybot.crypto import costs
from copybot.crypto.binance import BookTicker
from copybot.crypto.engine import DELIST_DAYS
from copybot.crypto.explain import fmt_pct
from copybot.crypto.strategy import (
    DELISTED_HAIRCUT,
    STRATEGY_V1,
    STRATEGY_VERSION,
    BookView,
    DayView,
    Holding,
    StrategyParams,
    decide,
)
from copybot.crypto.sync import EXCHANGE
from copybot.db.models import (
    CryptoPaperBook,
    CryptoPaperDay,
    CryptoPaperEvent,
    CryptoPaperOrder,
    CryptoPaperPosition,
    CryptoPaperRun,
    CryptoPaperSnapshot,
    MarketBar,
)
from copybot.sim.risk import BookState, halt_reasons

CRYPTO_LOCK_ID = 7_431_250_001
ACTIVE = ("RUNNING", "PAUSED", "HALTED_DAY", "HALTED_RISK")
PORTFOLIO, SIGNALS = "portfolio", "signals"
BTC = "BTCUSDT"
ZERO = Decimal(0)
MAX_SPREAD_PCT = Decimal("0.5")  # diferencial máximo del bookTicker para comprar (en %)
LIVE_BLOCK = "bloqueada en vivo"


class PaperError(RuntimeError):
    pass


@dataclass(frozen=True)
class Filters:
    """Lo que dice Binance del par (``market_symbols``). ``None``: no se sabe."""

    step: Decimal | None
    min_notional: Decimal | None
    status: str | None = None  # TRADING, BREAK, DESAPARECIDO…
    spot: bool | None = None  # isSpotTradingAllowed


@dataclass
class StepResult:
    run_id: int
    day: date
    status: str  # ok, ya_hecho, inactiva
    orders: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    equity: Decimal | None = None
    events: list[str] = field(default_factory=list)
    blocked: list[tuple[str, str]] = field(default_factory=list)  # moneda, «bloqueada en vivo: …»


def idempotency_key(
    run_id: int,
    book: str,
    day: date,
    symbol: str,
    side: str,
    purpose: str,
    position_id: int | None = None,
) -> str:
    """Clave única de una orden. Las ventas llevan la posición: el libro de señales puede
    tener dos posiciones de la misma moneda que cumplen sus 7 días el mismo día."""
    raw = f"{run_id}|{book}|{day.isoformat()}|{symbol}|{side}|{purpose}|{STRATEGY_VERSION}"
    if position_id is not None:
        raw += f"|{position_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _d(x: float) -> Decimal:
    return Decimal(str(round(x, 12)))


def block_reason(quote: BookTicker | None, filters: Filters | None) -> str | None:
    """Motivo por el que hoy no se puede comprar un par en vivo (``None``: se puede)."""
    if filters is not None and filters.status not in (None, "TRADING"):
        return f"el par no está en TRADING (estado {filters.status})"
    if filters is not None and filters.spot is False:
        return "Binance no permite negociarlo al contado"
    spread = None if quote is None else quote.spread_pct
    if spread is None:
        return "sin precio de compra ahora (bookTicker)"
    if spread > MAX_SPREAD_PCT:
        return (
            f"diferencial del {fmt_pct(spread / 100, 2, sign=False)} "
            f"(máximo {fmt_pct(MAX_SPREAD_PCT / 100, 1, sign=False)})"
        )
    return None


def live_blocks(
    view: DayView,
    quotes: dict[str, BookTicker],
    filters: dict[str, Filters],
    params: StrategyParams = STRATEGY_V1,
    held: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Candidatas a compra de hoy (puestos de entrada con tendencia y sin posición) que los
    filtros solo en vivo no dejan comprar, con el motivo. ``decide()`` pasa a la siguiente."""
    out = {}
    for sig in view.ranked:
        if sig.rank > params.entry_rank:
            break
        if sig.symbol in held or sig.trend_share + 1e-9 < params.entry_trend:
            continue
        why = block_reason(quotes.get(sig.symbol), filters.get(sig.symbol))
        if why is not None:
            out[sig.symbol] = why
    return out


async def start_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    kind: str,
    start_day: date,
    limits: RiskLimits,
    days: int | None = None,
    created_by: str = "cli",
    params: StrategyParams = STRATEGY_V1,
    btc_price: Decimal | None = None,
) -> int:
    """Crea una cartera (``continua`` o ``prueba``) con su libro de 100 $ y su libro de
    señales. Como mucho una activa de cada tipo. Sin ``btc_price``, la referencia de BTC
    se fija con el ask del primer paso."""
    if kind not in ("continua", "prueba"):
        raise ValueError("kind debe ser «continua» o «prueba»")
    async with sessions.begin() as s:
        busy = await s.scalar(
            select(CryptoPaperRun.id).where(
                CryptoPaperRun.kind == kind, CryptoPaperRun.status.in_(ACTIVE)
            )
        )
        if busy is not None:
            raise PaperError(f"ya hay una cartera «{kind}» activa (n.º {busy})")
        initial = limits.virtual_capital_usd
        run = CryptoPaperRun(
            kind=kind,
            requested_days=days,
            start_day=start_day,
            end_day=start_day + timedelta(days=days) if days else None,
            status="RUNNING",
            strategy_version=STRATEGY_VERSION,
            params_hash=params.params_hash(),
            limits=limits.model_dump(mode="json"),
            initial_usd=initial,
            created_by=created_by,
        )
        s.add(run)
        await s.flush()
        s.add_all(
            [
                CryptoPaperBook(
                    run_id=run.id,
                    book=PORTFOLIO,
                    initial_usd=initial,
                    cash_usd=initial,
                    peak_equity_usd=initial,
                    prev_equity_usd=initial,
                ),
                CryptoPaperBook(
                    run_id=run.id,
                    book=SIGNALS,
                    initial_usd=ZERO,
                    cash_usd=None,
                    peak_equity_usd=ZERO,
                    prev_equity_usd=ZERO,
                ),
                CryptoPaperEvent(
                    run_id=run.id,
                    kind="inicio",
                    details={"btc_precio": str(btc_price) if btc_price else None},
                ),
            ]
        )
        return run.id


async def active_runs(sessions: async_sessionmaker[AsyncSession]) -> list[CryptoPaperRun]:
    async with sessions() as s:
        return list(
            await s.scalars(
                select(CryptoPaperRun)
                .where(CryptoPaperRun.status.in_(ACTIVE))
                .order_by(CryptoPaperRun.id)
            )
        )


async def set_status(
    sessions: async_sessionmaker[AsyncSession], run_id: int, status: str, reason: str
) -> None:
    async with sessions.begin() as s:
        run = await s.get(CryptoPaperRun, run_id, with_for_update=True)
        if run is None:
            raise PaperError(f"no existe la cartera {run_id}")
        if run.status not in ACTIVE:
            raise PaperError(f"la cartera {run_id} ya terminó ({run.status})")
        run.status, run.status_reason = status, reason
        s.add(CryptoPaperEvent(run_id=run_id, kind=status.lower(), details={"motivo": reason}))


def drawdown_halt(run: CryptoPaperRun, book: CryptoPaperBook) -> str | None:
    """La parada por caída del 50 % sigue vigente con el último patrimonio al cierre frente
    al máximo: ``None`` si ya no (o si la cartera aún no ha dado ningún paso)."""
    limits = RiskLimits.model_validate(run.limits)
    cash = book.cash_usd or ZERO
    state = BookState(
        initial_usd=run.initial_usd,
        cash_usd=cash,
        reserved_usd=ZERO,
        positions_value_usd=book.prev_equity_usd - cash,
        open_positions=0,
        open_mints=frozenset(),
        peak_equity_usd=book.peak_equity_usd,
        day_start_equity_usd=book.prev_equity_usd,
    )
    return next((why for code, why in halt_reasons(state, limits) if code == "max_drawdown"), None)


async def resume(sessions: async_sessionmaker[AsyncSession], run_id: int) -> None:
    """Reanuda a mano una cartera en pausa o parada. Mientras la caída del 50 % siga
    vigente no se reanuda: el paso siguiente la volvería a parar sin avisar."""
    async with sessions.begin() as s:
        run = await s.get(CryptoPaperRun, run_id, with_for_update=True)
        if run is None:
            raise PaperError(f"no existe la cartera {run_id}")
        if run.status not in ACTIVE:
            raise PaperError(f"la cartera {run_id} ya terminó ({run.status})")
        book = await s.get(CryptoPaperBook, (run_id, PORTFOLIO))
        why = None if book is None else drawdown_halt(run, book)
        if why is not None:
            raise PaperError(
                "no se reanuda: la parada por caída del 50 % sigue vigente "
                f"({why}). El paso siguiente la volvería a parar; las ventas siguen sus "
                "reglas mientras tanto."
            )
        run.status, run.status_reason = "RUNNING", "reanudada a mano"
        s.add(CryptoPaperEvent(run_id=run_id, kind="resume", details={"motivo": "a mano"}))


async def _open_positions(s: AsyncSession, run_id: int, book: str) -> list[CryptoPaperPosition]:
    return list(
        await s.scalars(
            select(CryptoPaperPosition)
            .where(
                CryptoPaperPosition.run_id == run_id,
                CryptoPaperPosition.book == book,
                CryptoPaperPosition.status == "open",
            )
            .order_by(CryptoPaperPosition.symbol, CryptoPaperPosition.id)
        )
    )


def _last_close(p: CryptoPaperPosition, view: DayView) -> float:
    close = view.close.get(p.symbol)
    if close is not None:
        return close
    if p.last_value_usd is not None and p.qty > 0:
        return float(p.last_value_usd / p.qty)
    return float(p.entry_price)


async def _last_traded_day(s: AsyncSession, symbol: str, day: date) -> date | None:
    """Último día hasta ``day`` con vela diaria con negociación (``None``: sin velas)."""
    until = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
    last = await s.scalar(
        select(func.max(MarketBar.open_time)).where(
            MarketBar.exchange == EXCHANGE,
            MarketBar.symbol == symbol,
            MarketBar.interval == "1d",
            MarketBar.trades > 0,
            MarketBar.open_time < until,
        )
    )
    return None if last is None else last.date()


async def paper_step(
    sessions: async_sessionmaker[AsyncSession],
    run_id: int,
    view: DayView,
    quotes: dict[str, BookTicker],
    filters: dict[str, Filters],
    *,
    now: datetime,
    analysis_id: int | None = None,
    params: StrategyParams = STRATEGY_V1,
) -> StepResult:
    """Paso del día ``view.day`` (vela cerrada) para la cartera ``run_id``, ejecutando
    con los precios de ``quotes``. Idempotente: un día ya hecho no se repite."""
    day = view.day
    result = StepResult(run_id, day, "ok")
    async with sessions.begin() as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CRYPTO_LOCK_ID})
        run = await s.get(CryptoPaperRun, run_id, with_for_update=True)
        if run is None or run.status not in ACTIVE:
            result.status = "inactiva"
            return result
        if await s.get(CryptoPaperDay, (run_id, day)) is not None:
            result.status = "ya_hecho"
            return result
        if run.status == "HALTED_DAY":  # la parada diaria se reanuda sola al día siguiente
            run.status, run.status_reason = "RUNNING", None
        await _btc_start(s, run, quotes)
        limits = RiskLimits.model_validate(run.limits)
        book = await s.get(CryptoPaperBook, (run_id, PORTFOLIO), with_for_update=True)
        positions = await _open_positions(s, run_id, PORTFOLIO)
        # --- Retiradas, antes de decidir (como en el backtest) ---------------------------
        book.cash_usd += await _delistings(
            s, run_id, day, positions, view, quotes, filters, now, result
        )
        positions = [p for p in positions if p.status == "open"]
        held = frozenset(p.symbol for p in positions)
        blocked = live_blocks(view, quotes, filters, params, held)
        bv = BookView(
            cash=float(book.cash_usd),
            holdings={
                p.symbol: Holding(
                    p.symbol, float(p.qty), float(p.entry_price), p.opened_day, _last_close(p, view)
                )
                for p in positions
            },
            prev_equity=float(book.prev_equity_usd),
            peak_equity=float(book.peak_equity_usd),
            initial=float(run.initial_usd),
            halted_risk=run.status == "HALTED_RISK",
            paused=run.status == "PAUSED",
            blocked=frozenset(blocked),
        )
        decision = decide(view, bv, params, limits, mode="dolares", apply_drawdown_halt=True)
        codes = {code for code, _ in decision.halts}
        for _code, why in decision.halts:
            s.add(CryptoPaperEvent(run_id=run_id, kind="parada", details={"motivo": why}))
            result.events.append(why)
        if "max_drawdown" in codes and run.status != "HALTED_RISK":
            run.status, run.status_reason = "HALTED_RISK", "caída del 50 %: solo se reanuda a mano"
        elif "daily_loss" in codes and run.status == "RUNNING":
            run.status, run.status_reason = "HALTED_DAY", "pérdida del día del 20 %"
        if blocked:
            result.blocked = [(sym, f"{LIVE_BLOCK}: {why}") for sym, why in blocked.items()]
            s.add(
                CryptoPaperEvent(
                    run_id=run_id,
                    kind="bloqueo",
                    details={"dia": day.isoformat(), "bloqueadas": dict(result.blocked)},
                )
            )
        btc_ask = quotes[BTC].ask if BTC in quotes else None
        by_symbol = {p.symbol: p for p in positions}
        cash = book.cash_usd
        for order in decision.sells:
            pos = by_symbol[order.symbol]
            cash += await _sell(
                s,
                run_id,
                PORTFOLIO,
                day,
                pos,
                order.purpose,
                order.reason,
                quotes,
                filters,
                now,
                view,
                result,
                btc_bid=_bid(quotes),
            )
        # El último día de una prueba no se compra: _finish lo vende todo a continuación.
        last_day = run.kind == "prueba" and run.end_day is not None and day >= run.end_day
        for order in [] if last_day else decision.buys:
            spent = await _buy(
                s,
                run_id,
                PORTFOLIO,
                day,
                order.symbol,
                _d(order.usd),
                cash,
                order.reason,
                quotes,
                filters,
                now,
                view,
                result,
                btc_ask,
                details=order.details,
            )
            cash -= spent
        book.cash_usd = cash
        await _signals_book(s, run_id, day, view, quotes, filters, now, params, result)
        # --- Patrimonio y referencias ------------------------------------------------
        open_now = await _open_positions(s, run_id, PORTFOLIO)
        value_close = sum((p.qty * _d(_last_close(p, view)) for p in open_now), ZERO)
        for p in open_now:
            p.last_value_usd = p.qty * _d(_last_close(p, view))
        bid_value = sum(
            (p.qty * quotes[p.symbol].bid for p in open_now if p.symbol in quotes), ZERO
        )
        equity = _d(decision.equity)
        btc_bench = await _btc_benchmark(s, run, view)
        s.add(
            CryptoPaperSnapshot(
                run_id=run_id,
                book=PORTFOLIO,
                day=day,
                equity_usd=equity,
                worst_equity_usd=_d(decision.worst_equity),
                bid_equity_usd=cash + bid_value,
                cash_usd=cash,
                positions_value_usd=value_close,
                open_positions=len(open_now),
                btc_benchmark_usd=btc_bench,
            )
        )
        book.prev_equity_usd = equity
        book.peak_equity_usd = max(book.peak_equity_usd, equity)
        book.last_day = day
        s.add(
            CryptoPaperDay(
                run_id=run_id,
                day=day,
                status="ok",
                analysis_id=analysis_id,
                started_at=now,
                finished_at=now,
            )
        )
        result.equity = equity
        if last_day:
            await _finish(s, run, day, quotes, filters, now, view, result, btc_bench)
    return result


async def record_day(
    sessions: async_sessionmaker[AsyncSession],
    run_id: int,
    day: date,
    status: str,
    *,
    now: datetime,
    note: str,
    analysis_id: int | None = None,
) -> bool:
    """Apunta un día sin paso (``perdido`` o ``sin_datos``): no se opera ni se rellena con
    precios pasados. ``False`` si ese día ya estaba hecho o la cartera no está activa."""
    if status not in ("perdido", "sin_datos"):
        raise ValueError("status debe ser «perdido» o «sin_datos»")
    async with sessions.begin() as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CRYPTO_LOCK_ID})
        run = await s.get(CryptoPaperRun, run_id, with_for_update=True)
        if run is None or run.status not in ACTIVE:
            return False
        if await s.get(CryptoPaperDay, (run_id, day)) is not None:
            return False
        s.add(
            CryptoPaperDay(
                run_id=run_id,
                day=day,
                status=status,
                analysis_id=analysis_id,
                started_at=now,
                finished_at=now,
            )
        )
        s.add(
            CryptoPaperEvent(
                run_id=run_id, kind=status, details={"dia": day.isoformat(), "motivo": note}
            )
        )
    return True


def _bid(quotes: dict[str, BookTicker]) -> Decimal | None:
    return quotes[BTC].bid if BTC in quotes else None


async def _order(
    s: AsyncSession,
    run_id: int,
    book: str,
    day: date,
    symbol: str,
    side: str,
    purpose: str,
    position_id: int | None = None,
) -> CryptoPaperOrder | None:
    """Crea la orden pendiente; ``None`` si ya existía (clave única)."""
    key = idempotency_key(run_id, book, day, symbol, side, purpose, position_id)
    existing = await s.scalar(
        select(CryptoPaperOrder).where(CryptoPaperOrder.idempotency_key == key)
    )
    if existing is not None:
        return None
    order = CryptoPaperOrder(
        run_id=run_id,
        book=book,
        day=day,
        idempotency_key=key,
        symbol=symbol,
        side=side,
        purpose=purpose,
        status="pending",
    )
    s.add(order)
    return order


def _close(pos: CryptoPaperPosition, day: date, exit_purpose: str, net: Decimal, btc_bid) -> None:
    pos.proceeds_usd = (pos.proceeds_usd or ZERO) + net
    pos.status, pos.closed_day, pos.exit_purpose = "closed", day, exit_purpose
    pos.realized_pnl_usd = pos.proceeds_usd - pos.cost_usd
    pos.btc_exit_price = btc_bid


async def _sell(
    s, run_id, book, day, pos, purpose, reason, quotes, filters, now, view, result, *, btc_bid
) -> Decimal:
    order = await _order(s, run_id, book, day, pos.symbol, "sell", purpose, pos.id)
    if order is None:
        return ZERO
    order.reason = reason
    q = quotes.get(pos.symbol)
    f = filters.get(pos.symbol, Filters(None, None))
    if q is None or q.bid <= 0:
        order.status, order.reason = "rejected", f"{reason} · sin precio de venta ahora"
        pos.unsellable = True
        result.orders.append((pos.symbol, "sell", purpose, order.status, order.reason))
        return ZERO
    ref = view.close.get(pos.symbol)
    fill = costs.live_sell(pos.qty, q.bid, f.step)
    if not costs.min_notional_ok(fill.qty, fill.price, f.min_notional):
        # Polvo: Binance no deja vender por debajo de max(minNotional, 5 $), así que la
        # posición no se podría vender nunca. Se da por cerrada al bid menos la comisión.
        gross = pos.qty * q.bid
        fee = gross * Decimal(str(costs.FEE))
        dust = costs.LiveFill(
            qty=pos.qty, price=q.bid, fee_usd=fee, gross_usd=gross, net_usd=gross - fee
        )
        order.reason = (
            f"{reason} · polvo: por debajo del importe mínimo del par; se cierra al bid "
            "menos la comisión"
        )
        _fill(order, dust, q, now, ref)
        pos.qty = ZERO
        _close(pos, day, "polvo", dust.net_usd, btc_bid)
        result.orders.append((pos.symbol, "sell", purpose, "filled", order.reason))
        return dust.net_usd
    _fill(order, fill, q, now, ref)
    pos.qty -= fill.qty
    if pos.qty <= 0 or fill.note:
        _close(pos, day, purpose, fill.net_usd, btc_bid)
    else:
        pos.proceeds_usd = (pos.proceeds_usd or ZERO) + fill.net_usd
    result.orders.append((pos.symbol, "sell", purpose, "filled", reason))
    return fill.net_usd


async def _delistings(s, run_id, day, positions, view, quotes, filters, now, result) -> Decimal:
    """Retiradas de la cartera (apartado 6.6), antes de decidir. Devuelve el efectivo.

    * 7 días seguidos sin vela con negociación: se cierra al último cierre × 0,90 (igual
      que el backtest, que tampoco lo cuenta como decisión).
    * Solo en vivo: si el par ya no está en ``TRADING``, se vende al bid; sin bid queda
      no vendible, valorada al último cierre × 0,90, hasta que se cumplan los 7 días.
    """
    cash = ZERO
    for pos in positions:
        last = await _last_traded_day(s, pos.symbol, day)
        if last is None:  # sin velas guardadas: lo que diga la vista del día
            last = day if pos.symbol in view.close else pos.opened_day
        if (day - last).days >= DELIST_DAYS:
            cash += await _write_off(s, run_id, PORTFOLIO, day, pos, view, now, result, quotes)
            continue
        f = filters.get(pos.symbol)
        if f is not None and f.status not in (None, "TRADING"):
            cash += await _sell(
                s,
                run_id,
                PORTFOLIO,
                day,
                pos,
                "retirada",
                f"el par dejó de cotizar en Binance (estado {f.status})",
                quotes,
                filters,
                now,
                view,
                result,
                btc_bid=_bid(quotes),
            )
    return cash


async def _write_off(s, run_id, book, day, pos, view, now, result, quotes) -> Decimal:
    """Cierre de una retirada: último cierre × 0,90, sin bid (el par ya no cotiza)."""
    order = await _order(s, run_id, book, day, pos.symbol, "sell", "retirada", pos.id)
    if order is None:
        return ZERO
    price = _d(_last_close(pos, view) * DELISTED_HAIRCUT)
    value = pos.qty * price
    order.status, order.filled_at = "filled", now
    order.qty, order.price, order.fee_usd = pos.qty, price, ZERO
    order.reason = (
        f"{DELIST_DAYS} días seguidos sin cotizar: se da por retirada al último cierre × 0,90"
    )
    pos.qty = ZERO
    _close(pos, day, "retirada", value, _bid(quotes))
    s.add(
        CryptoPaperEvent(
            run_id=run_id,
            kind="delist",
            details={"dia": day.isoformat(), "moneda": pos.symbol, "valor": str(value)},
        )
    )
    result.orders.append((pos.symbol, "sell", "retirada", "filled", order.reason))
    return value


async def _buy(
    s,
    run_id,
    book,
    day,
    symbol,
    usd,
    cash,
    reason,
    quotes,
    filters,
    now,
    view,
    result,
    btc_ask,
    *,
    details=None,
    purpose="entrada",
) -> Decimal:
    order = await _order(s, run_id, book, day, symbol, "buy", purpose)
    if order is None:
        return ZERO
    order.reason, order.requested_usd, order.decision = reason, usd, details or None
    q = quotes.get(symbol)
    f = filters.get(symbol, Filters(None, None))
    fill = (
        None
        if q is None or q.ask <= 0
        else costs.live_buy(usd, q.ask, q.ask_qty, f.step, f.min_notional)
    )
    if fill is None or (cash is not None and fill.net_usd > cash):
        order.status = "rejected"
        order.reason = f"{reason} · " + (
            "sin precio de compra ahora"
            if q is None
            else "importe por debajo del mínimo"
            if fill is None
            else "no hay efectivo suficiente"
        )
        result.orders.append((symbol, "buy", purpose, order.status, order.reason))
        return ZERO
    _fill(order, fill, q, now, view.close.get(symbol))
    s.add(
        CryptoPaperPosition(
            run_id=run_id,
            book=book,
            symbol=symbol,
            status="open",
            qty=fill.qty,
            entry_price=fill.price,
            cost_usd=fill.net_usd,
            opened_day=day,
            btc_entry_price=btc_ask,
            last_value_usd=fill.gross_usd,
        )
    )
    result.orders.append((symbol, "buy", purpose, "filled", reason))
    return fill.net_usd


def _fill(order: CryptoPaperOrder, fill: costs.LiveFill, q: BookTicker, now, ref) -> None:
    order.status, order.filled_at = "filled", now
    order.qty, order.price, order.fee_usd = fill.qty, fill.price, fill.fee_usd
    order.bid, order.ask, order.ask_qty = q.bid, q.ask, q.ask_qty
    if fill.note:
        order.reason = f"{order.reason} · {fill.note}"
    if ref:
        order.ref_open_price = _d(ref)
        order.exec_gap_pct = (fill.price / _d(ref) - 1) * 100


async def _signals_book(s, run_id, day, view, quotes, filters, now, params, result) -> None:
    """Libro de señales: cada día, las 5 primeras con tendencia, 10 $ cada una, 7 días."""
    for pos in await _open_positions(s, run_id, SIGNALS):
        if (day - pos.opened_day).days >= params.signals_book_days:
            await _sell(
                s,
                run_id,
                SIGNALS,
                day,
                pos,
                "horizonte_7d",
                "cumple 7 días en el libro de señales",
                quotes,
                filters,
                now,
                view,
                result,
                btc_bid=_bid(quotes),
            )
    picks = [x for x in view.ranked if x.trend_share + 1e-9 >= params.entry_trend]
    btc_ask = quotes[BTC].ask if BTC in quotes else None
    for sig in picks[: params.signals_book_top]:
        await _buy(
            s,
            run_id,
            SIGNALS,
            day,
            sig.symbol,
            _d(params.signals_book_usd),
            None,
            f"puesto {sig.rank} del día",
            quotes,
            filters,
            now,
            view,
            result,
            btc_ask,
            purpose="senal",
        )


async def _btc_start(s: AsyncSession, run: CryptoPaperRun, quotes: dict[str, BookTicker]) -> None:
    """Precio de BTC de referencia: si la cartera se creó sin él (una prueba lanzada desde
    la consola, sin red), se fija con el ask de BTC de su primer paso."""
    if BTC not in quotes:
        return
    start = await s.scalar(
        select(CryptoPaperEvent).where(
            CryptoPaperEvent.run_id == run.id, CryptoPaperEvent.kind == "inicio"
        )
    )
    if start is None or (start.details or {}).get("btc_precio"):
        return
    done = await s.scalar(
        select(CryptoPaperDay.day).where(
            CryptoPaperDay.run_id == run.id, CryptoPaperDay.status == "ok"
        )
    )
    if done is None:  # solo en el primer paso: después la referencia ya no sería comparable
        start.details = {**(start.details or {}), "btc_precio": str(quotes[BTC].ask)}


async def _btc_benchmark(s: AsyncSession, run: CryptoPaperRun, view: DayView) -> Decimal | None:
    start = await s.scalar(
        select(CryptoPaperEvent.details).where(
            CryptoPaperEvent.run_id == run.id, CryptoPaperEvent.kind == "inicio"
        )
    )
    price = (start or {}).get("btc_precio")
    close = view.close.get(BTC)
    if not price or close is None:
        return None
    return run.initial_usd * _d(close) / Decimal(price)


async def _finish(s, run, day, quotes, filters, now, view, result, btc_bench) -> None:
    """Fin de una prueba: se vende todo al bid y se congela el veredicto."""
    cash_book = await s.get(CryptoPaperBook, (run.id, PORTFOLIO), with_for_update=True)
    for pos in await _open_positions(s, run.id, PORTFOLIO):
        cash_book.cash_usd += await _sell(
            s,
            run.id,
            PORTFOLIO,
            day,
            pos,
            "fin_prueba",
            "fin de la prueba",
            quotes,
            filters,
            now,
            view,
            result,
            btc_bid=_bid(quotes),
        )
    final = cash_book.cash_usd + sum(
        (p.qty * _d(_last_close(p, view)) for p in await _open_positions(s, run.id, PORTFOLIO)),
        ZERO,
    )
    days = (day - run.start_day).days
    run.status, run.finished_at = "FINISHED", now
    run.verdict = {
        "dias": days,
        "valor_final": str(final.quantize(Decimal("0.01"))),
        "rentabilidad": str(((final / run.initial_usd) - 1).quantize(Decimal("0.0001"))),
        "btc_mismo_periodo": None
        if btc_bench is None
        else str(btc_bench.quantize(Decimal("0.01"))),
        "semaforo": "gris" if days < 28 else None,
        "aviso": "Con menos de 28 días el semáforo de la prueba no puede pasar de gris."
        if days < 28
        else None,
    }
    s.add(CryptoPaperEvent(run_id=run.id, kind="fin", details=run.verdict))
    result.events.append("fin de la prueba")

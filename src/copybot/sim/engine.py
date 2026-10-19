"""Motor de la simulación: convierte las operaciones de los traders seguidos en órdenes
simuladas y gestiona las salidas.

Dos libros con las mismas señales:

* **portfolio**: la cartera de 100 $ con todos los límites de riesgo. Es lo que haría el
  bot de verdad.
* **signals**: cada compra de un trader se copia con 10 $ fijos, sin límite de capital
  ni de posiciones. Mide la calidad de las señales aunque la cartera esté llena o parada.

Para cada compra del trader (señal):

1. Se descarta si es vieja (por defecto, más de 60 s desde su bloque: la oportunidad ya
   pasó; así quedan fuera las recuperadas tras un corte) o si sus datos no cuadran.
2. Filtros del token (autoridades, extensiones peligrosas, comisión de transferencia).
3. Estado del sistema: la cartera solo compra en ``RUNNING``; el libro de señales,
   también en ``HALTED_RISK`` (sigue midiendo aunque la cartera se haya parado).
4. Mercado (Jupiter): liquidez, impacto de nuestra compra y pérdida de ida y vuelta.
5. Límites de riesgo y reserva del efectivo, en una sola transacción (``SimStore``).
6. Ejecución simulada: cotización t2 → latencia → t3, slippage, MEV, comisiones y renta.

Salidas: copia proporcional de las ventas del trader, stop-loss, tiempo máximo y
reintentos de las ventas que fallaron. Las salidas siguen funcionando en ``PAUSED``,
``READ_ONLY`` y ``HALTED_RISK``: reducen riesgo.

Cada decisión (también los rechazos) queda guardada con sus motivos y datos, y lleva una
clave de idempotencia: repetirla no crea otra orden.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import structlog

from copybot.config import RiskLimits
from copybot.db.models import Signal, SimPosition
from copybot.sim.costs import RENT_LAMPORTS, CostModel
from copybot.sim.executor import USDC_DECIMALS, Execution, Executor, QuoteSource, from_raw, to_raw
from copybot.sim.params import EngineParams, SafetyParams
from copybot.sim.quotes import Quote, QuoteError
from copybot.sim.risk import halt_reasons
from copybot.sim.safety import MarketCheck, MintInfo, market_rejections, mint_rejections
from copybot.sim.store import PORTFOLIO, SIGNALS, SimStore, idempotency_key
from copybot.solana.constants import USDC_MINT

log = structlog.get_logger(__name__)

ZERO = Decimal(0)
BOOKS = (PORTFOLIO, SIGNALS)
ENTRY_STATES = {PORTFOLIO: frozenset({"RUNNING"}), SIGNALS: frozenset({"RUNNING", "HALTED_RISK"})}
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SOL_PRICE_MAX_AGE_S = 600


class Market(Protocol):
    async def sol_usd(self) -> Decimal | None: ...

    async def liquidity_usd(self, mint: str) -> Decimal | None: ...

    async def mint_info(self, mint: str) -> MintInfo | None: ...


@dataclass(frozen=True)
class TokenProfile:
    program: str
    decimals: int
    transfer_fee_bps: int

    @property
    def rent_lamports(self) -> int:
        return RENT_LAMPORTS.get(self.program, max(RENT_LAMPORTS.values()))


@dataclass(frozen=True)
class MarketView:
    t2: Quote | None
    reasons: list[str]
    data: dict[str, Any]


def _dec(value: Decimal | None, places: int = 6) -> str | None:
    return None if value is None else str(round(value, places))


class Engine:
    def __init__(
        self,
        store: SimStore,
        market: Market,
        quotes: QuoteSource,
        executor: Executor,
        *,
        params: EngineParams,
        safety: SafetyParams,
        limits: RiskLimits,
        monitor_stale_seconds: float = 120.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.store = store
        self._market = market
        self._quotes = quotes
        self._executor = executor
        self.params = params
        self.safety = safety
        self.limits = limits
        self._stale = monitor_stale_seconds
        self._clock = clock
        self._sleep = sleep
        self._sol: tuple[datetime, Decimal] | None = None
        self._last_revalue: datetime | None = None
        self._stop = asyncio.Event()

    # --- Ciclo de vida -------------------------------------------------------------------

    async def start(self) -> int:
        """Arranque: estado RECOVERING, órdenes a medias dadas por fallidas y, si el
        sistema no estaba en pausa ni parado por riesgo, RUNNING."""
        await self.store.initialize(self.limits.virtual_capital_usd)
        state = (await self.store.state()).state
        if state not in ("PAUSED", "HALTED_RISK"):
            await self.store.set_state("RECOVERING", "arranque del motor", "engine")
        recovered = await self.store.recover_pending()
        await self.store.set_state(
            "RUNNING", "recuperación terminada", "engine", only_from=["RECOVERING"]
        )
        log.info("sim_start", recovered=recovered, state=(await self.store.state()).state)
        return recovered

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self.start()
        while not self._stop.is_set():
            if not await self.tick():
                break
            await self._sleep(self.params.poll_seconds)

    async def tick(self) -> bool:
        """Una vuelta del bucle. ``False`` si alguien ha parado el motor (STOPPED)."""
        state = (await self.store.state()).state
        if state == "STOPPED":
            log.info("sim_stopped")
            return False
        await self._check_feed(state)
        await self.store.ingest_signals()
        for signal in await self.store.pending_signals(BOOKS):
            try:
                await self.process_signal(signal)
            except Exception as exc:  # una señal no debe tumbar el motor
                log.exception("signal_failed", signal=signal.id, error=str(exc))
        await self.retry_exits()
        now = self._clock()
        if self._last_revalue is None or (now - self._last_revalue).total_seconds() >= (
            self.params.revalue_seconds
        ):
            self._last_revalue = now
            await self.revalue()
        return True

    async def _check_feed(self, state: str) -> None:
        """Sin latidos recientes del monitor no hay datos fiables: solo lectura."""
        age = await self.store.monitor_heartbeat_age()
        stale = age is None or age > self._stale
        if stale and state == "RUNNING":
            reason = (
                "el monitor no da señales de vida"
                if age is None
                else f"el último latido del monitor es de hace {age:.0f} s"
            )
            if await self.store.set_state("READ_ONLY", reason, "engine", only_from=["RUNNING"]):
                await self.store.risk_event("stale_feed", None, {"motivo": reason})
        elif not stale and state == "READ_ONLY":
            await self.store.set_state(
                "RUNNING", "el monitor vuelve a dar datos", "engine", only_from=["READ_ONLY"]
            )

    # --- Señales ---------------------------------------------------------------------------

    async def process_signal(self, signal: Signal) -> None:
        decided = await self.store.books_decided(signal.id)
        books = [b for b in BOOKS if b not in decided]
        if not books:
            return
        if signal.side == "buy":
            await self._entry(signal, books)
        else:
            await self._copy_exit(signal, books)

    def _key(self, *parts: object) -> str:
        return idempotency_key(self.params.decision_version, *parts)

    def _signal_reasons(self, signal: Signal, now: datetime) -> tuple[list[str], dict[str, Any]]:
        reasons = []
        ref = signal.block_time or signal.detected_at
        age = (now - ref).total_seconds()
        if age > self.params.max_signal_age_seconds:
            via = " (recuperada tras un corte)" if signal.detected_via == "backfill" else ""
            reasons.append(
                f"la compra del trader es de hace {age:.0f} s{via}; máximo "
                f"{self.params.max_signal_age_seconds:.0f} s: la oportunidad ya pasó"
            )
        if signal.trader_usd is not None and signal.trader_usd <= 0:
            reasons.append("datos incoherentes: el trader no pagó nada por los tokens")
        if signal.trader_token_amount <= 0:
            reasons.append("datos incoherentes: el trader no recibió tokens")
        data = {
            "señal": signal.id,
            "trader": signal.wallet,
            "mint": signal.mint,
            "edad_s": round(age, 2),
            "detectada": signal.detected_via,
            "trader_usd": _dec(signal.trader_usd),
            "trader_tokens": _dec(signal.trader_token_amount),
        }
        return reasons, data

    def _trader_price(self, signal: Signal) -> Decimal | None:
        if signal.trader_usd is None or signal.trader_token_amount <= 0:
            return None
        return signal.trader_usd / signal.trader_token_amount

    async def _entry(self, signal: Signal, books: list[str]) -> None:
        now = self._clock()
        reasons, decision = self._signal_reasons(signal, now)
        profile = costs = None
        if not reasons:
            profile, token_reasons = await self._token_profile(signal.mint)
            reasons += token_reasons
        if not reasons:
            costs = await self._costs()
            if costs is None:
                reasons.append("no hay precio de SOL para calcular las comisiones")
        if reasons or profile is None or costs is None:
            for book in books:
                await self._reject(signal, book, "buy", "entry", reasons, decision)
            return

        state = (await self.store.state()).state
        decision |= {
            "estado": state,
            "sol_usd": _dec(costs.sol_usd, 4),
            "comision_red_usd": _dec(costs.tx_fee_usd()),
            "token": {
                "programa": profile.program,
                "comision_transferencia_bps": (profile.transfer_fee_bps),
            },
        }
        sizes: dict[str, tuple[Decimal, int, Decimal, Decimal]] = {}
        for book in books:
            if state not in ENTRY_STATES[book]:
                await self._reject(
                    signal,
                    book,
                    "buy",
                    "entry",
                    [f"el sistema está en estado {state}: no se abren posiciones"],
                    decision,
                )
                continue
            size = (
                self.limits.max_position_usd
                if book == PORTFOLIO
                else self.params.signal_book_size_usd
            )
            existing = await self.store.position_for(book, signal.mint, signal.wallet)
            rent = 0 if existing is not None else profile.rent_lamports
            rent_usd = costs.lamports_usd(rent)
            # Los 10 $ lo incluyen todo: comisión de red y renta de la cuenta de token.
            swap_usd = (size - costs.tx_fee_usd() - rent_usd).quantize(Decimal("0.000001"))
            if swap_usd <= 0:
                await self._reject(
                    signal, book, "buy", "entry", ["las comisiones superan el tamaño"], decision
                )
                continue
            sizes[book] = (size, rent, rent_usd, swap_usd)

        views: dict[Decimal, MarketView] = {}
        orders: list[tuple[str, int, Decimal, int, Decimal]] = []
        for book, (size, rent, rent_usd, swap_usd) in sizes.items():
            if swap_usd not in views:
                views[swap_usd] = await self._market_view(signal.mint, profile, swap_usd)
            view = views[swap_usd]
            book_decision = decision | {
                "tamaño_usd": _dec(size),
                "compra_usd": _dec(swap_usd),
                "renta_usd": _dec(rent_usd),
                "mercado": view.data,
            }
            if view.reasons:
                await self._reject(signal, book, "buy", "entry", view.reasons, book_decision)
                continue
            result = await self.store.open_entry(
                book=book,
                signal=signal,
                key=self._key(book, signal.id, "entry"),
                size=size,
                decision=book_decision,
                limits=self.limits if book == PORTFOLIO else None,
                allowed_states=ENTRY_STATES[book],
            )
            if result.accepted and result.order_id is not None:
                orders.append((book, result.order_id, swap_usd, rent, rent_usd))

        executions: dict[Decimal, Execution] = {}
        price = self._trader_price(signal)
        for book, order_id, swap_usd, rent, rent_usd in orders:
            if swap_usd not in executions:
                t2 = views[swap_usd].t2
                if t2 is None:  # no pasa: sin t2 la vista de mercado ya rechaza
                    raise RuntimeError("compra sin cotización t2")
                executions[swap_usd] = await self._buy(signal.mint, profile, swap_usd, t2, costs)
            ex = executions[swap_usd]
            if not ex.ok:
                await self.store.fail_order(order_id, ex.reason, ex.fees_usd)
                continue
            size = sizes[book][0]
            await self.store.fill_entry(
                order_id,
                ex,
                rent_lamports=rent,
                rent_usd=rent_usd,
                theoretical_price=price,
                theoretical_tokens=None if price is None else size / price,
            )
            log.info("sim_buy", book=book, mint=signal.mint, usd=str(swap_usd))

    async def _buy(
        self, mint: str, profile: TokenProfile, usd: Decimal, t2: Quote, costs: CostModel
    ) -> Execution:
        try:
            return await self._executor.buy(
                mint, profile.decimals, usd, profile.transfer_fee_bps, t2, costs
            )
        except QuoteError as exc:
            # No sabemos si la transacción habría entrado: se cuenta como fallida y pagada.
            return Execution(
                False, f"Jupiter no responde al ejecutar: {exc}", fees_usd=(costs.tx_fee_usd())
            )

    async def _reject(
        self,
        signal: Signal,
        book: str,
        side: str,
        purpose: str,
        reasons: list[str],
        decision: dict[str, Any],
    ) -> None:
        await self.store.record_rejection(
            book=book,
            signal=signal,
            key=self._key(book, signal.id, purpose),
            side=side,
            purpose=purpose,
            reasons=reasons,
            decision=decision,
        )

    async def _market_view(self, mint: str, profile: TokenProfile, usd: Decimal) -> MarketView:
        """Liquidez, cotización t2 de nuestra compra e ida y vuelta (detecta invendibles)."""
        tolerance = self.params.slippage_tolerance_bps
        try:
            liquidity = await self._market.liquidity_usd(mint)
            t2 = await self._quotes.quote(USDC_MINT, mint, to_raw(usd, USDC_DECIMALS), tolerance)
            back = None
            if t2 is not None:
                received = CostModel.after_transfer_fee(
                    from_raw(t2.out_raw, profile.decimals), profile.transfer_fee_bps
                )
                sent = to_raw(
                    CostModel.after_transfer_fee(received, profile.transfer_fee_bps),
                    profile.decimals,
                )
                back = await self._quotes.quote(mint, USDC_MINT, sent, tolerance)
        except Exception as exc:  # sin datos de mercado no se compra
            return MarketView(None, [f"no hay datos de mercado: {exc}"], {})
        roundtrip = None
        if t2 is not None and back is not None and t2.in_raw > 0:
            roundtrip = (1 - Decimal(back.out_raw) / Decimal(t2.in_raw)) * 100
        check = MarketCheck(
            liquidity_usd=liquidity,
            buy_price_impact_pct=None if t2 is None else t2.price_impact_pct,
            roundtrip_loss_pct=roundtrip,
        )
        data = {
            "liquidez_usd": _dec(liquidity, 0),
            "impacto_compra_pct": _dec(check.buy_price_impact_pct, 3),
            "ida_y_vuelta_pct": _dec(roundtrip, 3),
            "t2_tokens_raw": None if t2 is None else t2.out_raw,
            "ruta": [] if t2 is None else list(t2.route),
        }
        return MarketView(t2, market_rejections(check, self.safety), data)

    async def _token_profile(self, mint: str) -> tuple[TokenProfile | None, list[str]]:
        since = self._clock() - timedelta(minutes=self.safety.mint_check_cache_minutes)
        row = await self.store.latest_safety_check(mint, since)
        if row is not None:
            return self._profile_from(row.details), list(row.reasons)
        try:
            info = await self._market.mint_info(mint)
        except Exception as exc:
            return None, [f"no se pudo leer la cuenta del token: {exc}"]
        reasons = mint_rejections(info, self.safety)
        details: dict[str, Any] = {}
        if info is not None:
            details = {
                "programa": info.program,
                "decimales": info.decimals,
                "comision_transferencia_bps": info.transfer_fee_bps,
                "comision_mutable": info.transfer_fee_mutable,
                "mint_authority": info.mint_authority,
                "freeze_authority": info.freeze_authority,
                "extensiones": sorted(info.extensions),
            }
        await self.store.save_safety_check(mint, not reasons, reasons, details)
        return self._profile_from(details), reasons

    @staticmethod
    def _profile_from(details: dict[str, Any]) -> TokenProfile | None:
        if "decimales" not in details:
            return None
        return TokenProfile(
            program=str(details.get("programa", "")),
            decimals=int(details["decimales"]),
            transfer_fee_bps=int(details.get("comision_transferencia_bps", 0)),
        )

    async def _known_profile(self, mint: str) -> TokenProfile | None:
        """Datos del token para vender lo que ya tenemos (aunque la caché haya caducado)."""
        row = await self.store.latest_safety_check(mint, EPOCH)
        profile = None if row is None else self._profile_from(row.details)
        if profile is None:
            profile, _ = await self._token_profile(mint)
        return profile

    async def _costs(self) -> CostModel | None:
        now = self._clock()
        if self._sol is None or (now - self._sol[0]).total_seconds() > 60:
            try:
                price = await self._market.sol_usd()
            except Exception as exc:
                log.warning("sol_price_failed", error=str(exc))
                price = None
            if price is not None and price > 0:
                self._sol = (now, price)
        # Un precio de SOL de hace más de 10 minutos ya no sirve para calcular comisiones.
        if self._sol is None or (now - self._sol[0]).total_seconds() > SOL_PRICE_MAX_AGE_S:
            return None
        return CostModel(
            sol_usd=self._sol[1],
            base_fee_lamports=self.params.base_fee_lamports,
            priority_fee_lamports=self.params.priority_fee_lamports,
            mev_haircut_bps=self.params.mev_haircut_bps,
        )

    # --- Salidas -----------------------------------------------------------------------------

    async def _copy_exit(self, signal: Signal, books: list[str]) -> None:
        _, decision = self._signal_reasons(signal, self._clock())
        fraction = signal.trader_sell_fraction
        decision["fraccion_vendida_trader"] = _dec(fraction)
        price = self._trader_price(signal)
        for book in books:
            pos = await self.store.position_for(book, signal.mint, signal.wallet)
            if pos is None:
                await self._reject(
                    signal,
                    book,
                    "sell",
                    "copy_exit",
                    ["no tenemos posición copiada de este trader en este token"],
                    decision,
                )
                continue
            amount = self._exit_amount(pos, fraction)
            order_id = await self.store.open_exit(
                position_id=pos.id,
                key=self._key(book, signal.id, "copy_exit"),
                amount=amount,
                purpose="copy_exit",
                signal_id=signal.id,
                decision=decision | {"vender_tokens": _dec(amount)},
            )
            if order_id is not None:
                await self._sell(order_id, pos, amount, theoretical_price=price)

    def _exit_amount(self, pos: SimPosition, fraction: Decimal | None) -> Decimal:
        """Misma fracción que vende el trader (todo si no se sabe); sin dejar restos."""
        if fraction is None or fraction >= 1:
            return pos.amount
        amount = pos.amount * fraction
        if pos.amount - amount <= pos.amount * self.params.exit_dust_fraction:
            return pos.amount
        return amount

    async def _sell(
        self,
        order_id: int,
        pos: SimPosition,
        amount: Decimal,
        *,
        theoretical_price: Decimal | None = None,
    ) -> bool:
        profile = await self._known_profile(pos.mint)
        costs = await self._costs()
        if profile is None or costs is None:
            await self.store.fail_order(order_id, "faltan datos del token o precio de SOL", ZERO)
            return False
        try:
            ex = await self._executor.sell(
                pos.mint, profile.decimals, amount, profile.transfer_fee_bps, costs
            )
        except QuoteError as exc:
            await self.store.fail_order(order_id, f"Jupiter no responde: {exc}", ZERO)
            return False
        if not ex.ok:
            await self.store.fail_order(order_id, ex.reason, ex.fees_usd)
            if ex.no_route:
                await self.store.mark_value(pos.id, ZERO, True)
            return False
        if theoretical_price is None and amount > 0:
            # Sin precio del trader: la copia teórica vende al precio de mercado, sin costes.
            raw = ex.detail.get("t3_out")
            theoretical_price = None if raw is None else from_raw(int(raw), USDC_DECIMALS) / amount
        await self.store.fill_exit(
            order_id,
            ex,
            rent_refund_usd=costs.lamports_usd(pos.rent_lamports),
            theoretical_price=theoretical_price,
        )
        log.info("sim_sell", book=pos.book, mint=pos.mint, usd=str(ex.usd_amount))
        return True

    async def _exit_all(self, pos: SimPosition, purpose: str, why: str) -> None:
        attempt = await self.store.exit_attempts(pos.id, purpose)
        order_id = await self.store.open_exit(
            position_id=pos.id,
            key=self._key(pos.book, pos.id, purpose, attempt),
            amount=pos.amount,
            purpose=purpose,
            signal_id=None,
            decision={"motivo": why, "intento": attempt + 1},
        )
        if order_id is not None:
            await self._sell(order_id, pos, pos.amount)

    async def retry_exits(self) -> None:
        """Vuelve a intentar las ventas que fallaron, cada vez más espaciadas."""
        now = self._clock()
        for pos in await self.store.open_positions():
            if not pos.exit_pending or await self.store.has_pending_order(pos.id):
                continue
            last = await self.store.last_exit_order(pos.id)
            if last is None:
                continue
            attempts = await self.store.exit_attempts(pos.id, last.purpose)
            wait = min(30 * 2 ** max(attempts - 1, 0), 1800)
            since = pos.last_exit_attempt_at or last.updated_at
            if (now - since).total_seconds() < wait:
                continue
            amount = min(last.requested_amount or pos.amount, pos.amount)
            order_id = await self.store.open_exit(
                position_id=pos.id,
                key=self._key(pos.book, pos.id, last.purpose, "reintento", attempts),
                amount=amount,
                purpose=last.purpose,
                signal_id=last.signal_id,
                decision={"reintento": attempts, "orden_fallida": last.id},
            )
            if order_id is not None:
                await self._sell(order_id, pos, amount)

    # --- Valoración, stops y límites ----------------------------------------------------------

    async def revalue(self, limit: int = 30) -> None:
        now = self._clock()
        costs = await self._costs()
        before = now - timedelta(seconds=self.params.revalue_seconds)
        max_hold = timedelta(hours=float(self.params.max_hold_hours))
        if costs is not None:
            for pos in await self.store.stale_positions(before, limit):
                await self._revalue_position(pos, costs, now, max_hold)
        await self._check_risk(costs)

    async def _revalue_position(
        self, pos: SimPosition, costs: CostModel, now: datetime, max_hold: timedelta
    ) -> None:
        profile = await self._known_profile(pos.mint)
        if profile is None:
            return
        sent = to_raw(
            CostModel.after_transfer_fee(pos.amount, profile.transfer_fee_bps), profile.decimals
        )
        try:
            quote = await self._quotes.quote(
                pos.mint, USDC_MINT, sent, self.params.slippage_tolerance_bps
            )
        except QuoteError as exc:
            log.warning("revalue_failed", position=pos.id, error=str(exc))
            return
        rent_refund = costs.lamports_usd(pos.rent_lamports)
        if quote is None:
            await self.store.mark_value(pos.id, ZERO, True)
            token_value = ZERO
        else:
            token_value = costs.after_mev(from_raw(quote.out_raw, USDC_DECIMALS)) - (
                costs.tx_fee_usd()
            )
            await self.store.mark_value(pos.id, max(token_value, ZERO) + rent_refund, False)
        if pos.exit_pending:
            return
        if pos.cost_usd > 0:
            loss_pct = (1 - token_value / pos.cost_usd) * 100
            if loss_pct >= self.params.stop_loss_pct:
                why = f"pierde un {loss_pct:.1f} % (stop-loss en el {self.params.stop_loss_pct} %)"
                await self._exit_all(pos, "stop_loss", why)
                return
        if now - pos.opened_at >= max_hold:
            await self._exit_all(
                pos, "max_hold", f"lleva abierta más de {self.params.max_hold_hours} h"
            )

    async def _check_risk(self, costs: CostModel | None) -> None:
        state = await self.store.roll_equity(PORTFOLIO)
        control = (await self.store.state()).state
        halts = halt_reasons(state, self.limits)
        if halts and control in ("RUNNING", "READ_ONLY"):
            text = "; ".join(detail for _, detail in halts)
            if await self.store.set_state(
                "HALTED_RISK", text, "engine", only_from=["RUNNING", "READ_ONLY"]
            ):
                for kind, detail in halts:
                    await self.store.risk_event(kind, PORTFOLIO, {"detalle": detail})
                log.warning("sim_halted", reasons=text)
                control = "HALTED_RISK"
        await self.store.snapshot(
            PORTFOLIO,
            state,
            {
                "estado": control,
                "sol_usd": None if costs is None else _dec(costs.sol_usd, 4),
                "maximo_usd": _dec(state.peak_equity_usd),
                "inicio_dia_usd": _dec(state.day_start_equity_usd),
            },
        )

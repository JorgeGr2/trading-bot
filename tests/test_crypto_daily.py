"""Paso diario: plazo de las 02:00, reintentos con datos incompletos, arranque del
servicio, cartera continua y medidas de riesgo del informe (Binance simulado)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from copybot.config import RiskLimits
from copybot.crypto import daily
from copybot.crypto.analysis import analyze, store
from copybot.crypto.binance import BookTicker
from copybot.crypto.daily import DailyResult, run_daily, run_forever
from copybot.crypto.paper import StepResult, set_status, start_run
from copybot.crypto_cli import paper_target
from copybot.db.models import (
    CryptoAnalysis,
    CryptoPaperDay,
    CryptoPaperOrder,
    CryptoPaperRun,
    CryptoPaperSnapshot,
)
from copybot.db.session import make_sessionmaker
from tests.test_crypto_engine import make_panel

D = Decimal
LIMITS = RiskLimits.model_construct()


def _panel(n: int = 24):
    """Panel sintético con BTC muy por encima de su media de 100 días (mercado favorable);
    con ``n`` < 19 el universo no llega a 20 monedas (datos incompletos)."""
    panel = make_panel(n=n, days=260, seed=3, drift=lambda k: (k - 12) * 0.001)
    btc = panel.series["BTCUSDT"]
    for i in range(len(btc.close) - 30, len(btc.close)):
        f = 1.02 ** (i - (len(btc.close) - 30))
        btc.close[i] *= f
        btc.open[i] *= f
        btc.high[i] *= f * 1.01
        btc.low[i] *= f * 0.99
    return panel


class FakeMarket:
    """Binance simulado: solo el bookTicker (alrededor del último cierre)."""

    def __init__(self, panel) -> None:
        self.panel = panel

    async def __aenter__(self) -> FakeMarket:
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def book_tickers(self) -> list[BookTicker]:
        out = []
        for s in self.panel.series.values():
            c = D(str(round(s.close[-1], 6)))
            out.append(BookTicker(s.symbol, c * D("0.9995"), c * D("1.0005"), D(10**6), D(10**6)))
        return out


@pytest.fixture
def market(monkeypatch):
    """Sustituye la red y la carga de velas del paso diario por un panel sintético."""
    state = SimpleNamespace(panel=_panel())

    async def load_panel(_sessions, **_kw):
        return state.panel

    monkeypatch.setattr(daily, "load_panel", load_panel)
    monkeypatch.setattr(daily, "BinanceMarket", lambda: FakeMarket(state.panel))
    return state


def _at(panel, hour: int, minute: int = 0, days_after: int = 1) -> datetime:
    """Hora UTC del día siguiente a la última vela del panel."""
    d = panel.days[-1] + timedelta(days=days_after)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC)


async def _count(sessions, model, *where) -> int:
    async with sessions() as s:
        return await s.scalar(select(func.count()).select_from(model).where(*where))


# --- Plazo de las 02:00 (T5) -------------------------------------------------------------


@pytest.mark.postgres
async def test_a_late_step_does_not_trade_and_the_day_is_lost(engine, market):
    """T5: pasadas las 02:00 UTC no se opera con precios de otro momento: el día queda
    «perdido», pero el análisis (recalculado, no en vivo) y el informe se guardan."""
    sessions = make_sessionmaker(engine)
    r = await run_daily(sessions, now=_at(market.panel, 10), sync=False)
    assert r.status == "tarde" and not r.steps
    async with sessions() as s:
        a = await s.get(CryptoAnalysis, r.analysis_id)
        days = list(await s.scalars(select(CryptoPaperDay)))
    assert a.computed_live is False and a.report_md
    assert [d.status for d in days] == ["perdido"]  # la continua arrancó, pero no opera
    assert await _count(sessions, CryptoPaperOrder) == 0
    assert "Hoy no opera: el paso llegó después de las 02:00 UTC" in r.text


@pytest.mark.postgres
async def test_a_late_step_trades_only_when_asked(engine, market):
    """T5: con --tarde se opera igual (el análisis sigue sin contar como emitido en vivo)."""
    sessions = make_sessionmaker(engine)
    r = await run_daily(sessions, now=_at(market.panel, 10), sync=False, late_ok=True)
    assert r.status == "ok" and r.steps and r.steps[0].status == "ok"
    async with sessions() as s:
        assert (await s.get(CryptoAnalysis, r.analysis_id)).computed_live is False
    assert await _count(sessions, CryptoPaperOrder, CryptoPaperOrder.status == "filled") > 0


# --- Datos incompletos (P4) --------------------------------------------------------------


@pytest.mark.postgres
async def test_incomplete_data_is_retried_and_only_the_last_try_is_stored(engine, market):
    """P4: con datos incompletos antes de las 02:00 no se guarda nada en vivo (se
    reintenta); el último intento guarda el análisis incompleto y el día «sin datos»; y un
    análisis completo posterior lo sustituye."""
    sessions = make_sessionmaker(engine)
    complete = market.panel
    market.panel = _panel(n=10)
    early = await run_daily(sessions, now=_at(complete, 0, 12), sync=False)
    assert early.status == "reintentar" and early.analysis_id is None
    assert await _count(sessions, CryptoAnalysis) == 0
    assert await _count(sessions, CryptoPaperRun) == 0  # ni siquiera arranca la continua
    last = await run_daily(sessions, now=_at(complete, 1, 50), sync=False, final=True)
    assert last.status == "sin_datos"
    async with sessions() as s:
        a = await s.get(CryptoAnalysis, last.analysis_id)
        days = list(await s.scalars(select(CryptoPaperDay)))
    assert a.computed_live and a.status == "datos_incompletos"
    assert [d.status for d in days] == ["sin_datos"]
    # Un análisis en vivo incompleto no decidió nada: uno completo lo sustituye (mismo id,
    # al que apunta el día de la cartera).
    again = await store(sessions, analyze(complete, complete.days[-1]), live=True)
    async with sessions() as s:
        a = await s.get(CryptoAnalysis, again)
    assert again == last.analysis_id and a.status == "ok" and a.computed_live
    # Y uno en vivo completo ya no se toca.
    assert await store(sessions, analyze(market.panel, complete.days[-1]), live=False) == again
    async with sessions() as s:
        assert (await s.get(CryptoAnalysis, again)).status == "ok"


# --- Cartera continua (P5) ---------------------------------------------------------------


@pytest.mark.postgres
async def test_a_stopped_continua_is_not_restarted(engine, market):
    """P5: la continua arranca sola una única vez por versión; parada, no vuelve."""
    sessions = make_sessionmaker(engine)
    first = await run_daily(sessions, now=_at(market.panel, 0, 12), sync=False)
    assert first.status == "ok"
    async with sessions() as s:
        run_id = await s.scalar(select(CryptoPaperRun.id))
    await set_status(sessions, run_id, "STOPPED", "parada a mano")
    await run_daily(sessions, now=_at(market.panel, 0, 12, days_after=2), sync=False)
    assert await _count(sessions, CryptoPaperRun) == 1


def test_stopping_the_continua_needs_an_explicit_kind_or_id():
    continua = SimpleNamespace(id=1, kind="continua")
    test = SimpleNamespace(id=2, kind="prueba")
    target, why = paper_target("stop", [continua])
    assert target is None and "--kind continua" in why
    assert paper_target("stop", [continua], kind="continua") == (continua, None)
    assert paper_target("stop", [continua], run_id=1) == (continua, None)
    assert paper_target("stop", [continua, test]) == (test, None)
    assert paper_target("pause", [continua]) == (continua, None)
    assert paper_target("resume", [], kind="prueba")[0] is None


# --- Medidas de riesgo del informe (P9) --------------------------------------------------


@pytest.mark.postgres
async def test_report_day_loss_and_drawdown_are_the_halt_measures(engine):
    """P9: «Pérdida de hoy» es 1 − W(t)/E(t−1) (la de la parada del 20 %) y la caída es
    1 − W(t)/máximo hasta t−1 (la de la parada del 50 %)."""
    sessions = make_sessionmaker(engine)
    day = date(2026, 10, 20)
    run_id = await start_run(sessions, kind="continua", start_day=day, limits=LIMITS)
    async with sessions.begin() as s:
        for k, equity, worst in ((-2, 120, 118), (-1, 100, 98), (0, 95, 85)):
            s.add(
                CryptoPaperSnapshot(
                    run_id=run_id,
                    book="portfolio",
                    day=day + timedelta(days=k),
                    equity_usd=D(equity),
                    worst_equity_usd=D(worst),
                    cash_usd=D(10),
                    positions_value_usd=D(equity - 10),
                    open_positions=5,
                )
            )
    info = await daily._paper_info(sessions, run_id, day, StepResult(run_id, day, "ok"))
    assert info.day_loss == pytest.approx(1 - 85 / 100)
    assert info.drawdown == pytest.approx(1 - 85 / 120)


# --- Servicio (P4 y P10) -----------------------------------------------------------------


class Stop(Exception):
    pass


class Clock:
    """Reloj falso: solo avanza con ``sleep``; se para tras ``limit`` esperas."""

    def __init__(self, now: datetime, limit: int = 6) -> None:
        self.now, self.sleeps, self.limit = now, [], limit

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) > self.limit:
            raise Stop
        self.now += timedelta(seconds=seconds)


def _fake_daily(calls: list, statuses):
    async def step(_sessions, *, now, say, final):
        calls.append((now, final))
        status = statuses(now, final)
        if isinstance(status, Exception):
            raise status
        return DailyResult(now.date() - timedelta(days=1), None, [], status, status)

    return step


async def _serve(clock: Clock, calls: list, statuses, done=None) -> None:
    async def is_done(_day: date) -> bool:
        return bool(done and done(clock.now))

    with pytest.raises(Stop):
        await run_forever(
            None,
            say=lambda _m: None,
            clock=clock,
            sleep=clock.sleep,
            daily=_fake_daily(calls, statuses),
            done=is_done,
        )


async def test_a_restart_inside_the_window_runs_the_pending_step_at_once():
    """P10: un (re)arranque entre las 00:10 y las 02:00 con el paso de hoy sin hacer lo da
    ya, sin esperar al día siguiente."""
    clock = Clock(datetime(2026, 10, 25, 0, 30, tzinfo=UTC), limit=2)
    calls: list = []
    await _serve(clock, calls, lambda _n, _f: "ok")
    assert calls[0] == (datetime(2026, 10, 25, 0, 30, tzinfo=UTC), False)
    assert clock.sleeps[0] == pytest.approx(90 * 60)  # hasta las 02:00
    assert calls[1][0] == datetime(2026, 10, 26, 0, 10, tzinfo=UTC)  # y al día siguiente


async def test_outside_the_window_or_with_the_step_done_it_waits_for_00_10():
    clock = Clock(datetime(2026, 10, 25, 3, 0, tzinfo=UTC), limit=1)
    calls: list = []
    await _serve(clock, calls, lambda _n, _f: "ok")
    assert clock.sleeps[0] == pytest.approx((21 * 60 + 10) * 60) and len(calls) == 1
    clock = Clock(datetime(2026, 10, 25, 0, 30, tzinfo=UTC), limit=0)
    calls = []
    await _serve(clock, calls, lambda _n, _f: "ok", done=lambda _n: True)
    assert not calls and clock.sleeps == [pytest.approx((23 * 60 + 40) * 60)]


async def test_incomplete_data_or_failures_are_retried_until_the_last_try():
    """P4: mientras el paso no sea completo se reintenta cada 10 minutos; el último
    intento (antes de las 02:00) es el «final» que guarda el día sin datos."""
    clock = Clock(datetime(2026, 10, 25, 1, 30, tzinfo=UTC), limit=2)
    calls: list = []

    def statuses(now, final):
        if now.minute == 40:
            return RuntimeError("Binance no responde")
        return "sin_datos" if final else "reintentar"

    await _serve(clock, calls, statuses)
    assert [(c.strftime("%H:%M"), final) for c, final in calls] == [
        ("01:30", False),
        ("01:40", False),
        ("01:50", True),
    ]

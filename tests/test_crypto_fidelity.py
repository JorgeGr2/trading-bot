"""Fidelidad: comparar las decisiones en vivo con la réplica del backtest."""

from __future__ import annotations

from datetime import timedelta

import pytest

from copybot.config import RiskLimits
from copybot.crypto.engine import build_views
from copybot.crypto.fidelity import check_run, compare, replay_decisions
from copybot.crypto.paper import start_run
from copybot.crypto.strategy import STRATEGY_V1
from copybot.db.models import CryptoPaperDay, CryptoPaperEvent, CryptoPaperOrder
from copybot.db.session import make_sessionmaker
from tests.test_crypto_engine import make_panel

LIMITS = RiskLimits.model_construct()


def _setup():
    panel = make_panel(drift=lambda k: 0.01 if k == 1 else 0.0)
    days = panel.days[130:200]
    return panel, days, build_views(panel, days, STRATEGY_V1)


def test_identical_decisions_pass_and_a_difference_is_red_until_explained():
    panel, days, views = _setup()
    replay = replay_decisions(panel, views, days, LIMITS)
    assert any(replay.values())
    live = {d: set(v) for d, v in replay.items()}
    assert compare(live, replay, days).ok
    day = next(d for d in days if replay.get(d))
    live[day] = set()  # en vivo no hizo lo que tocaba
    report = compare(live, replay, days)
    assert not report.ok and report.unexplained == [day]
    assert compare(live, replay, days, explained={day}).ok


def test_a_coin_blocked_live_is_also_skipped_in_the_replay():
    """Los filtros solo en vivo (diferencial, estado…) bloquean una compra: la réplica
    recibe los mismos bloqueos y pasa a la siguiente candidata, igual que en vivo."""
    panel, days, views = _setup()
    replay = replay_decisions(panel, views, days, LIMITS)
    day = next(d for d in days if any(side == "buy" for _, side, _ in replay.get(d, ())))
    symbol = next(sym for sym, side, _ in sorted(replay[day]) if side == "buy")
    blocked = replay_decisions(panel, views, days, LIMITS, blocked={day: frozenset({symbol})})
    assert (symbol, "buy", "entrada") not in blocked[day]
    assert {d: v for d, v in blocked.items() if d < day} == {
        d: v for d, v in replay.items() if d < day
    }


@pytest.mark.postgres
async def test_a_missed_live_day_does_not_break_the_replay(engine):
    """T2: con un día sin paso en vivo, la réplica recorre el calendario completo (lo
    decidido la víspera se ejecuta ese día) y solo compara los días con paso."""
    panel, days, views = _setup()
    full = replay_decisions(panel, views, days, LIMITS)
    # Un día perdido justo después de uno con compras: lo pendiente se ejecuta igual.
    missed = next(
        d
        for d in days[1:-1]
        if any(side == "buy" for _, side, _ in full.get(d - timedelta(days=1), ()))
    )
    # Lo que hizo la cartera en vivo: ese día no hubo paso ni decisión.
    live = replay_decisions(panel, {d: v for d, v in views.items() if d != missed}, days, LIMITS)
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="continua", start_day=days[0], limits=LIMITS)
    async with sessions.begin() as s:
        for day in days:
            if day == missed:
                continue
            s.add(CryptoPaperDay(run_id=run_id, day=day, status="ok"))
            for k, (symbol, side, purpose) in enumerate(sorted(live.get(day, ()))):
                s.add(
                    CryptoPaperOrder(
                        run_id=run_id,
                        book="portfolio",
                        day=day,
                        idempotency_key=f"{day}-{k}",
                        symbol=symbol,
                        side=side,
                        purpose=purpose,
                        status="filled",
                    )
                )
        # Una retirada (7 días sin cotizar o par fuera de TRADING) no sale de decide().
        s.add(
            CryptoPaperOrder(
                run_id=run_id,
                book="portfolio",
                day=days[-1],
                idempotency_key="retirada",
                symbol="C05USDT",
                side="sell",
                purpose="retirada",
                status="filled",
            )
        )
    report = await check_run(sessions, run_id, panel, views)
    assert report.ok and report.days_checked == len(days) - 1
    # Antes solo se replicaban los días con fila: lo pendiente del día perdido no se
    # ejecutaba nunca y todo lo posterior divergía (ROJO falso).
    ok_days = [d for d in days if d != missed]
    assert not compare(live, replay_decisions(panel, views, ok_days, LIMITS), ok_days).ok


@pytest.mark.postgres
async def test_check_run_replays_the_live_blocks(engine):
    """Un día con una candidata bloqueada en vivo no es una diferencia de fidelidad."""
    panel, days, views = _setup()
    replay = replay_decisions(panel, views, days, LIMITS)
    day = next(d for d in days if any(side == "buy" for _, side, _ in replay.get(d, ())))
    symbol = next(sym for sym, side, _ in sorted(replay[day]) if side == "buy")
    live = replay_decisions(panel, views, days, LIMITS, blocked={day: frozenset({symbol})})
    sessions = make_sessionmaker(engine)
    run_id = await start_run(sessions, kind="continua", start_day=days[0], limits=LIMITS)
    async with sessions.begin() as s:
        for d in days:
            s.add(CryptoPaperDay(run_id=run_id, day=d, status="ok"))
            for k, (sym, side, purpose) in enumerate(sorted(live.get(d, ()))):
                s.add(
                    CryptoPaperOrder(
                        run_id=run_id,
                        book="portfolio",
                        day=d,
                        idempotency_key=f"{d}-{k}",
                        symbol=sym,
                        side=side,
                        purpose=purpose,
                        status="filled",
                    )
                )
        s.add(
            CryptoPaperEvent(
                run_id=run_id,
                kind="bloqueo",
                details={"dia": day.isoformat(), "bloqueadas": {symbol: "bloqueada en vivo"}},
            )
        )
    assert (await check_run(sessions, run_id, panel, views)).ok

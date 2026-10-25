"""Decisión diaria de la regla v1 con vistas sintéticas (casos calculados a mano)."""

from __future__ import annotations

import random
from datetime import date

import pytest

from copybot.config import RiskLimits
from copybot.crypto.signals import CoinSignal
from copybot.crypto.strategy import (
    STRATEGY_V1,
    VARIANTS,
    BookView,
    DayView,
    Holding,
    Regime,
    decide,
    size_usd,
)
from copybot.crypto.universe import Member, Universe

DAY = date(2026, 10, 20)
LIMITS = RiskLimits()  # topes vigentes: 25 $, 8 posiciones, 10 % de reserva


def coin(symbol: str, rank: int, trend: int = 3, close: float = 10.0) -> CoinSignal:
    above = {20: trend >= 1, 50: trend >= 2, 100: trend >= 3}
    return CoinSignal(
        symbol, symbol[:-4], close, 0.03, {30: 0.1}, {30: 1.0}, above, 100 - rank, rank
    )


def view(coins: list[CoinSignal], *, favorable: bool = True, excluded=None, lows=None):
    members = [Member(c.symbol, c.base, 1e8, k) for k, c in enumerate(coins, 1)]
    close = {c.symbol: c.close for c in coins}
    low = {c.symbol: c.close for c in coins} | (lows or {})
    reg = Regime(100.0, 90.0 if favorable else 110.0, favorable)
    u = Universe(DAY, members, excluded or {})
    return DayView(DAY, u, sorted(coins, key=lambda c: c.rank), reg, close, low)


def book(cash=100.0, holdings=None, prev=100.0, peak=100.0, **kw) -> BookView:
    return BookView(cash, holdings or {}, prev, peak, 100.0, **kw)


def held(symbol: str, qty: float = 1.8, entry: float = 10.0) -> Holding:
    return Holding(symbol, qty, entry, date(2026, 10, 1), entry)


COINS = [coin(f"C{k}USDT", k) for k in range(1, 16)]


def test_sizes_within_the_hard_caps():
    assert size_usd(100, STRATEGY_V1, LIMITS, "dolares") == 18.0
    assert size_usd(140, STRATEGY_V1, LIMITS, "dolares") == 25.0  # tope por posición
    assert size_usd(60, STRATEGY_V1, LIMITS, "dolares") == 10.0
    assert size_usd(30, STRATEGY_V1, LIMITS, "dolares") == 4.0  # < 5 $: no se compra
    assert size_usd(1000, STRATEGY_V1, LIMITS, "porcentaje") == pytest.approx(180)


def test_buys_the_top_five_with_trend():
    coins = [coin("AUSDT", 1), coin("BUSDT", 2, trend=1), *COINS[2:]]
    d = decide(view(coins), book(), STRATEGY_V1, LIMITS)
    # B no tiene tendencia (1 de 3); el puesto 6 no entra aunque quede hueco.
    assert [o.symbol for o in d.buys] == ["AUSDT", "C3USDT", "C4USDT", "C5USDT"]
    assert all(o.usd == 18.0 for o in d.buys) and not d.sells


def test_rank_12_is_kept_and_13_is_sold():
    holdings = {"C12USDT": held("C12USDT"), "C13USDT": held("C13USDT")}
    d = decide(view(COINS), book(cash=64, holdings=holdings), STRATEGY_V1, LIMITS)
    assert [(o.symbol, o.purpose) for o in d.sells] == [("C13USDT", "puesto")]
    assert "se vende a partir del 13" in d.sells[0].reason


def test_trend_one_third_sells_and_stop_at_minus_25():
    coins = [coin("AUSDT", 1, trend=1), coin("BUSDT", 2, close=7.5), *COINS[2:]]
    holdings = {"AUSDT": held("AUSDT"), "BUSDT": held("BUSDT")}
    d = decide(view(coins), book(cash=64, holdings=holdings), STRATEGY_V1, LIMITS)
    assert {(o.symbol, o.purpose) for o in d.sells} == {
        ("AUSDT", "tendencia"),
        ("BUSDT", "stop"),
    }
    assert "AUSDT" not in {o.symbol for o in d.buys}  # sin recompra el mismo día


def test_bear_market_sells_everything_and_buys_nothing():
    holdings = {"C1USDT": held("C1USDT")}
    d = decide(view(COINS, favorable=False), book(cash=82, holdings=holdings), STRATEGY_V1, LIMITS)
    assert [(o.symbol, o.purpose) for o in d.sells] == [("C1USDT", "regimen")]
    assert not d.buys and "media de 100 días" in d.sells[0].reason
    # Sin filtro de mercado (variante V2) se queda y compra.
    d2 = decide(
        view(COINS, favorable=False), book(cash=82, holdings=holdings), VARIANTS["V2"], LIMITS
    )
    assert not d2.sells and len(d2.buys) == 4


def test_out_of_universe_is_sold_with_its_reason():
    holdings = {"XUSDT": held("XUSDT")}
    v = view(COINS, excluded={"XUSDT": "poco volumen"})
    v.close["XUSDT"] = 10.0
    d = decide(v, book(cash=82, holdings=holdings), STRATEGY_V1, LIMITS)
    assert d.sells[0].purpose == "fuera_universo" and "poco volumen" in d.sells[0].reason


def test_daily_halt_blocks_buys_but_not_sells():
    holdings = {"C13USDT": held("C13USDT", qty=10)}  # vale 100 $ al cierre
    lows = {"C13USDT": 7.5}  # en el peor momento del día valía 75 $: −25 %
    v = view(COINS, lows=lows)
    d = decide(v, book(cash=0, holdings=holdings, prev=100, peak=100), STRATEGY_V1, LIMITS)
    assert d.halts and d.halts[0][0] == "daily_loss"
    assert [o.symbol for o in d.sells] == ["C13USDT"] and not d.buys


def test_halted_risk_and_paused_do_not_buy():
    for kw in ({"halted_risk": True}, {"paused": True}):
        d = decide(view(COINS), book(**kw), STRATEGY_V1, LIMITS)
        assert not d.buys


def test_blocked_in_live_is_skipped_without_changing_ranks():
    d = decide(view(COINS), book(blocked=frozenset({"C1USDT"})), STRATEGY_V1, LIMITS)
    assert [o.symbol for o in d.buys] == ["C2USDT", "C3USDT", "C4USDT", "C5USDT"]


def test_variant_v1_holds_only_the_number_one():
    p = VARIANTS["V1"]
    d = decide(view(COINS), book(), p, LIMITS, mode="porcentaje")
    assert [o.symbol for o in d.buys] == ["C1USDT"] and d.buys[0].usd == pytest.approx(90)
    d2 = decide(view(COINS), book(cash=10, holdings={"C2USDT": held("C2USDT", qty=9)}), p, LIMITS)
    assert [o.symbol for o in d2.sells] == ["C2USDT"]


def test_tightened_limits_reduce_positions():
    tight = LIMITS.model_copy(update={"max_open_positions": 3})
    d = decide(view(COINS), book(), STRATEGY_V1, tight)
    assert len(d.buys) == 3


def test_random_books_never_break_the_caps():
    rng = random.Random(7)  # noqa: S311 (prueba, no criptografía)
    for _ in range(2000):
        n = rng.randint(0, 5)
        syms = rng.sample([c.symbol for c in COINS], n)
        holdings = {s: held(s, qty=rng.uniform(0.1, 3), entry=rng.uniform(5, 20)) for s in syms}
        cash = rng.uniform(0, 100)
        b = book(cash=cash, holdings=holdings, prev=100, peak=rng.uniform(100, 150))
        d = decide(view(COINS), b, STRATEGY_V1, LIMITS)
        remaining = len(holdings) - len(d.sells)
        assert remaining + len(d.buys) <= min(STRATEGY_V1.n_positions, LIMITS.max_open_positions)
        spent = sum(o.usd for o in d.buys)
        proceeds = sum(holdings[o.symbol].qty * 10.0 * (1 - 0.005) for o in d.sells)
        assert spent <= cash + proceeds + 1e-9
        assert all(o.usd <= float(LIMITS.max_position_usd) for o in d.buys)
        if d.buys:
            assert cash + proceeds - spent >= 10 - 1e-9  # la reserva del 10 % del capital


def test_percent_mode_keeps_the_same_reserve_as_the_null():
    """Curva A y nulo aleatorio con la misma regla de huecos y reserva (el 10 % de E en
    modo porcentaje, apartado 6.2): con 25 $ de efectivo y E = 100 $ no cabe una compra de
    18 $ sin tocar la reserva de 10 $; con 30 $ (E = 105 $) cabe una."""
    from copybot.crypto.engine import random_chooser

    holdings = {s: held(s, qty=2.5) for s in ("C10USDT", "C11USDT", "C12USDT")}  # 75 $
    for cash, buys in ((25.0, 0), (30.0, 1), (100.0, 2)):
        b = book(cash=cash, holdings=holdings, prev=cash + 75, peak=cash + 75)
        d = decide(view(COINS), b, STRATEGY_V1, LIMITS, mode="porcentaje")
        assert len(d.buys) == buys
        null = random_chooser(1, [7], STRATEGY_V1, LIMITS)(view(COINS), d, b)
        assert len(null.buys) == buys


def test_pending_orders_count_as_held():
    """Órdenes decididas y aún sin ejecutar (un día de retraso): las compras ocupan hueco
    y efectivo, y lo que ya se está vendiendo no se vuelve a vender."""
    holdings = {"C13USDT": held("C13USDT")}  # puesto 13: toca venderla
    pending = {s: 18.0 for s in ("C7USDT", "C8USDT", "C9USDT")}
    b = book(cash=100, holdings=holdings, pending_buys=pending)
    d = decide(view(COINS), b, STRATEGY_V1, LIMITS)
    assert [o.symbol for o in d.sells] == ["C13USDT"]
    assert [o.symbol for o in d.buys] == ["C1USDT", "C2USDT"]  # 5 − 3 pendientes
    # Una venta pendiente cuenta como vendida (hueco y efectivo), pero no se repite.
    b2 = book(cash=100, holdings=holdings, pending_sells=frozenset({"C13USDT"}))
    d2 = decide(view(COINS), b2, STRATEGY_V1, LIMITS)
    same_day = decide(view(COINS), book(cash=100, holdings=holdings), STRATEGY_V1, LIMITS)
    assert not d2.sells and d2.buys == same_day.buys and len(d2.buys) == 4


def test_params_are_frozen():
    assert STRATEGY_V1.params_hash() == STRATEGY_V1.params_hash()
    assert VARIANTS["V2"].params_hash() != STRATEGY_V1.params_hash()
    with pytest.raises(AttributeError):
        STRATEGY_V1.n_positions = 3  # type: ignore[misc]

"""Backtest del copy-trading con precios retrasados de mentira (cifras comprobables)."""

from __future__ import annotations

from decimal import Decimal

from copybot.backtest.copy import (
    PORTFOLIO,
    SIGNALS,
    CopyBacktest,
    CopyParams,
    Quote,
    TraderEvent,
)
from copybot.config import RiskLimits

D = Decimal
T0 = 1_791_000_000
W = "Trader1111111111111111111111111111111111111"
CONSERVATIVE = RiskLimits(
    max_position_usd=10,
    max_open_positions=5,
    min_cash_reserve_pct=20,
    max_daily_loss_pct=10,
    max_drawdown_pct=25,
)
# Sin costes: cifras limpias para comprobar la lógica.
FREE = CopyParams(
    size_usd=D(10),
    impact_pct=D(0),
    mev_bps=0,
    base_fee_lamports=0,
    priority_fee_lamports=0,
    rent_lamports=0,
)


class Prices:
    """Precio retrasado por (mint, instante); ``None`` = nadie operó."""

    def __init__(self) -> None:
        # Sin clave: no está en caché. Con ``None``: nadie operó el token.
        self.at: dict[tuple[str, int], D | None] = {}
        self.asked: list[tuple[str, int, tuple[int, int]]] = []

    async def quote(self, mint, at, window):
        self.asked.append((mint, at, window))
        if (mint, at) not in self.at:
            return None
        return Quote(self.at[(mint, at)], D(200))


def ev(kind: str, at: int, amount: str, usd: str, mint: str = "M1", wallet: str = W, n=0):
    return TraderEvent(wallet, mint, kind, D(amount), D(usd), None, T0 + at, n)


async def run(events, prices, params=FREE, limits=CONSERVATIVE, end=10 * 86400):
    bt = CopyBacktest(prices, params, limits)
    return await bt.run(events, T0, T0 + end)


async def test_copy_buys_late_and_sells_with_the_trader():
    prices = Prices()
    prices.at[("M1", T0 + 100)] = D("0.012")  # 4-8 s después ya está un 20 % más caro
    prices.at[("M1", T0 + 900)] = D("0.018")
    events = [ev("buy", 100, "1000", "10"), ev("sell", 900, "1000", "20")]
    result = await run(events, prices)
    (pos,) = result.book(SIGNALS)
    assert pos.status == "closed" and pos.exits[0].reason == "copia"
    assert pos.entry_slippage_pct == 20  # pagamos 0,012 frente a 0,010 del trader
    assert pos.proceeds_usd == D(10) / D("0.012") * D("0.018")  # 833,3 tokens × 0,018 = 15
    assert pos.pnl_usd == 5
    assert pos.theo_pnl_usd == 10  # la copia teórica: 1.000 tokens de 0,01 a 0,02
    assert prices.asked[0] == ("M1", T0 + 100, (4, 8))


async def test_costs_are_applied_on_both_sides():
    prices = Prices()
    prices.at[("M1", T0)] = D("0.01")
    prices.at[("M1", T0 + 60)] = D("0.01")
    params = CopyParams(size_usd=D(10), impact_pct=D(1), mev_bps=50, rent_lamports=0)
    result = await run([ev("buy", 0, "1000", "10"), ev("sell", 60, "1000", "10")], prices, params)
    (pos,) = result.book(SIGNALS)
    fee = D(105_000) / D(10**9) * 200  # 0,021 $ por transacción
    tokens = (D(10) - fee) / (D("0.01") * D("1.01")) * D("0.995")
    expected = tokens * D("0.01") * D("0.99") * D("0.995") - fee
    assert pos.proceeds_usd == expected
    assert D("-0.4") < pos.pnl_usd < D("-0.3")  # ~3,5 % de ida y vuelta sin moverse el precio


async def test_partial_sell_uses_the_trader_balance():
    prices = Prices()
    for t in (0, 50, 80):
        prices.at[("M1", T0 + t)] = D("0.01")
    events = [
        ev("buy", 0, "1000", "10"),
        ev("sell", 50, "250", "2.5"),  # vende la cuarta parte
        ev("sell", 80, "750", "7.5"),
    ]
    result = await run(events, prices)
    (pos,) = result.book(SIGNALS)
    assert [round(e.tokens) for e in pos.exits] == [250, 750]
    assert pos.status == "closed"


async def test_max_hold_and_write_off():
    prices = Prices()
    prices.at[("M1", T0)] = D("0.01")
    prices.at[("M1", T0 + 48 * 3600)] = D("0.005")  # a las 48 h vale la mitad
    prices.at[("M2", T0 + 10)] = D("0.01")
    prices.at[("M2", T0 + 10 + 48 * 3600)] = None  # a las 48 h nadie lo opera → perdido
    prices.at[("M3", T0 + 20)] = D("0.01")  # M3: su precio a las 48 h no se consultó
    events = [
        ev("buy", 0, "1000", "10"),
        ev("buy", 10, "1000", "10", mint="M2", n=1),
        ev("buy", 20, "1000", "10", mint="M3", n=2),
    ]
    result = await run(events, prices, end=5 * 86400)
    by_mint = {p.mint: p for p in result.book(SIGNALS)}
    assert by_mint["M1"].exits[0].reason == "max_hold" and by_mint["M1"].pnl_usd == -5
    assert by_mint["M2"].status == "written_off" and by_mint["M2"].pnl_usd == -10
    # No se inventa una pérdida total: se vende al último precio visto y se cuenta.
    assert by_mint["M3"].status == "closed" and by_mint["M3"].pnl_usd == 0
    assert result.unpriced_exits == 2  # en los dos libros


async def test_stop_loss_on_an_observed_price():
    prices = Prices()
    prices.at[("M1", T0)] = D("0.01")
    prices.at[("M1", T0 + 300)] = D("0.004")
    events = [
        ev("buy", 0, "1000", "10"),
        # Otro trader seguido compra el mismo token a 0,004: la posición pierde un 60 %.
        ev("buy", 300, "1000", "4", wallet="Other", n=1),
    ]
    result = await run(events, prices)
    first = next(p for p in result.book(SIGNALS) if p.wallet == W)
    assert first.exits[0].reason == "stop_loss" and first.pnl_usd == -6


async def test_portfolio_limits_and_signals_book():
    prices = Prices()
    events = []
    for i in range(7):
        prices.at[(f"M{i}", T0 + i)] = D("0.01")
        events.append(ev("buy", i, "1000", "10", mint=f"M{i}", n=i))
    result = await run(events, prices, end=3600)
    assert len(result.book(SIGNALS)) == 7
    assert len(result.book(PORTFOLIO)) == 5  # máximo 5 posiciones (y 20 % de reserva)
    assert result.skipped == {"cartera: ya hay N posiciones abiertas (máximo N)": 2}


async def test_risk_halt_stops_the_portfolio_only():
    prices = Prices()
    events = []
    for i in range(3):
        prices.at[(f"M{i}", T0 + i)] = D("0.01")
        events.append(ev("buy", i, "1000", "10", mint=f"M{i}", n=i))
    # Los tres tokens se hunden (precios observados de otros compradores).
    for i in range(3):
        prices.at[(f"M{i}", T0 + 100 + i)] = D("0.0001")
        events.append(ev("buy", 100 + i, "1000", "0.1", mint=f"M{i}", wallet="Other", n=10 + i))
    prices.at[("M9", T0 + 200)] = D("0.01")
    events.append(ev("buy", 200, "1000", "10", mint="M9", n=20))
    params = CopyParams(**{**FREE.__dict__, "stop_loss_pct": D(100)})
    result = await run(events, prices, params, end=3600)
    assert result.halts and "pérdida del día" in result.halts[0][1]  # salta antes que la caída
    assert "M9" not in {p.mint for p in result.book(PORTFOLIO)}
    assert "M9" in {p.mint for p in result.book(SIGNALS)}


async def test_missing_prices_are_counted_not_invented():
    prices = Prices()
    result = await run([ev("buy", 0, "1000", "10")], prices)
    assert result.positions == [] and result.skipped == {"sin precio en caché": 1}


async def test_same_inputs_same_result():
    prices = Prices()
    prices.at[("M1", T0)] = D("0.011")
    prices.at[("M1", T0 + 99)] = D("0.017")
    events = [ev("buy", 0, "1000", "10"), ev("sell", 99, "1000", "18")]
    a = await run(events, prices)
    b = await run(list(reversed(events)), prices)
    assert [(p.book, p.pnl_usd) for p in a.positions] == [(p.book, p.pnl_usd) for p in b.positions]


async def test_stop_loss_uses_the_cost_of_what_is_left():
    prices = Prices()
    prices.at[("M1", T0)] = D("0.01")
    prices.at[("M1", T0 + 50)] = D("0.01")
    prices.at[("M1", T0 + 90)] = D("0.015")
    events = [
        ev("buy", 0, "1000", "10"),
        ev("sell", 50, "750", "7.5"),  # vende tres cuartas partes sin ganar ni perder
        ev("buy", 90, "1000", "15", wallet="Other", n=2),  # luego sube un 50 %
    ]
    result = await run(events, prices)
    pos = next(p for p in result.book(SIGNALS) if p.wallet == W)
    # El resto no salta por stop-loss al subir (antes contaba el coste de lo vendido).
    assert [e.reason for e in pos.exits] == ["copia", "max_hold"]
    assert pos.exits[0].tokens == 750


async def test_same_second_events_keep_the_database_order():
    prices = Prices()
    prices.at[("M1", T0)] = D("0.01")
    # Compra y venta en el mismo segundo: el orden lo da la carga (bloque e índice).
    events = [ev("buy", 0, "1000", "10", n=0), ev("sell", 0, "1000", "10", n=1)]
    result = await run(events, prices)
    (pos,) = result.book(SIGNALS)
    assert pos.exits[0].reason == "copia"


async def test_daily_loss_counts_from_before_the_first_event_of_the_day():
    day = 86400
    prices = Prices()
    events = []
    for i in range(2):
        prices.at[(f"M{i}", T0 + i)] = D("0.01")
        events.append(ev("buy", i, "1000", "10", mint=f"M{i}", n=i))
    # Al día siguiente, el primer evento es una caída a la mitad de los dos tokens.
    for i in range(2):
        prices.at[(f"M{i}", T0 + day + i)] = D("0.005")
        events.append(ev("buy", day + i, "1000", "5", mint=f"M{i}", wallet="Other", n=10 + i))
    params = CopyParams(**{**FREE.__dict__, "stop_loss_pct": D(100)})
    result = await run(events, prices, params, end=2 * day)
    assert result.halts and "pérdida del día" in result.halts[0][1]
    assert result.equity_curve[0] == (T0, D(100))  # la curva empieza en el capital inicial


async def test_cost_multiplier_scales_network_fees_too():
    prices = Prices()
    prices.at[("M1", T0)] = D("0.01")
    base = CopyParams(size_usd=D(10), impact_pct=D(0), mev_bps=0, rent_lamports=0)
    double = CopyParams(**{**base.__dict__, "cost_multiplier": D(2)})
    a = (await run([ev("buy", 0, "1000", "10")], prices, base)).book(SIGNALS)[0]
    b = (await run([ev("buy", 0, "1000", "10")], prices, double)).book(SIGNALS)[0]
    fee = D(105_000) / D(10**9) * 200
    assert a.exits[0].tokens == (10 - fee) / D("0.01")
    assert b.exits[0].tokens == (10 - 2 * fee) / D("0.01")


async def test_buys_are_copied_only_while_the_trader_is_selected():
    from datetime import UTC, datetime

    from copybot.backtest.data import Selection, only_while_selected
    from copybot.backtest.report import Round, Scenario, render

    prices = Prices()
    for t in (0, 100, 200):
        prices.at[("M1", T0 + t)] = D("0.01")
    events = [
        ev("buy", 0, "1000", "10", n=0),  # antes de elegirlo: solo saldo
        ev("buy", 100, "1000", "10", n=1),  # elegido: se copia
        ev("sell", 200, "2000", "20", n=2),  # vende todo: cerramos con él
    ]
    events = only_while_selected(events, {W: [(T0 + 50, T0 + 150)]})
    result = await run(events, prices)
    (pos,) = result.book(SIGNALS)
    assert pos.opened_at == T0 + 100 and pos.status == "closed"
    when = datetime.fromtimestamp(T0 + 50, UTC)
    text = render(
        [Scenario("prueba", result, CONSERVATIVE)],
        [Round(when, [Selection(W, D(70), True, 30, D("0.1"))], [W])],
        out_of_sample=True,
        select_window=21,
        universe="actividad",
        lookups=0,
        missing=0,
        seed=7,
    )
    assert "| prueba | señales | 1 |" in text and "fuera de muestra" in text
    assert text == render(
        [Scenario("prueba", result, CONSERVATIVE)],
        [Round(when, [Selection(W, D(70), True, 30, D("0.1"))], [W])],
        out_of_sample=True,
        select_window=21,
        universe="actividad",
        lookups=0,
        missing=0,
        seed=7,
    )  # mismo informe con los mismos datos y la misma semilla

"""Componentes puros del simulador: costes, riesgo, filtros, cotizaciones y ejecutor."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from copybot.config import RiskLimits
from copybot.sim.costs import CostModel
from copybot.sim.executor import SimExecutor, to_raw
from copybot.sim.params import SafetyParams
from copybot.sim.quotes import JUPITER_QUOTE_URL, JupiterQuotes, Quote, QuoteError
from copybot.sim.risk import BookState, entry_rejections, halt_reasons
from copybot.sim.safety import (
    MarketCheck,
    MintInfo,
    market_rejections,
    mint_rejections,
    parse_mint_account,
)
from copybot.solana.constants import USDC_MINT

D = Decimal
NOW = datetime(2026, 10, 18, 20, tzinfo=UTC)
MINTS = Path(__file__).parent / "fixtures" / "mints"
LIMITS = RiskLimits()
SAFETY = SafetyParams(_env_file=None)
MEME = "GTBxUiw6wJdmmkCGZgRHLyYxqu1vG4KtRpeox6yDpump"


def mint_fixture(mint: str) -> MintInfo | None:
    return parse_mint_account(mint, json.loads((MINTS / f"{mint}.json").read_text()))


# --- Costes ------------------------------------------------------------------------


def test_costs_by_hand():
    costs = CostModel(
        sol_usd=D(100), base_fee_lamports=5_000, priority_fee_lamports=100_000, mev_haircut_bps=50
    )
    assert costs.tx_fee_usd() == D("0.0105")  # 105.000 lamports a 100 $/SOL
    assert costs.after_mev(D(1000)) == D(995)
    assert costs.after_transfer_fee(D(1000), 100) == D(990)


# --- Riesgo --------------------------------------------------------------------------


def book(**kw) -> BookState:
    base = dict(
        initial_usd=D(100),
        cash_usd=D(100),
        reserved_usd=D(0),
        positions_value_usd=D(0),
        open_positions=0,
        open_mints=frozenset(),
        peak_equity_usd=D(100),
        day_start_equity_usd=D(100),
    )
    base.update(kw)
    return BookState(**base)


def test_entry_limits():
    assert entry_rejections(book(), "M", D(10), LIMITS) == []
    full = entry_rejections(book(open_positions=5), "M", D(10), LIMITS)
    assert any("5 posiciones" in r for r in full)
    assert any(
        "este token" in r
        for r in entry_rejections(book(open_mints=frozenset({"M"})), "M", D(10), LIMITS)
    )
    # Reserva del 20 %: con 29 $ en caja, una orden de 10 $ dejaría 19 $ < 20 $.
    assert any("reserva" in r for r in entry_rejections(book(cash_usd=D(29)), "M", D(10), LIMITS))
    assert entry_rejections(book(cash_usd=D(30)), "M", D(10), LIMITS) == []


def test_halts_by_hand():
    assert halt_reasons(book(), LIMITS) == []
    # Pico 100 $, ahora 75 $: caída del 25 % → para.
    kinds = {k for k, _ in halt_reasons(book(cash_usd=D(75), day_start_equity_usd=D(80)), LIMITS)}
    assert kinds == {"max_drawdown"}
    # Empezó el día con 90 $ y lleva 81 $: −10 % en el día → para.
    kinds = {k for k, _ in halt_reasons(book(cash_usd=D(81), day_start_equity_usd=D(90)), LIMITS)}
    assert kinds == {"daily_loss"}
    # Las posiciones y lo reservado cuentan como patrimonio.
    assert (
        halt_reasons(book(cash_usd=D(60), reserved_usd=D(10), positions_value_usd=D(30)), LIMITS)
        == []
    )


# --- Filtros de seguridad -----------------------------------------------------------


def test_real_clean_token_passes():
    info = mint_fixture(MEME)
    assert info is not None and info.program == "spl-token-2022" and info.decimals == 6
    assert mint_rejections(info, SAFETY) == []


def test_real_token_with_mutable_transfer_fee_is_rejected():
    info = mint_fixture("DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP")
    assert info.transfer_fee_bps == 100 and info.transfer_fee_mutable
    assert mint_rejections(info, SAFETY) == ["el creador puede subir la comisión de transferencia"]
    relaxed = SafetyParams(_env_file=None, reject_mutable_transfer_fee=False)
    assert mint_rejections(info, relaxed) == []


def test_real_usdc_has_authorities():
    info = mint_fixture(USDC_MINT)
    reasons = mint_rejections(info, SAFETY)
    assert any("emitir" in r for r in reasons) and any("congelar" in r for r in reasons)


@pytest.mark.parametrize(
    ("extensions", "phrase"),
    [
        ({"transferHook": {"programId": "Hook111"}}, "transfer hook"),
        ({"permanentDelegate": {"delegate": "Del111"}}, "delegado permanente"),
        ({"nonTransferable": {}}, "no se pueden transferir"),
        ({"pausableConfig": {}}, "pausar"),
        ({"defaultAccountState": {"accountState": "frozen"}}, "congeladas"),
        ({"transferFeeConfig": {"newerTransferFee": {"transferFeeBasisPoints": 300}}}, "3.00 %"),
        # Sin programa ni comisión hoy, pero el creador puede activarlos después.
        ({"transferHook": {"authority": "Auth111", "programId": None}}, "transfer hook"),
        (
            {
                "transferFeeConfig": {
                    "transferFeeConfigAuthority": "Auth111",
                    "newerTransferFee": {"transferFeeBasisPoints": 0},
                }
            },
            "subir la comisión",
        ),
    ],
)
def test_dangerous_extensions(extensions, phrase):
    info = MintInfo("M", "spl-token-2022", 6, None, None, extensions)
    assert any(phrase in r for r in mint_rejections(info, SAFETY))


def test_inactive_extensions_are_fine():
    info = MintInfo(
        "M",
        "spl-token-2022",
        6,
        None,
        None,
        {"transferHook": {"programId": None}, "permanentDelegate": {"delegate": None}},
    )
    assert mint_rejections(info, SAFETY) == []
    assert mint_rejections(None, SAFETY) == ["no se pudo leer la cuenta del token"]


def test_market_filters():
    ok = MarketCheck(liquidity_usd=D(50_000), buy_price_impact_pct=D(1), roundtrip_loss_pct=D(4))
    assert market_rejections(ok, SAFETY) == []
    bad = MarketCheck(liquidity_usd=D(5_000), buy_price_impact_pct=D(5), roundtrip_loss_pct=D(30))
    assert len(market_rejections(bad, SAFETY)) == 3
    unknown = MarketCheck(None, None, None)
    assert len(market_rejections(unknown, SAFETY)) == 3


# --- Cotizaciones ---------------------------------------------------------------------


class Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@respx.mock
async def test_quote_parses_retries_and_detects_no_route():
    respx.get(JUPITER_QUOTE_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(
                200,
                json={
                    "inAmount": "10000000",
                    "outAmount": "1505694766",
                    "priceImpactPct": "0.0098",
                    "routePlan": [{"swapInfo": {"label": "Pump.fun Amm"}, "percent": 100}],
                },
            ),
            httpx.Response(
                400,
                json={"error": "Could not find any route", "errorCode": "COULD_NOT_FIND_ANY_ROUTE"},
            ),
        ]
    )
    sleeper = Sleeper()
    client = JupiterQuotes(rate_per_second=1000, sleep=sleeper, clock=lambda: NOW)
    q = await client.quote(USDC_MINT, MEME, 10_000_000, 300)
    assert (q.in_raw, q.out_raw, q.route) == (10_000_000, 1_505_694_766, ("Pump.fun Amm",))
    assert q.price_impact_pct == D("0.98")
    assert 2.0 in sleeper.delays
    assert await client.quote(USDC_MINT, MEME, 10_000_000, 300) is None
    assert await client.quote(USDC_MINT, MEME, 0, 300) is None  # sin pedir nada
    await client.aclose()


# --- Ejecutor simulado ----------------------------------------------------------------


class FakeQuotes:
    """Devuelve las cotizaciones de una lista, en orden."""

    def __init__(self, outs: list[int | None]) -> None:
        self.outs = list(outs)
        self.asked: list[tuple[str, str, int]] = []

    async def quote(self, input_mint, output_mint, amount_raw, slippage_bps):
        self.asked.append((input_mint, output_mint, amount_raw))
        out = self.outs.pop(0)
        if out is None:
            return None
        return Quote(input_mint, output_mint, amount_raw, out, D("0.5"), ("X",), NOW)


COSTS = CostModel(
    sol_usd=D(100), base_fee_lamports=5_000, priority_fee_lamports=100_000, mev_haircut_bps=50
)


def t2(out: int) -> Quote:
    return Quote(USDC_MINT, MEME, 10_000_000, out, D("0.5"), ("X",), NOW)


async def test_buy_fills_after_latency_with_mev_and_transfer_fee():
    sleeper = Sleeper()
    ex = SimExecutor(
        FakeQuotes([1_000_000_000]), latency_ms=1500, slippage_tolerance_bps=300, sleep=sleeper
    )
    r = await ex.buy(MEME, 6, D(10), 100, t2(1_000_000_000), COSTS)
    assert r.ok and sleeper.delays == [1.5]
    # 1.000 tokens − 0,5 % MEV − 1 % de comisión de transferencia = 985,05
    assert r.token_amount == D("985.05")
    assert (r.usd_amount, r.fees_usd) == (D(10), D("0.0105"))


async def test_buy_fails_on_slippage_and_still_pays_the_fee():
    # En t3 se recibirían 960 en vez de 1.000: −4 % > 3 % de tolerancia.
    ex = SimExecutor(
        FakeQuotes([960_000_000]), latency_ms=0, slippage_tolerance_bps=300, sleep=Sleeper()
    )
    r = await ex.buy(MEME, 6, D(10), 0, t2(1_000_000_000), COSTS)
    assert not r.ok and "slippage" in r.reason and "4.00%" in r.reason
    assert r.fees_usd == D("0.0105") and r.token_amount == 0
    # −2,4 % sigue dentro de la tolerancia.
    ok = SimExecutor(
        FakeQuotes([976_000_000]), latency_ms=0, slippage_tolerance_bps=300, sleep=Sleeper()
    )
    assert (await ok.buy(MEME, 6, D(10), 0, t2(1_000_000_000), COSTS)).ok


async def test_sell_without_route_is_marked_unsellable():
    ex = SimExecutor(FakeQuotes([None]), latency_ms=0, slippage_tolerance_bps=300, sleep=Sleeper())
    r = await ex.sell(MEME, 6, D(500), 0, COSTS)
    assert not r.ok and r.no_route and r.fees_usd == 0  # no se llegó a enviar nada


async def test_sell_sends_amount_net_of_transfer_fee():
    quotes = FakeQuotes([6_000_000, 6_000_000])
    ex = SimExecutor(quotes, latency_ms=0, slippage_tolerance_bps=300, sleep=Sleeper())
    r = await ex.sell(MEME, 6, D(500), 100, COSTS)
    assert r.ok and r.token_amount == D(500)
    assert quotes.asked[0] == (MEME, USDC_MINT, to_raw(D(495), 6))  # 500 − 1 %
    assert r.usd_amount == D("5.97")  # 6 $ − 0,5 % de MEV


# --- Respuestas reales de Jupiter grabadas (19-oct-2026, token Kimchi) ------------------

JUP = Path(__file__).parent / "fixtures" / "jupiter"
KIMCHI = "Am8iBffqgaedfdK5XhtorbcRrKzwH9nyL3ySikQfpump"


@respx.mock
async def test_real_jupiter_round_trip_passes_market_filters():
    buy = json.loads((JUP / "quote_usdc_kimchi.json").read_text())
    sell = json.loads((JUP / "quote_kimchi_usdc.json").read_text())
    respx.get(JUPITER_QUOTE_URL).mock(
        side_effect=[httpx.Response(200, json=buy), httpx.Response(200, json=sell)]
    )
    client = JupiterQuotes(rate_per_second=1000, sleep=Sleeper(), clock=lambda: NOW)
    t2 = await client.quote(USDC_MINT, KIMCHI, 9_600_000, 300)
    back = await client.quote(KIMCHI, USDC_MINT, t2.out_raw, 300)
    await client.aclose()
    assert t2.route == ("GoonFi V2", "Pump.fun Amm") and t2.out_raw == 2_238_905_755
    assert back.out_raw == 9_474_510
    loss = (1 - D(back.out_raw) / D(t2.in_raw)) * 100  # 1,31 % de ida y vuelta
    check = MarketCheck(D(124_929), t2.price_impact_pct, loss)
    assert market_rejections(check, SafetyParams()) == []
    assert D("1.3") < loss < D("1.4")


@respx.mock
async def test_real_jupiter_price_gives_sol_price_and_liquidity():
    from copybot.ingest.prices import JUPITER_PRICE_URL, JupiterPrices
    from copybot.sim.market import LiveMarket
    from copybot.solana.constants import WSOL_MINT

    data = json.loads((JUP / "price_kimchi_sol.json").read_text())
    respx.get(JUPITER_PRICE_URL).mock(return_value=httpx.Response(200, json=data))
    prices = JupiterPrices(sleep=Sleeper())
    market = LiveMarket(prices, rpc=None)  # type: ignore[arg-type]
    assert await market.sol_usd() == D(str(data[WSOL_MINT]["usdPrice"]))
    assert int(await market.liquidity_usd(KIMCHI)) == 124_929
    await prices.aclose()


async def test_mev_never_takes_more_than_the_slippage_floor():
    # t3 justo en el mínimo (−3 %): el sandwich no puede llevarse nada más sin hacer
    # fallar la transacción, así que se recibe el mínimo.
    ex = SimExecutor(
        FakeQuotes([970_000_000, 980_000_000]),
        latency_ms=0,
        slippage_tolerance_bps=300,
        sleep=Sleeper(),
    )
    r = await ex.buy(MEME, 6, D(10), 0, t2(1_000_000_000), COSTS)
    assert r.ok and r.token_amount == D(970)
    # Con más margen, el MEV se lleva su 0,5 % (980 → 975,1), por encima del mínimo.
    r = await ex.buy(MEME, 6, D(10), 0, t2(1_000_000_000), COSTS)
    assert r.ok and r.token_amount == D("975.1")
    # Y con una tolerancia menor que el MEV no fallan todas las órdenes.
    tight = SimExecutor(
        FakeQuotes([1_000_000_000]), latency_ms=0, slippage_tolerance_bps=10, sleep=Sleeper()
    )
    r = await tight.buy(MEME, 6, D(10), 0, t2(1_000_000_000), COSTS)
    assert r.ok and r.token_amount == D(999)


async def test_jupiter_error_at_t3_fails_and_pays_the_fee():
    class Broken:
        async def quote(self, *args):
            raise QuoteError("503")

    ex = SimExecutor(Broken(), latency_ms=0, slippage_tolerance_bps=300, sleep=Sleeper())
    r = await ex.buy(MEME, 6, D(10), 0, t2(1_000_000_000), COSTS)
    assert not r.ok and "ya se habría enviado" in r.reason and r.fees_usd == COSTS.tx_fee_usd()


@respx.mock
async def test_non_json_jupiter_answer_is_a_quote_error():
    respx.get(JUPITER_QUOTE_URL).mock(
        return_value=httpx.Response(403, text="<html>Attention Required</html>")
    )
    client = JupiterQuotes(rate_per_second=1000, sleep=Sleeper(), clock=lambda: NOW)
    with pytest.raises(QuoteError, match="sin JSON"):
        await client.quote(USDC_MINT, MEME, 10_000_000, 300)
    respx.get(JUPITER_QUOTE_URL).mock(return_value=httpx.Response(200, json={"foo": 1}))
    with pytest.raises(QuoteError, match="inesperada"):
        await client.quote(USDC_MINT, MEME, 10_000_000, 300)
    await client.aclose()

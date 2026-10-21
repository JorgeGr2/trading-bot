"""Precio retrasado: extracción de precios, precio de SOL de los pools y caché."""

from __future__ import annotations

from decimal import Decimal

import pytest

from copybot.backtest.delayed import DelayedPrices, SwapPrice, swap_prices, usd_price
from copybot.backtest.poolprice import SOL, PoolPrices, pool_swaps
from copybot.db.session import make_sessionmaker
from copybot.solana.constants import TOKEN_PROGRAM, USDC_MINT, WSOL_MINT
from tests.fakes import MEME, RELAYER, FakeHistoryRpc, fomo_like_swap

D = Decimal
T = 1_791_000_000  # en punto
OTHER = "Other11111111111111111111111111111111111111"
TEMP = "TempWsolAccount1111111111111111111111111111"
POOL = SOL.pools[0]


def swap(sig: str, at: int, tokens: int, usdc: int) -> dict:
    return fomo_like_swap(
        sig, at, OTHER, token_raw_delta=tokens, usdc_raw_delta=usdc, block_time=at
    )


def own_swap(sig: str, at: int, tokens: int, lamports: int) -> dict:
    """Swap normal pagado en SOL: el usuario paga la comisión y firma también una cuenta
    temporal de wSOL recién creada (el caso que el modo descubrimiento no veía)."""
    tx = fomo_like_swap(sig, at, OTHER, token_raw_delta=tokens, usdc_raw_delta=0, block_time=at)
    keys = tx["transaction"]["message"]["accountKeys"]
    keys[0]["pubkey"] = OTHER
    keys[1]["pubkey"] = TEMP
    meta = tx["meta"]
    meta["preBalances"][0] = 10**10
    meta["postBalances"][0] = 10**10 - meta["fee"] + lamports
    return tx


def pool_tx(at: int, sol_lamports: int, usdc_raw: int) -> dict:
    """Cambios de saldo de las cuentas del pool SOL-USDC (lo único que se lee)."""

    def bal(idx: int, mint: str, amount: int, decimals: int) -> dict:
        return {
            "accountIndex": idx,
            "mint": mint,
            "owner": POOL,
            "programId": TOKEN_PROGRAM,
            "uiTokenAmount": {"amount": str(amount), "decimals": decimals},
        }

    return {
        "slot": at,
        "blockTime": at,
        "transaction": {"signatures": [f"pool{at}{sol_lamports}"], "message": {}},
        "meta": {
            "err": None,
            "preTokenBalances": [bal(1, WSOL_MINT, 10**15, 9), bal(2, USDC_MINT, 10**13, 6)],
            "postTokenBalances": [
                bal(1, WSOL_MINT, 10**15 + sol_lamports, 9),
                bal(2, USDC_MINT, 10**13 + usdc_raw, 6),
            ],
        },
    }


def test_prices_from_other_users_swaps():
    txs = [
        swap("a" * 10, T + 5, 10**9, -12 * 10**6),  # compra 1.000 tokens por 12 $
        swap("b" * 10, T + 6, -2 * 10**9, 26 * 10**6),  # vende 2.000 por 26 $
        swap("d" * 10, T + 6, 10**6, -12_000),  # 0,012 $: demasiado pequeña, no cuenta
        own_swap("c" * 10, T + 7, 10**9, -(10**8)),  # compra 1.000 por 0,1 SOL
    ]
    prices = swap_prices(txs, MEME, T, frozenset({RELAYER}))
    assert [(p.offset_s, p.quote, p.price) for p in prices] == [
        (5, "USDC", D("0.012")),
        (6, "USDC", D("0.013")),
        (7, "SOL", D("0.0001")),
    ]


def test_sol_swaps_need_a_sol_price():
    prices = [SwapPrice(4, "SOL", D("0.0001")), SwapPrice(5, "USDC", D("0.0101"))]
    assert usd_price(prices, D(100)) == (D("0.01005"), 2)
    assert usd_price(prices, None) == (D("0.0101"), 1)  # sin precio de SOL: se descarta
    assert usd_price([SwapPrice(4, "SOL", D("0.0001"))], None) == (None, 0)


def test_pool_swaps_read_the_pool_accounts():
    txs = [
        pool_tx(T + 1, -2 * 10**9, 240 * 10**6),  # el pool entrega 2 SOL y recibe 240 USDC
        pool_tx(T + 2, 10**9, -119 * 10**6),  # recibe 1 SOL y entrega 119 USDC
        pool_tx(T + 3, 0, 0),  # un bot que pasa por el pool sin cambiar nada
        pool_tx(T + 4, 10**6, -121_000),  # 0,12 USDC: demasiado pequeño
    ]
    assert [(s.at, s.price, s.amount) for s in pool_swaps(txs, POOL, WSOL_MINT)] == [
        (T + 1, D(120), D(2)),
        (T + 2, D(119), D(1)),
    ]


@pytest.mark.postgres
async def test_sol_price_is_interpolated_between_hourly_samples(engine):
    rpc = FakeHistoryRpc(credits_per_call=10)
    for i in range(3):
        rpc.add(POOL, pool_tx(T + i, -(10**9), 120 * 10**6))
        rpc.add(POOL, pool_tx(T + 3600 + i, -(10**9), 130 * 10**6))
    sessions = make_sessionmaker(engine)
    sol = PoolPrices(sessions, rpc)
    assert await sol.at(T + 900, fetch=True) == D("122.5")
    assert len(rpc.calls) == 2 and sol.lookups == 2  # una muestra por hora, primer pool
    cached = PoolPrices(sessions, None)
    assert await cached.at(T + 1800, fetch=False) == D(125)
    assert await cached.at(T + 7200 * 5, fetch=False) is None


@pytest.mark.postgres
async def test_lookup_extends_the_window_and_is_cached(engine):
    rpc = FakeHistoryRpc(credits_per_call=10)
    for hour in (T, T + 3600):
        for i in range(3):
            rpc.add(POOL, pool_tx(hour + i, -(10**9), 200 * 10**6))  # SOL a 200 $
    rpc.add(MEME, own_swap("c" * 10, T + 30, 10**9, -(10**8)))  # nadie en 4-8 s
    prices = DelayedPrices(make_sessionmaker(engine), rpc, frozenset({RELAYER}))
    row = await prices.get(MEME, T)
    assert row.price_usd == D("0.02") and row.sol_usd == 200
    assert row.window_end == 60 and row.swaps == 1
    assert [c["address"] for c in rpc.calls] == [MEME, MEME, POOL, POOL]  # 4-8 s, 8-60 s
    assert prices.lookups == 4
    again = await prices.get(MEME, T)
    assert again.price_usd == D("0.02") and len(rpc.calls) == 4  # de la caché
    nobody = await prices.get(MEME, T + 1000)
    assert nobody.price_usd is None and nobody.swaps == 0
    offline = DelayedPrices(make_sessionmaker(engine), None, frozenset())
    assert await offline.get(MEME, T + 99) is None and offline.missing == 1


@pytest.mark.postgres
async def test_busy_windows_are_paged(engine):
    rpc = FakeHistoryRpc(credits_per_call=10)
    for i in range(230):  # transacciones de bots sin cambios de saldo
        rpc.add(MEME, swap(f"n{i:09d}", T + 5, 0, 0), tx_index=i)
    rpc.add(MEME, swap("y" * 10, T + 6, 10**9, -10 * 10**6), tx_index=999)
    sessions = make_sessionmaker(engine)
    prices = DelayedPrices(sessions, rpc, frozenset({RELAYER}), sol=PoolPrices(sessions, None))
    row = await prices.get(MEME, T)
    assert row.price_usd == D("0.01") and row.detail["paginas"] == 3


@pytest.mark.postgres
async def test_pool_samples_page_past_the_bots_and_series_is_hourly(engine):
    rpc = FakeHistoryRpc(credits_per_call=10)
    for i in range(150):  # bots que pasan por el pool sin cambiar nada
        rpc.add(POOL, pool_tx(T, 0, 0), tx_index=i)
    for i in range(3):
        rpc.add(POOL, pool_tx(T + 1 + i, -(10**9), 150 * 10**6))
        rpc.add(POOL, pool_tx(T + 7200 + i, -(10**9), 160 * 10**6))
    sol = PoolPrices(make_sessionmaker(engine), rpc)
    series = await sol.hourly(T, T + 7200, fetch=True)
    assert series == [(T, D(150)), (T + 7200, D(160))]  # la hora T+3600 no tiene swaps
    assert sol.lookups == 2 + 2 + 1  # T: 2 páginas; T+3600: 2 pools vacíos; T+7200: 1


@pytest.mark.postgres
async def test_unsettled_or_unconvertible_windows_are_not_cached(engine):
    from datetime import UTC, datetime

    rpc = FakeHistoryRpc(credits_per_call=10)
    sessions = make_sessionmaker(engine)
    # La ventana acaba dentro de los últimos 2 minutos: aún pueden llegar operaciones.
    soon = DelayedPrices(
        sessions, rpc, frozenset({RELAYER}), clock=lambda: datetime.fromtimestamp(T + 90, UTC)
    )
    assert await soon.get(MEME, T) is None and soon.missing == 1 and not rpc.calls
    # Hubo operaciones, pero solo en SOL y sin precio de SOL: no es «nadie operó».
    rpc.add(MEME, own_swap("e" * 10, T + 5, 10**9, -(10**8)))
    blind = DelayedPrices(sessions, rpc, frozenset({RELAYER}), sol=PoolPrices(sessions, None))
    assert await blind.get(MEME, T) is None and blind.missing == 1
    assert await soon.get(MEME, T, fetch=False) is None  # nada guardado


def test_pool_outliers_do_not_set_the_price():
    # Cambio y aporte de liquidez en la misma transacción: saldos netos sin sentido.
    txs = [pool_tx(T + i, -(10**9), 120 * 10**6) for i in range(3)]
    txs.insert(0, pool_tx(T, -6 * 10**6, 200 * 10**6))  # «33.333 $ por SOL»
    swaps = pool_swaps(txs, POOL, WSOL_MINT)
    assert len(swaps) == 4  # el filtro de la mediana se aplica al formar la muestra


@pytest.mark.postgres
async def test_pool_sample_drops_outliers(engine):
    rpc = FakeHistoryRpc(credits_per_call=10)
    rpc.add(POOL, pool_tx(T, -6 * 10**6, 200 * 10**6), tx_index=0)
    for i in range(3):
        rpc.add(POOL, pool_tx(T + 1 + i, -(10**9), 120 * 10**6))
    sol = PoolPrices(make_sessionmaker(engine), rpc)
    assert await sol.hour(T, fetch=True) == 120

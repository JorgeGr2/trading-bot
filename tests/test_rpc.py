from __future__ import annotations

import json

import httpx
import pytest
import respx

from copybot.solana.rpc import HELIUS_CREDITS_PER_REQUEST, RateLimiter, RpcError, SolanaRpc

URL = "https://rpc.example.com/?api-key=SECRETO123"


class Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def ok(result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


@respx.mock
async def test_retries_429_respecting_retry_after():
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            ok(123),
        ]
    )
    sleeper = Sleeper()
    async with SolanaRpc(URL, rate_per_second=1000, sleep=sleeper) as rpc:
        assert await rpc.get_slot() == 123
    assert route.call_count == 2
    assert 7.0 in sleeper.delays
    assert rpc.usage["getSlot"] == 2  # cada intento cuenta para el gasto


@respx.mock
async def test_gives_up_after_max_attempts_without_leaking_url():
    respx.post(URL).mock(return_value=httpx.Response(503))
    async with SolanaRpc(URL, rate_per_second=1000, max_attempts=3, sleep=Sleeper()) as rpc:
        with pytest.raises(RpcError) as info:
            await rpc.get_slot()
    assert "SECRETO123" not in str(info.value)
    assert "rpc.example.com" not in str(info.value)


@respx.mock
async def test_non_retryable_rpc_error_raises_immediately():
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}}
        )
    )
    async with SolanaRpc(URL, rate_per_second=1000, sleep=Sleeper()) as rpc:
        with pytest.raises(RpcError) as info:
            await rpc.get_slot()
    assert info.value.code == -32602
    assert route.call_count == 1


@respx.mock
async def test_retryable_rpc_error_is_retried():
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "behind"}},
            ),
            ok(5),
        ]
    )
    async with SolanaRpc(URL, rate_per_second=1000, sleep=Sleeper()) as rpc:
        assert await rpc.get_slot() == 5


@respx.mock
async def test_transport_errors_are_retried():
    respx.post(URL).mock(side_effect=[httpx.ConnectTimeout("t"), httpx.ReadError("r"), ok(9)])
    async with SolanaRpc(URL, rate_per_second=1000, sleep=Sleeper()) as rpc:
        assert await rpc.get_slot() == 9


@respx.mock
async def test_get_transaction_requests_v1_and_jsonparsed():
    route = respx.post(URL).mock(return_value=ok(None))
    async with SolanaRpc(URL, rate_per_second=1000, sleep=Sleeper()) as rpc:
        assert await rpc.get_transaction("sig") is None
    body = route.calls.last.request.content
    assert b'"maxSupportedTransactionVersion":1' in body.replace(b" ", b"")
    assert b"jsonParsed" in body


@respx.mock
async def test_usage_callback():
    respx.post(URL).mock(return_value=ok([]))
    seen: list[tuple[str, int, int]] = []

    async def on_usage(method: str, n: int, credits: int) -> None:
        seen.append((method, n, credits))

    async with SolanaRpc(
        URL,
        rate_per_second=1000,
        on_usage=on_usage,
        credits_per_request=HELIUS_CREDITS_PER_REQUEST,
        sleep=Sleeper(),
    ) as rpc:
        await rpc.get_signatures_for_address("addr", until="x")
        await rpc.get_transactions_for_address("addr", full=False)
        await rpc.get_transactions_for_address("addr", full=True)
    assert seen == [
        ("getSignaturesForAddress", 1, 10),
        ("getTransactionsForAddress", 1, 10),  # solo firmas
        ("getTransactionsForAddress", 1, 100),  # transacciones completas
    ]
    assert rpc.credits["getTransactionsForAddress"] == 110


@respx.mock
async def test_budget_check_runs_before_the_request_and_can_block_it():
    route = respx.post(URL).mock(return_value=ok(1))
    checked: list[tuple[str, int]] = []

    class Blocked(Exception):
        pass

    async def budget_check(method: str, credits: int) -> None:
        checked.append((method, credits))
        if method == "getTransactionsForAddress":
            raise Blocked

    async with SolanaRpc(
        URL,
        rate_per_second=1000,
        budget_check=budget_check,
        credits_per_request=HELIUS_CREDITS_PER_REQUEST,
        sleep=Sleeper(),
    ) as rpc:
        assert await rpc.get_slot() == 1
        with pytest.raises(Blocked):
            await rpc.get_transactions_for_address("addr", full=True)
    assert route.call_count == 1  # la petición bloqueada no llega a enviarse
    assert checked == [("getSlot", 1), ("getTransactionsForAddress", 100)]
    assert rpc.usage["getTransactionsForAddress"] == 0


@respx.mock
async def test_gtfa_builds_filters_and_stops_on_short_page():
    page = {"data": [{"signature": "a"}, {"signature": "b"}], "paginationToken": "9:1"}
    route = respx.post(URL).mock(return_value=ok(page))
    async with SolanaRpc(URL, rate_per_second=1000, sleep=Sleeper()) as rpc:
        full = await rpc.get_transactions_for_address(
            "addr",
            full=True,
            limit=2,
            block_time_gte=100,
            block_time_lt=200,
            pagination_token="5:3",
        )
        short = await rpc.get_transactions_for_address("addr", full=False, limit=3)
    first = json.loads(route.calls[0].request.content)["params"][1]
    assert first == {
        "transactionDetails": "full",
        "limit": 2,
        "sortOrder": "desc",
        "filters": {
            "tokenAccounts": "balanceChanged",
            "status": "succeeded",
            "blockTime": {"gte": 100, "lt": 200},
        },
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 1,
        "paginationToken": "5:3",
    }
    # Página llena: hay que seguir. Página incompleta: no hay más (el nodo devuelve
    # token igualmente, pero la siguiente página llegaría vacía y costaría créditos).
    assert full.pagination_token == "9:1"
    assert short.pagination_token is None
    assert [i["signature"] for i in short.items] == ["a", "b"]


async def test_gtfa_rejects_out_of_range_limit():
    async with SolanaRpc(URL, rate_per_second=1000, sleep=Sleeper()) as rpc:
        with pytest.raises(ValueError):
            await rpc.get_transactions_for_address("addr", full=True, limit=1001)


async def test_rate_limiter_spaces_requests():
    now = [100.0]
    sleeper = Sleeper()

    async def fake_sleep(delay: float) -> None:
        await sleeper(delay)
        now[0] += delay

    limiter = RateLimiter(2.0, clock=lambda: now[0], sleep=fake_sleep)
    for _ in range(3):
        await limiter.acquire()
    assert sleeper.delays == [pytest.approx(0.5), pytest.approx(0.5)]


def test_rate_limiter_rejects_invalid_rate():
    with pytest.raises(ValueError):
        RateLimiter(0)

from __future__ import annotations

import httpx
import pytest
import respx

from copybot.solana.rpc import RateLimiter, RpcError, SolanaRpc

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
    seen: list[tuple[str, int]] = []

    async def on_usage(method: str, n: int) -> None:
        seen.append((method, n))

    async with SolanaRpc(URL, rate_per_second=1000, on_usage=on_usage, sleep=Sleeper()) as rpc:
        await rpc.get_signatures_for_address("addr", until="x")
    assert seen == [("getSignaturesForAddress", 1)]


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

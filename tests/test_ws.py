"""Suscriptor WebSocket contra un servidor local que imita a un nodo de Solana."""

from __future__ import annotations

import asyncio
import json

import websockets

from copybot.solana.ws import LogsSubscriber, Notification, Reconnected

ADDR = "7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm"


async def test_notifications_and_reconnection():
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        request = json.loads(await ws.recv())
        assert request["method"] == "logsSubscribe"
        assert request["params"][0] == {"mentions": [ADDR]}
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": 42}))
        for sig, err in ((f"s{connections}a", None), (f"s{connections}b", {"e": 1})):
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "logsNotification",
                        "params": {
                            "subscription": 42,
                            "result": {
                                "context": {"slot": 7},
                                "value": {"signature": sig, "err": err, "logs": []},
                            },
                        },
                    }
                )
            )
        # El servidor corta la conexión de golpe.
        await ws.close()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        sub = LogsSubscriber(f"ws://127.0.0.1:{port}", [ADDR], backoff_base_seconds=0.01)
        items = []

        async def collect():
            async for item in sub.stream():
                items.append(item)
                if len(items) == 6:
                    return

        await asyncio.wait_for(collect(), timeout=10)

    assert isinstance(items[0], Reconnected)
    assert isinstance(items[3], Reconnected)
    notes = [i for i in items if isinstance(i, Notification)]
    assert [(n.signature, n.failed, n.slot, n.address) for n in notes] == [
        ("s1a", False, 7, ADDR),
        ("s1b", True, 7, ADDR),
        ("s2a", False, 7, ADDR),
        ("s2b", True, 7, ADDR),
    ]
    assert connections == 2


async def test_unreachable_server_keeps_retrying():
    sub = LogsSubscriber("ws://127.0.0.1:9", [ADDR], backoff_base_seconds=0.01)
    stream = sub.stream()
    task = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.3)
    # No ha emitido nada (no hay conexión) pero sigue intentándolo sin fallar.
    assert not task.done()
    task.cancel()

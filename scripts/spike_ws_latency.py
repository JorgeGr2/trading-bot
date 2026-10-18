"""Mide la latencia de detección de operaciones de FOMO por WebSocket.

Se suscribe a los logs que mencionan al relayer de FOMO durante ``--seconds``
segundos, anota la hora de recepción de cada transacción y después la compara
con el ``blockTime`` que devuelve ``getSignaturesForAddress``.

El ``blockTime`` de Solana tiene resolución de 1 segundo y es una estimación, así
que el resultado es orientativo (±1 s). La medición definitiva se hará en el
servidor con el proveedor contratado.

Uso:
    uv run python scripts/spike_ws_latency.py --seconds 120
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import urllib.request

import websockets

RELAYER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"


async def listen(ws_url: str, seconds: float, commitment: str) -> dict[str, float]:
    received: dict[str, float] = {}
    async with websockets.connect(ws_url, max_size=2**22, open_timeout=20) as ws:
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [{"mentions": [RELAYER]}, {"commitment": commitment}],
                }
            )
        )
        ack = json.loads(await ws.recv())
        if "error" in ack:
            raise RuntimeError(ack["error"])
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError:
                break
            now = time.time()
            msg = json.loads(raw)
            value = msg.get("params", {}).get("result", {}).get("value", {})
            sig = value.get("signature")
            if sig and value.get("err") is None:
                received.setdefault(sig, now)
    return received


def block_times(rpc_url: str) -> dict[str, int]:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [RELAYER, {"limit": 1000}],
        }
    ).encode()
    req = urllib.request.Request(rpc_url, data=body, headers={"content-type": "application/json"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return {s["signature"]: s["blockTime"] for s in json.load(resp)["result"] if s["blockTime"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--ws-url", default="wss://api.mainnet-beta.solana.com")
    ap.add_argument("--rpc-url", default="https://api.mainnet-beta.solana.com")
    ap.add_argument("--commitment", default="confirmed", choices=["processed", "confirmed"])
    args = ap.parse_args()
    received = asyncio.run(listen(args.ws_url, args.seconds, args.commitment))
    times = block_times(args.rpc_url)
    lat = sorted(received[s] - times[s] for s in received if s in times)
    print(
        f"Notificaciones recibidas: {len(received)} en {args.seconds:.0f} s "
        f"({len(received) / args.seconds:.2f} tx/s), commitment={args.commitment}"
    )
    if lat:
        p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))]
        print(
            f"Latencia respecto a blockTime (±1 s): n={len(lat)}, "
            f"mediana {statistics.median(lat):.2f} s, p95 {p95:.2f} s, "
            f"min {lat[0]:.2f} s, max {lat[-1]:.2f} s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

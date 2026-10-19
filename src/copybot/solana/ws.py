"""Suscripción en vivo a la actividad de wallets por WebSocket (``logsSubscribe``).

Cada vez que se pierde la conexión se emite un ``Reconnected``: quien consume los
eventos debe recuperar los posibles huecos consultando el historial, porque las
notificaciones emitidas mientras estábamos desconectados no se reenvían.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
import websockets

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Notification:
    address: str
    signature: str
    slot: int | None
    failed: bool
    received_at: datetime


@dataclass(frozen=True)
class Reconnected:
    """Se ha (re)establecido la conexión; puede haber notificaciones perdidas."""

    attempt: int


LiveItem = Notification | Reconnected


class LogsSubscriber:
    def __init__(
        self,
        ws_url: str,
        addresses: Sequence[str],
        *,
        commitment: str = "confirmed",
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        idle_timeout_seconds: float | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._url = ws_url
        self._addresses = list(addresses)
        self._commitment = commitment
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._idle_timeout = idle_timeout_seconds
        self._clock = clock
        # ¿Está suscrito ahora mismo? (lo publica el latido del monitor)
        self.connected = False

    async def stream(self) -> AsyncIterator[LiveItem]:
        attempt = 0
        while True:
            try:
                async with websockets.connect(
                    self._url, max_size=2**22, open_timeout=20, ping_interval=20
                ) as ws:
                    subs = await self._subscribe(ws)
                    self.connected = True
                    yield Reconnected(attempt)
                    attempt = 0
                    async for item in self._read(ws, subs):
                        yield item
            except (OSError, TimeoutError, websockets.WebSocketException) as exc:
                log.warning("ws_disconnected", error=type(exc).__name__, attempt=attempt)
            finally:
                self.connected = False
            delay = min(self._backoff_max, self._backoff_base * 2**attempt)
            attempt += 1
            await asyncio.sleep(delay * (0.5 + random.random() / 2))  # noqa: S311

    async def _subscribe(self, ws: websockets.ClientConnection) -> dict[int, str]:
        pending: dict[int, str] = {}
        for request_id, address in enumerate(self._addresses, start=1):
            pending[request_id] = address
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "logsSubscribe",
                        "params": [{"mentions": [address]}, {"commitment": self._commitment}],
                    }
                )
            )
        subs: dict[int, str] = {}
        while pending:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if msg.get("id") in pending:
                address = pending.pop(msg["id"])
                if "error" in msg:
                    raise websockets.WebSocketException(f"suscripción rechazada: {msg['error']}")
                subs[int(msg["result"])] = address
        return subs

    async def _read(
        self, ws: websockets.ClientConnection, subs: dict[int, str]
    ) -> AsyncIterator[Notification]:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=self._idle_timeout)
            msg = json.loads(raw)
            if msg.get("method") != "logsNotification":
                continue
            params = msg.get("params") or {}
            address = subs.get(params.get("subscription"))
            result = params.get("result") or {}
            value = result.get("value") or {}
            signature = value.get("signature")
            if address is None or not signature:
                continue
            yield Notification(
                address=address,
                signature=signature,
                slot=(result.get("context") or {}).get("slot"),
                failed=value.get("err") is not None,
                received_at=self._clock(),
            )

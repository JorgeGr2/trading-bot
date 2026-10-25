"""Aplicación web del panel (Starlette): una página y su API JSON, solo de lectura.

* La conexión a la base de datos es de **solo lectura** (``default_transaction_read_only``):
  aunque hubiera un fallo en el panel, PostgreSQL rechazaría cualquier escritura.
* Nunca se devuelve configuración: ni la URL de la base de datos ni claves de proveedores.
  Si la base de datos falla, la respuesta solo dice que no se pudo leer.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from copybot.panel.crypto import crypto_state
from copybot.panel.sniper import sniper_state

log = structlog.get_logger(__name__)

STATEMENT_TIMEOUT_MS = 10_000
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
# Todo va dentro de la página: nada de CDN ni de recursos de otros sitios.
PAGE_HEADERS = {
    **HEADERS,
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
}
DB_ERROR = "No se pudo leer la base de datos."


def make_readonly_engine(url: str) -> AsyncEngine:
    """Motor de PostgreSQL (asyncpg) en el que toda transacción es de solo lectura."""
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=2,
        connect_args={
            "server_settings": {
                "default_transaction_read_only": "on",
                "statement_timeout": str(STATEMENT_TIMEOUT_MS),
                "application_name": "copybot-panel",
            }
        },
    )


def page_html() -> str:
    return files("copybot.panel").joinpath("page.html").read_text(encoding="utf-8")


def create_app(
    sessions: async_sessionmaker[AsyncSession],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    engine: AsyncEngine | None = None,
) -> Starlette:
    """La aplicación. Con ``engine``, se cierra al parar el servidor."""
    page = page_html()

    async def index(_request: Request) -> Response:
        return HTMLResponse(page, headers=PAGE_HEADERS)

    async def answer(build: Callable[[], Any], name: str) -> Response:
        try:
            data = await build()
        except Exception as exc:  # el panel no debe caerse ni enseñar el error
            log.warning("panel_error", api=name, error=type(exc).__name__)
            return JSONResponse({"error": DB_ERROR}, status_code=503, headers=HEADERS)
        return JSONResponse(data, headers=HEADERS)

    async def estado(request: Request) -> Response:
        raw = request.query_params.get("cartera", "")
        run_id = int(raw) if raw.isdigit() else None
        return await answer(lambda: crypto_state(sessions, clock(), run_id=run_id), "estado")

    async def sniper(_request: Request) -> Response:
        return await answer(lambda: sniper_state(sessions), "sniper")

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield
        if engine is not None:
            await engine.dispose()

    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/estado", estado),
            Route("/api/sniper", sniper),
        ],
        lifespan=lifespan,
    )

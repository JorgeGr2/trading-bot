"""Pestaña «Sniper Solana (simulación)»: de momento, sin datos.

El bot de sniping en Solana (simulado) se construirá aparte. Cuando exista, esta función
leerá sus tablas y devolverá, si los tiene, estos campos (la página pinta los que haya):

* ``saldo``, ``pnl``: dólares (número).
* ``operaciones``: número de operaciones.
* ``aciertos``: ``{"ganadas": int, "perdidas": int}``.
* ``latencia_ms_mediana``: milisegundos (número).
* ``log``: lista de filas, p. ej. ``{"hora", "lado", "token", "usd", "motivo",
  "resultado"}``.
* ``posiciones``: lista de filas, p. ej. ``{"token", "coste", "valor", "desde"}``.

Solo simulación: nada de claves, firmas ni envío de transacciones.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.panel.common import SIN_DATOS


async def sniper_state(sessions: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Estado del sniper de Solana para el panel (todavía no hay datos que leer)."""
    del sessions  # aún no hay tablas del sniper
    return {"estado": SIN_DATOS}

"""Calibración de los costes con fotos reales del libro de Binance (mejor bid y ask).

Cada foto guarda el bid, el ask y sus cantidades de todos los pares USDT. El coste medido
por tramo de liquidez (percentil 75 del medio diferencial más un colchón) se compara con
la tabla de ``costs.py``: si un tramo medido la supera, la tabla se sube **antes** de
registrar la regla.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.crypto import costs
from copybot.crypto.binance import BinanceMarket
from copybot.crypto.sync import EXCHANGE
from copybot.db.models import CryptoCostSnapshot, MarketBar, MarketSymbol


async def _median_volumes(sessions: async_sessionmaker[AsyncSession]) -> dict[str, float]:
    """Mediana del volumen diario en USDT de los últimos 30 días de cada par."""
    since = datetime.now(UTC) - timedelta(days=31)
    async with sessions() as s:
        rows = (
            await s.execute(
                select(MarketBar.symbol, MarketBar.quote_volume).where(
                    MarketBar.exchange == EXCHANGE,
                    MarketBar.interval == "1d",
                    MarketBar.open_time >= since,
                )
            )
        ).all()
    by: dict[str, list[float]] = {}
    for symbol, qv in rows:
        by.setdefault(symbol, []).append(float(qv))
    return {symbol: statistics.median(v) for symbol, v in by.items()}


async def calibration_snapshot(
    sessions: async_sessionmaker[AsyncSession], market: BinanceMarket
) -> tuple[datetime, int]:
    async with sessions() as s:
        usdt = set(
            await s.scalars(
                select(MarketSymbol.symbol).where(
                    MarketSymbol.exchange == EXCHANGE,
                    MarketSymbol.quote == "USDT",
                    MarketSymbol.status == "TRADING",
                )
            )
        )
    books = [b for b in await market.book_tickers() if b.symbol in usdt]
    taken = datetime.now(UTC).replace(microsecond=0)
    volumes = await _median_volumes(sessions)
    rows = [
        {
            "taken_at": taken,
            "symbol": b.symbol,
            "bid": b.bid,
            "ask": b.ask,
            "bid_qty": b.bid_qty,
            "ask_qty": b.ask_qty,
            "quote_volume_24h": volumes.get(b.symbol),
        }
        for b in books
    ]
    async with sessions.begin() as s:
        for i in range(0, len(rows), 1000):
            await s.execute(
                insert(CryptoCostSnapshot).values(rows[i : i + 1000]).on_conflict_do_nothing()
            )
    return taken, len(rows)


async def calibration_report(sessions: async_sessionmaker[AsyncSession]) -> str:
    """Tabla en castellano: coste de la tabla frente al medido, por tramo."""
    async with sessions() as s:
        n_photos = await s.scalar(select(func.count(func.distinct(CryptoCostSnapshot.taken_at))))
        rows = list(await s.scalars(select(CryptoCostSnapshot)))
    snaps = [
        (r.symbol, r.bid, r.ask, r.ask_qty, r.quote_volume_24h)
        for r in rows
        if r.quote_volume_24h is not None
    ]
    cal = costs.calibrate(snaps)
    lines = [
        f"Fotos del libro: {n_photos} ({len(snaps)} observaciones con volumen conocido).",
        "",
        "| Tramo (mediana de 30 días) | Coste de la tabla | Medido (p75 + 0,02 %) | Fotos "
        "| Libro fino (< 25 $) |",
        "|---|---|---|---|---|",
    ]
    for t in cal.tiers:
        measured = "—" if t.measured_cost is None else f"{t.measured_cost * 100:.3f} %"
        thin = "—" if t.thin_share is None else f"{t.thin_share:.0%}"
        lines.append(
            f"| {t.label} | {t.table_cost * 100:.3f} % | {measured} | {t.samples} | {thin} |"
        )
    lines.append("")
    lines += cal.warnings() or ["La tabla cubre los costes medidos en todos los tramos."]
    return "\n".join(lines)

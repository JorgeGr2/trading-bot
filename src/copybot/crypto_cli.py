"""Órdenes del bot de cripto (Binance, solo simulación).

copybot crypto sync [--since 2019-01-01]   descarga pares y velas diarias (lo que falte)
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from copybot.config import Settings
from copybot.crypto.binance import BinanceMarket
from copybot.crypto.sync import HISTORY_START, sync_all
from copybot.db.session import make_engine, make_sessionmaker


def add_parsers(sub: argparse._SubParsersAction) -> None:
    crypto = sub.add_parser("crypto", help="bot de cripto en Binance (simulado)")
    csub = crypto.add_subparsers(dest="action", required=True)
    sync = csub.add_parser("sync", help="descargar pares y velas diarias de Binance")
    sync.add_argument(
        "--since",
        type=lambda v: datetime.fromisoformat(v).replace(tzinfo=UTC),
        default=HISTORY_START,
        help="desde qué día (solo para pares sin historial todavía)",
    )


async def run(args: argparse.Namespace, settings: Settings) -> int:
    engine = make_engine(settings.database_url.get_secret_value())
    sessions = make_sessionmaker(engine)
    try:
        if args.action == "sync":
            return await _sync(args, sessions)
    finally:
        await engine.dispose()
    return 2


async def _sync(args: argparse.Namespace, sessions) -> int:
    def progress(done: int, total: int, symbol: str) -> None:
        if done % 25 == 0 or done == total:
            print(f"  {done}/{total} pares ({symbol})", file=sys.stderr, flush=True)

    async with BinanceMarket() as market:
        result = await sync_all(sessions, market, since=args.since, progress=progress)
    print(
        f"Pares en Binance: {result.symbols} ({result.new_symbols} nuevos). Velas diarias "
        f"nuevas: {result.bars:,} en {result.updated_symbols} pares. Peticiones: "
        f"{result.requests}."
    )
    return 0

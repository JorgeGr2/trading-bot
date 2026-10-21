"""Órdenes del backtester (Fase 5).

copybot backtest copy --select-as-of 2026-10-05 [--select-window 21] [--until …]
                      [--wallet W …] [--top 10] [--fetch --max-lookups 2000] [--out F]
                      [--no-references]

Sin ``--fetch`` solo usa los precios ya guardados en caché (no gasta créditos). Salvo con
``--no-references``, compara también con comprar y mantener, la compra semanal y el
seguimiento de tendencia de SOL, BTC, ETH y SPYx en el mismo periodo.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from copybot.backtest.copy import CopyBacktest, CopyParams
from copybot.backtest.data import (
    DelayedQuotes,
    load_events,
    select_traders,
    transfer_fees,
)
from copybot.backtest.delayed import DelayedPrices
from copybot.backtest.poolprice import ASSETS, HOUR, SOL, PoolPrices
from copybot.backtest.references import RefParams, RefResult, references
from copybot.backtest.report import Coverage, Scenario, render
from copybot.config import RiskLimits, Settings
from copybot.solana.addresses import validate_address
from copybot.traders_cli import context, parse_as_of

CONSERVATIVE = RiskLimits(
    max_position_usd=10,
    max_open_positions=5,
    min_cash_reserve_pct=20,
    max_daily_loss_pct=10,
    max_drawdown_pct=25,
)


def add_parsers(sub: argparse._SubParsersAction) -> None:
    bt = sub.add_parser("backtest", help="qué habría pasado en un periodo pasado")
    bsub = bt.add_subparsers(dest="action", required=True)
    c = bsub.add_parser("copy", help="copiar a traders con el retraso y los costes reales")
    c.add_argument("--select-as-of", help="fecha de selección: se eligen con datos anteriores")
    c.add_argument("--select-window", type=int, default=21, help="días de la ventana de selección")
    c.add_argument(
        "--from", dest="start", help="inicio del periodo copiado (por defecto, la selección)"
    )
    c.add_argument("--until", help="fin del periodo copiado (por defecto, ahora)")
    c.add_argument("--wallet", action="append", default=[], help="copiar estas wallets")
    c.add_argument("--top", type=int, default=10, help="como mucho N traders seleccionados")
    c.add_argument("--fetch", action="store_true", help="consultar a Helius lo que falte")
    c.add_argument("--max-lookups", type=int, default=2000, help="tope de consultas (10 créditos)")
    c.add_argument(
        "--impact", type=Decimal, default=Decimal("1.5"), help="impacto propio por lado, en %%"
    )
    c.add_argument("--seed", type=int, default=7)
    c.add_argument("--out", help="guardar el informe en este fichero")
    c.add_argument(
        "--no-references", action="store_true", help="sin comparar con SOL, BTC, ETH y SPYx"
    )


async def run(args: argparse.Namespace, settings: Settings) -> int:
    async with context(settings, needs_helius=args.fetch) as ctx:
        sessions = ctx.repo.sessions
        until = parse_as_of(args.until)
        select_as_of = parse_as_of(args.select_as_of) if args.select_as_of else None
        start = parse_as_of(args.start) if args.start else select_as_of
        if start is None:
            raise SystemExit("Indica --select-as-of (selección fuera de muestra) o --from.")
        if args.wallet:
            selected = [validate_address(w) for w in args.wallet]
            selection = await select_traders(ctx.repo, selected, start, args.select_window)
        else:
            if select_as_of is None:
                raise SystemExit(
                    "Sin --wallet hace falta --select-as-of para elegir a los traders."
                )
            imported = [w.wallet for w in await ctx.repo.imported_wallets()]
            print(f"Analizando {len(imported)} traders a fecha {select_as_of:%Y-%m-%d}…")
            selection = await select_traders(ctx.repo, imported, select_as_of, args.select_window)
            selected = [s.wallet for s in selection if s.eligible and (s.score or 0) > 0]
            selected = selected[: args.top]
        if not selected:
            print("Ningún trader con ventaja a esa fecha: no hay nada que copiar.")
            return 0
        events = await load_events(sessions, selected, until)
        t0, t1 = int(start.timestamp()), int(until.timestamp())
        in_period = [e for e in events if t0 <= e.at < t1 and e.kind in ("buy", "sell")]
        buys = sum(1 for e in in_period if e.kind == "buy")
        expected = int(len(in_period) * 1.5) + (t1 - t0) // 3600
        print(
            f"{len(selected)} traders; {buys} compras y {len(in_period) - buys} ventas que "
            f"copiar ({start:%Y-%m-%d %H:%M} → {until:%Y-%m-%d %H:%M} UTC). Consultas "
            f"previstas si no hay nada en caché: unas {expected} (≈{expected * 10:,} créditos), "
            f"precio horario de SOL incluido."
        )
        prices = DelayedPrices(sessions, ctx.rpc, frozenset({settings.fomo_relayer}))
        quotes = DelayedQuotes(prices, fetch=args.fetch, max_lookups=args.max_lookups)
        fees = await transfer_fees(sessions, {e.mint for e in in_period})
        aggressive = RiskLimits()
        base = CopyParams(size_usd=aggressive.max_position_usd, impact_pct=args.impact)
        scenarios = []
        plans = [
            ("agresivo (25 $), costes medidos", base, aggressive),
            (
                "agresivo, costes a la mitad",
                _with(base, cost_multiplier=Decimal("0.5")),
                aggressive,
            ),
            ("agresivo, costes al doble", _with(base, cost_multiplier=Decimal(2)), aggressive),
            (
                "conservador (10 $), costes medidos",
                _with(base, size_usd=CONSERVATIVE.max_position_usd),
                CONSERVATIVE,
            ),
        ]
        for name, params, limits in plans:
            bt = CopyBacktest(quotes, params, limits, transfer_fee_bps=fees)
            scenarios.append(Scenario(name, await bt.run(events, t0, t1)))
        refs: list[tuple[RefResult, RefResult]] = []
        coverage: list[Coverage] = []
        ref_lookups = 0
        if not args.no_references:
            for asset in ASSETS.values():
                pool = prices.sol if asset is SOL else PoolPrices(sessions, ctx.rpc, asset)
                refs_asset, cov = await _references(pool, t0, t1, fetch=args.fetch)
                refs += refs_asset
                coverage.append(cov)
                if pool is not prices.sol:
                    ref_lookups += pool.lookups
        text = render(
            scenarios,
            selection,
            selected=selected,
            select_as_of=select_as_of,
            select_window=args.select_window if select_as_of else None,
            lookups=quotes.lookups + ref_lookups,
            missing=quotes.missing,
            seed=args.seed,
            references=refs,
            ref_params=RefParams(),
            coverage=coverage,
        )
        if args.out:
            await asyncio.to_thread(Path(args.out).write_text, text, encoding="utf-8")
            print(f"Informe guardado en {args.out}")
        print(text)
        if not args.fetch and quotes.missing:
            print(
                f"Faltan {quotes.missing} precios en caché: repite con --fetch para "
                "consultarlos (gasta créditos de Helius)."
            )
    return 0


async def _references(
    pool: PoolPrices, t0: int, t1: int, *, fetch: bool
) -> tuple[list[tuple[RefResult, RefResult]], Coverage]:
    """Referencias de un activo con costes medidos y al doble (mismo orden)."""
    base = RefParams()
    series = await pool.hourly(t0 - base.trend_hours * HOUR, t1, fetch=fetch)
    hours = len(range(t0 - t0 % HOUR, t1 + 1, HOUR))
    have = sum(1 for t, _ in series if t >= t0)
    cov = Coverage(pool.asset.name, have, hours)
    if not have:
        return [], cov
    doubled = RefParams(cost_multiplier=Decimal(2))
    pairs = zip(
        references(pool.asset.name, series, t0, t1, base),
        references(pool.asset.name, series, t0, t1, doubled),
        strict=True,
    )
    return list(pairs), cov


def _with(params: CopyParams, **changes) -> CopyParams:
    return CopyParams(**{**params.__dict__, **changes})


def now_utc() -> datetime:
    return datetime.now(UTC)

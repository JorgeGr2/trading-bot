"""Órdenes de descubrimiento y análisis de traders (las usa ``copybot.cli``).

copybot discover sample [--windows N] [--days D] [--seed S]
copybot discover candidates [--limit N]
copybot discover import [--top N] [--wallet W ...]
copybot discover run [--windows N] [--days D] [--top N] [--token-lookups N]
copybot analyze [--wallet W ...] [--as-of FECHA] [--token-lookups N]
copybot traders ranking [--limit N]
copybot traders show <wallet>
"""

from __future__ import annotations

import argparse
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from copybot.analysis.metrics import AnalysisParams
from copybot.analysis.scoring import ScoringParams
from copybot.analysis.service import TraderAnalyzer, WalletAnalysis
from copybot.budget import BudgetExceededError, CreditBudget
from copybot.config import Settings
from copybot.db.repo import Repository
from copybot.db.session import make_engine, make_sessionmaker
from copybot.ingest.discovery import RelayerSampler, TokenCreationLookup
from copybot.ingest.history import HistoryImporter
from copybot.solana.addresses import validate_address
from copybot.solana.rpc import HELIUS_CREDITS_PER_REQUEST, SolanaRpc

log = structlog.get_logger(__name__)


def add_parsers(sub: argparse._SubParsersAction) -> None:
    disc = sub.add_parser("discover", help="descubrir traders a partir del relayer de FOMO")
    dsub = disc.add_subparsers(dest="action", required=True)
    sample = dsub.add_parser("sample", help="muestrear ventanas del relayer")
    _sample_args(sample)
    cand = dsub.add_parser("candidates", help="listar las wallets candidatas")
    cand.add_argument("--limit", type=int, default=20)
    imp = dsub.add_parser("import", help="importar el historial de las candidatas")
    imp.add_argument("--top", type=int, default=20)
    imp.add_argument("--wallet", action="append", default=[])
    run = dsub.add_parser("run", help="muestrear, importar y analizar de una vez")
    _sample_args(run)
    run.add_argument("--top", type=int, default=20)
    run.add_argument("--token-lookups", type=int, default=15)

    ana = sub.add_parser("analyze", help="calcular métricas y puntuaciones")
    ana.add_argument("--wallet", action="append", default=[])
    ana.add_argument("--as-of", help="fecha ISO 8601 en UTC (por defecto, ahora)")
    ana.add_argument(
        "--token-lookups",
        type=int,
        default=0,
        help="fechas de creación de token a consultar por wallet (cuesta créditos)",
    )

    tr = sub.add_parser("traders", help="ver el ranking y las explicaciones")
    tsub = tr.add_subparsers(dest="action", required=True)
    rank = tsub.add_parser("ranking")
    rank.add_argument("--limit", type=int, default=20)
    show = tsub.add_parser("show")
    show.add_argument("wallet")


def _sample_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--windows", type=int, default=12)
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--seed", type=int, default=None, help="por defecto, la hora actual")


@dataclass
class Context:
    settings: Settings
    repo: Repository
    rpc: SolanaRpc | None
    budget: CreditBudget | None


@asynccontextmanager
async def context(settings: Settings, *, needs_helius: bool) -> AsyncIterator[Context]:
    engine = make_engine(settings.database_url.get_secret_value())
    repo = Repository(make_sessionmaker(engine))
    rpc = budget = None
    if needs_helius:
        if settings.helius_api_key is None:
            raise SystemExit(
                "Esta orden necesita Helius (getTransactionsForAddress): define "
                "COPYBOT_HELIUS_API_KEY."
            )
        budget = CreditBudget(
            repo, provider="helius", monthly_limit=settings.analysis_credit_limit()
        )

        async def on_usage(method: str, requests: int, credits: int) -> None:
            await repo.record_usage("helius", method, requests, credits=credits)

        rpc = SolanaRpc(
            settings.rpc_endpoints()[0],
            rate_per_second=settings.rpc_max_requests_per_second,
            # Una página de 1.000 transacciones completas pesa unos 30 MB.
            timeout_seconds=max(settings.rpc_timeout_seconds, 90.0),
            on_usage=on_usage,
            budget_check=budget.check,
            credits_per_request=HELIUS_CREDITS_PER_REQUEST,
        )
    try:
        yield Context(settings, repo, rpc, budget)
    finally:
        if rpc is not None:
            await rpc.aclose()
        await engine.dispose()


def _rpc(ctx: Context) -> SolanaRpc:
    if ctx.rpc is None:
        raise RuntimeError("esta orden necesita el cliente de Helius")
    return ctx.rpc


def _analyzer(ctx: Context) -> TraderAnalyzer:
    return TraderAnalyzer(ctx.repo, analysis=AnalysisParams(), scoring=ScoringParams())


async def _report_credits(ctx: Context, spent_before: int) -> None:
    if ctx.rpc is None or ctx.budget is None:
        return
    spent = sum(ctx.rpc.credits.values()) - spent_before
    left = await ctx.budget.remaining()
    print(
        f"Créditos de Helius usados ahora: {spent:,} (estimación según la tarifa publicada). "
        f"Quedan {left:,} de {ctx.budget.monthly_limit:,} este mes para el análisis."
    )


async def sample(ctx: Context, windows: int, days: float, seed: int | None) -> None:
    seed = int(time.time()) if seed is None else seed
    sampler = RelayerSampler(
        _rpc(ctx),
        ctx.repo,
        relayer=ctx.settings.fomo_relayer,
        relayers=frozenset({ctx.settings.fomo_relayer}),
    )
    starts = sampler.window_starts(windows, days, seed)
    print(f"Muestreando {windows} ventanas de los últimos {days:g} días (semilla {seed})…")
    for start in starts:
        result = await sampler.sample_window(start)
        print(
            f"  {start:%Y-%m-%d %H:%M} UTC: {result.transactions} transacciones, "
            f"{result.trades} compraventas, {result.new_observations} nuevas"
        )


async def show_candidates(ctx: Context, limit: int) -> None:
    rows = await ctx.repo.discovery_candidates(limit=limit)
    if not rows:
        print("No hay candidatas: ejecuta antes «copybot discover sample».")
        return
    print(f"{'wallet':44}  ventanas  compraventas  volumen visto")
    for c in rows:
        print(f"{c.wallet:44}  {c.windows:8}  {c.trades:12}  {c.volume_usd:12.2f} $")


async def import_histories(ctx: Context, wallets: list[str]) -> list[str]:
    importer = HistoryImporter(
        _rpc(ctx),
        ctx.repo,
        relayers=frozenset({ctx.settings.fomo_relayer}),
        source="helius",
        max_pages=ctx.settings.history_max_pages,
    )
    done: list[str] = []
    for i, wallet in enumerate(wallets, start=1):
        try:
            r = await importer.import_wallet(wallet, days=ctx.settings.history_days)
        except BudgetExceededError as exc:
            print(f"Se detiene la importación: {exc}")
            break
        done.append(wallet)
        note = " (TRUNCADO: demasiadas transacciones)" if r.truncated else ""
        print(
            f"  [{i}/{len(wallets)}] {wallet}: {r.transactions} transacciones, "
            f"{r.new_events} eventos nuevos, {r.pages} páginas{note}"
        )
    return done


async def analyze(
    ctx: Context, wallets: list[str], as_of: datetime, token_lookups: int
) -> list[WalletAnalysis]:
    analyzer = _analyzer(ctx)
    lookup = TokenCreationLookup(ctx.rpc, ctx.repo) if ctx.rpc and token_lookups else None
    results = []
    for wallet in wallets:
        if lookup is not None:
            try:
                mints = await analyzer.opened_mints(wallet, as_of)
                await lookup.ensure(mints, max_lookups=token_lookups)
            except BudgetExceededError as exc:
                print(f"Sin presupuesto para más fechas de creación: {exc}")
                lookup = None
        results.append(await analyzer.analyze(wallet, as_of))
    eligible = sorted(
        (r for r in results if r.score.eligible), key=lambda r: r.score.score, reverse=True
    )
    print(
        f"Analizadas {len(results)} wallets a fecha {as_of:%Y-%m-%d %H:%M} UTC: "
        f"{len(eligible)} puntuables, {len(results) - len(eligible)} excluidas."
    )
    return results


def _fmt_pct(value: str | None) -> str:
    return "—" if value is None else f"{Decimal(value) * 100:+.1f} %"


async def ranking(ctx: Context, limit: int) -> None:
    analyzer = _analyzer(ctx)
    scores = await ctx.repo.latest_scores(params_hash=analyzer.score_hash)
    if not scores:
        print("No hay puntuaciones con los parámetros actuales: ejecuta «copybot analyze».")
        return
    rows = []
    for s in scores:
        snap = await ctx.repo.metric_snapshot(
            s.wallet, s.as_of, analyzer.scoring.window_days, analyzer.metrics_hash
        )
        rows.append((s, snap.metrics if snap else {}))

    def order(row: tuple) -> tuple:
        # Puntuables primero; a igual puntuación (p. ej. 0), mejor rentabilidad media.
        s, m = row
        mean = m.get("clipped_mean_return")
        return (
            not s.eligible,
            -(s.score if s.score is not None else Decimal(-1)),
            -(Decimal(mean) if mean is not None else Decimal(-100)),
        )

    rows.sort(key=order)
    print(
        f"{'#':>3}  {'wallet':44}  {'puntos':>6}  {'oper.':>5}  {'media/op':>9}  "
        f"{'1ª venta':>9}  motivo de exclusión"
    )
    for i, (s, m) in enumerate(rows[:limit], start=1):
        first = m.get("median_seconds_to_first_sell")
        first_txt = "—" if first is None else f"{Decimal(first):.0f} s"
        reason = s.explanation[1].strip(" -") if not s.eligible and len(s.explanation) > 1 else ""
        points = f"{s.score:6.2f}" if s.score is not None else "excl."
        print(
            f"{i:3}  {s.wallet:44}  {points:>6}  {m.get('round_trips', 0):5}  "
            f"{_fmt_pct(m.get('clipped_mean_return')):>9}  {first_txt:>9}  {reason}"
        )


async def show(ctx: Context, wallet: str) -> None:
    analyzer = _analyzer(ctx)
    wallet = validate_address(wallet)
    scores = [s for s in await ctx.repo.latest_scores() if s.wallet == wallet]
    if not scores:
        print("Esa wallet no tiene puntuación todavía.")
        return
    s = scores[0]
    print(f"Wallet {wallet} a fecha {s.as_of:%Y-%m-%d %H:%M} UTC (puntuación v{s.scoring_version})")
    for line in s.explanation:
        print(f"  {line}")
    snap = await ctx.repo.metric_snapshot(
        wallet, s.as_of, analyzer.scoring.window_days, analyzer.metrics_hash
    )
    if snap:
        m = snap.metrics
        print("Métricas (30 días):")
        for key in (
            "round_trips",
            "win_rate",
            "mean_return",
            "median_return",
            "realized_pnl_usd",
            "conservative_pnl_usd",
            "buy_volume_usd",
            "trades_per_day",
            "median_hold_seconds",
            "median_seconds_to_first_sell",
            "max_drawdown_usd",
            "unmatched_sell_share",
            "launch_checked",
            "early_buys",
        ):
            print(f"  {key}: {m.get(key)}")


def parse_as_of(text: str | None, now: datetime | None = None) -> datetime:
    """Fecha de análisis en UTC. No puede ser futura: el snapshot se guardaría como si
    conociera datos que aún no existen y, al ser inmutable, quedaría incompleto."""
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    if text is None:
        return now
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    if moment > now:
        raise SystemExit(f"--as-of no puede ser posterior a ahora ({now:%Y-%m-%d %H:%M:%S} UTC)")
    return moment


async def run(args: argparse.Namespace, settings: Settings) -> int:
    needs_helius = args.command == "discover" and args.action != "candidates"
    needs_helius = needs_helius or (args.command == "analyze" and args.token_lookups > 0)
    async with context(settings, needs_helius=needs_helius) as ctx:
        spent_before = sum(ctx.rpc.credits.values()) if ctx.rpc else 0
        try:
            if args.command == "discover":
                if args.action == "sample":
                    await sample(ctx, args.windows, args.days, args.seed)
                elif args.action == "candidates":
                    await show_candidates(ctx, args.limit)
                elif args.action == "import":
                    wallets = [validate_address(w) for w in args.wallet] or [
                        c.wallet for c in await ctx.repo.discovery_candidates(limit=args.top)
                    ]
                    await import_histories(ctx, wallets)
                else:
                    await sample(ctx, args.windows, args.days, args.seed)
                    candidates = await ctx.repo.discovery_candidates(limit=args.top)
                    print(f"Importando el historial de {len(candidates)} candidatas…")
                    await import_histories(ctx, [c.wallet for c in candidates])
                    wallets = [w.wallet for w in await ctx.repo.imported_wallets()]
                    await analyze(ctx, wallets, parse_as_of(None), args.token_lookups)
                    await ranking(ctx, args.top)
            elif args.command == "analyze":
                wallets = [validate_address(w) for w in args.wallet] or [
                    w.wallet for w in await ctx.repo.imported_wallets()
                ]
                await analyze(ctx, wallets, parse_as_of(args.as_of), args.token_lookups)
            elif args.action == "ranking":
                await ranking(ctx, args.limit)
            else:
                await show(ctx, args.wallet)
        except BudgetExceededError as exc:
            print(f"Detenido por presupuesto: {exc}")
            return 3
        finally:
            await _report_credits(ctx, spent_before)
    return 0

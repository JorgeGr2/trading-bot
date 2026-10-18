"""Órdenes de descubrimiento y análisis de traders (las usa ``copybot.cli``).

copybot discover sample [--windows N] [--days D] [--seed S]
copybot discover candidates [--limit N]
copybot discover screen [--limit N] [--max-windows N]
copybot discover add <wallet> ... [--file F] [--source leaderboard|manual] [--label T]
copybot discover import [--top N | --wallet W ... | --activity MIN:MAX --sample N |
                         --source leaderboard|manual] [--days D]
copybot discover run [--windows N] [--days D] [--top N] [--token-lookups N]
copybot analyze [--wallet W ...] [--as-of FECHA | --prices] [--token-lookups N]
copybot traders ranking [--limit N]
copybot traders show <wallet>
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
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
from copybot.ingest.discovery import ActivityScreener, RelayerSampler, TokenCreationLookup
from copybot.ingest.history import HistoryImporter
from copybot.ingest.prices import JupiterPrices, PriceRefresher
from copybot.solana.addresses import validate_address
from copybot.solana.rpc import HELIUS_CREDITS_PER_REQUEST, RpcError, SolanaRpc

log = structlog.get_logger(__name__)


def add_parsers(sub: argparse._SubParsersAction) -> None:
    disc = sub.add_parser("discover", help="descubrir traders a partir del relayer de FOMO")
    dsub = disc.add_subparsers(dest="action", required=True)
    sample = dsub.add_parser("sample", help="muestrear ventanas del relayer")
    _sample_args(sample)
    cand = dsub.add_parser("candidates", help="listar las wallets candidatas")
    cand.add_argument("--limit", type=int, default=20)
    screen = dsub.add_parser(
        "screen", help="medir cuánto opera cada wallet vista (consulta barata, sin importar)"
    )
    screen.add_argument("--limit", type=int, default=1000)
    screen.add_argument(
        "--max-windows",
        type=int,
        default=None,
        help="solo wallets vistas en como mucho N ventanas (1 = las menos activas)",
    )
    screen.add_argument("--days", type=int, default=37)
    add = dsub.add_parser("add", help="añadir wallets candidatas a mano (p. ej. de la leaderboard)")
    add.add_argument("wallets", nargs="*")
    add.add_argument("--file", help="fichero con una wallet por línea: dirección[,etiqueta]")
    add.add_argument("--source", choices=["leaderboard", "manual"], default="leaderboard")
    add.add_argument("--label", help="p. ej. «@usuario, puesto 3 a 30 días»")
    imp = dsub.add_parser("import", help="importar el historial de las candidatas")
    group = imp.add_mutually_exclusive_group()
    group.add_argument("--top", type=int, default=20, help="las N más recurrentes del muestreo")
    group.add_argument("--wallet", action="append", default=[])
    group.add_argument(
        "--activity",
        type=parse_band,
        help="banda de transacciones al día, p. ej. 0.7:15 (necesita «discover screen»)",
    )
    group.add_argument("--source", choices=["leaderboard", "manual"])
    imp.add_argument("--sample", type=int, default=100, help="cuántas, con --activity")
    imp.add_argument("--seed", type=int, default=0, help="semilla de la muestra, con --activity")
    imp.add_argument("--days", type=int, default=None, help="días de historial a importar")
    run = dsub.add_parser("run", help="muestrear, importar y analizar de una vez")
    _sample_args(run)
    run.add_argument("--top", type=int, default=20)
    run.add_argument("--token-lookups", type=int, default=15)

    ana = sub.add_parser("analyze", help="calcular métricas y puntuaciones")
    ana.add_argument("--wallet", action="append", default=[])
    ana.add_argument("--as-of", help="fecha ISO 8601 en UTC (por defecto, ahora)")
    ana.add_argument(
        "--prices",
        action="store_true",
        help="valorar las posiciones abiertas con el precio actual de Jupiter (gratis)",
    )
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


def parse_band(text: str) -> tuple[Decimal, Decimal]:
    try:
        low, high = (Decimal(part) for part in text.split(":"))
    except (ValueError, ArithmeticError) as exc:
        raise argparse.ArgumentTypeError("formato MIN:MAX, p. ej. 0.7:15") from exc
    if not 0 <= low <= high:
        raise argparse.ArgumentTypeError("debe cumplirse 0 ≤ MIN ≤ MAX")
    return low, high


def read_wallet_file(path: str) -> list[tuple[str, str | None]]:
    """Una wallet por línea: ``dirección`` o ``dirección,etiqueta``. Ignora vacías y #."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            address, _, label = line.partition(",")
            rows.append((validate_address(address), label.strip() or None))
    return rows


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


async def screen(ctx: Context, limit: int, max_windows: int | None, days: int) -> None:
    screener = ActivityScreener(_rpc(ctx), ctx.repo, days=days)
    wallets = await ctx.repo.wallets_to_screen(limit=limit, max_windows=max_windows)
    print(f"Midiendo la actividad de {len(wallets)} wallets (últimos {days} días)…")
    bands = Counter()
    for i, wallet in enumerate(wallets, start=1):
        try:
            activity = await screener.screen(wallet)
        except RpcError as exc:
            log.warning("screen_failed", wallet=wallet, error=str(exc))
            bands["error"] += 1
            continue
        per_day = activity.per_day
        if activity.capped:
            bands["≥ 27 al día (máximo de la consulta)"] += 1
        elif per_day < Decimal("0.7"):
            bands["< 0,7 al día"] += 1
        elif per_day <= 15:
            bands["0,7 - 15 al día"] += 1
        else:
            bands["> 15 al día"] += 1
        if i % 200 == 0:
            print(f"  {i}/{len(wallets)}")
    for band, count in bands.most_common():
        print(f"  {band}: {count}")


async def add_candidates(ctx: Context, rows: list[tuple[str, str | None]], source: str) -> None:
    new = 0
    for wallet, label in rows:
        new += await ctx.repo.add_candidate(wallet, source=source, label=label)
    print(f"{len(rows)} wallets ({new} nuevas) registradas como «{source}».")


async def import_histories(ctx: Context, wallets: list[str], days: int | None = None) -> list[str]:
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
            r = await importer.import_wallet(wallet, days=days or ctx.settings.history_days)
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
    ctx: Context,
    wallets: list[str],
    as_of: datetime | None,
    token_lookups: int,
    prices: bool = False,
) -> list[WalletAnalysis]:
    """Analiza a fecha ``as_of`` (``None``: ahora). Con ``prices``, antes pide a Jupiter el
    precio actual de las posiciones abiertas y analiza justo después, para que esos
    precios sean anteriores a ``as_of``."""
    if prices and as_of is not None:
        raise SystemExit("--prices usa precios actuales: no se puede combinar con --as-of")
    analyzer = _analyzer(ctx)
    lookup = TokenCreationLookup(ctx.rpc, ctx.repo) if ctx.rpc and token_lookups else None
    provisional = as_of or parse_as_of(None)
    if lookup is not None:
        for wallet in wallets:
            try:
                mints = await analyzer.opened_mints(wallet, provisional)
                await lookup.ensure(mints, max_lookups=token_lookups)
            except BudgetExceededError as exc:
                print(f"Sin presupuesto para más fechas de creación: {exc}")
                break
    if prices:
        open_mints: set[str] = set()
        for wallet in wallets:
            open_mints.update(await analyzer.open_mints(wallet, provisional))
        client = JupiterPrices()
        try:
            asked = await PriceRefresher(client, ctx.repo).refresh(open_mints)
        finally:
            await client.aclose()
        print(f"Precios de Jupiter: {asked} tokens con posición abierta consultados.")
    as_of = as_of or parse_as_of(None)
    results = [await analyzer.analyze(wallet, as_of) for wallet in wallets]
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
        mean = m.get("prudent_mean_return")
        return (
            not s.eligible,
            -(s.score if s.score is not None else Decimal(-1)),
            -(Decimal(mean) if mean is not None else Decimal(-100)),
        )

    rows.sort(key=order)
    origin = {
        w: c.source for w, c in (await ctx.repo.candidate_info(r[0].wallet for r in rows)).items()
    }
    print(
        f"{'#':>3}  {'wallet':44}  {'origen':11}  {'puntos':>6}  {'oper.':>5}  {'prudente':>9}  "
        f"{'1ª venta':>9}  motivo de exclusión"
    )
    for i, (s, m) in enumerate(rows[:limit], start=1):
        first = m.get("median_seconds_to_first_sell")
        first_txt = "—" if first is None else f"{Decimal(first):.0f} s"
        reason = s.explanation[1].strip(" -") if not s.eligible and len(s.explanation) > 1 else ""
        points = f"{s.score:6.2f}" if s.score is not None else "excl."
        source = origin.get(s.wallet, "relayer")
        print(
            f"{i:3}  {s.wallet:44}  {source:11}  {points:>6}  {m.get('round_trips', 0):5}  "
            f"{_fmt_pct(m.get('prudent_mean_return')):>9}  {first_txt:>9}  {reason}"
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
    info = (await ctx.repo.candidate_info([wallet])).get(wallet)
    if info is not None:
        label = f" ({info.label})" if info.label else ""
        print(f"  Origen: {info.source}{label}")
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
    needs_helius = args.command == "discover" and args.action not in ("candidates", "add")
    needs_helius = needs_helius or (args.command == "analyze" and args.token_lookups > 0)
    async with context(settings, needs_helius=needs_helius) as ctx:
        spent_before = sum(ctx.rpc.credits.values()) if ctx.rpc else 0
        try:
            if args.command == "discover":
                if args.action == "sample":
                    await sample(ctx, args.windows, args.days, args.seed)
                elif args.action == "candidates":
                    await show_candidates(ctx, args.limit)
                elif args.action == "screen":
                    await screen(ctx, args.limit, args.max_windows, args.days)
                elif args.action == "add":
                    rows = [(validate_address(w), args.label) for w in args.wallets]
                    if args.file:
                        rows += read_wallet_file(args.file)
                    await add_candidates(ctx, rows, args.source)
                elif args.action == "import":
                    if args.wallet:
                        wallets = [validate_address(w) for w in args.wallet]
                    elif args.activity:
                        low, high = args.activity
                        wallets = await ctx.repo.candidates_by_activity(
                            min_per_day=low, max_per_day=high, limit=args.sample, seed=args.seed
                        )
                        print(
                            f"{len(wallets)} wallets elegidas al azar entre las que hacen "
                            f"{low}-{high} transacciones al día."
                        )
                    elif args.source:
                        wallets = await ctx.repo.candidates_from(args.source)
                    else:
                        wallets = [
                            c.wallet for c in await ctx.repo.discovery_candidates(limit=args.top)
                        ]
                    await import_histories(ctx, wallets, args.days)
                else:
                    await sample(ctx, args.windows, args.days, args.seed)
                    candidates = await ctx.repo.discovery_candidates(limit=args.top)
                    print(f"Importando el historial de {len(candidates)} candidatas…")
                    await import_histories(ctx, [c.wallet for c in candidates])
                    wallets = [w.wallet for w in await ctx.repo.imported_wallets()]
                    await analyze(ctx, wallets, None, args.token_lookups, prices=True)
                    await ranking(ctx, args.top)
            elif args.command == "analyze":
                wallets = [validate_address(w) for w in args.wallet] or [
                    w.wallet for w in await ctx.repo.imported_wallets()
                ]
                as_of = parse_as_of(args.as_of) if args.as_of else None
                await analyze(ctx, wallets, as_of, args.token_lookups, prices=args.prices)
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

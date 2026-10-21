"""Informe del backtest del copy-trading (markdown reproducible)."""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from copybot.backtest.copy import PORTFOLIO, SIGNALS, CopyPosition, CopyResult
from copybot.backtest.data import Selection
from copybot.backtest.references import RefParams, RefResult
from copybot.backtest.stats import TradeStats, max_drawdown_pct, trade_stats

ZERO = Decimal(0)


@dataclass(frozen=True)
class Scenario:
    name: str
    result: CopyResult


@dataclass(frozen=True)
class Coverage:
    asset: str
    hours_with_price: int
    hours: int


def _short(address: str) -> str:
    return f"{address[:4]}…{address[-4:]}"


def _usd(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:+,.2f} $"


def _pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:+.1f} %"


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def book_stats(positions: list[CopyPosition], seed: int) -> TradeStats:
    done = [p for p in positions if p.status in ("closed", "written_off")]
    return trade_stats([p.pnl_usd for p in done], [p.return_pct for p in done], seed=seed)


def data_hash(result: CopyResult, selection: list[Selection]) -> str:
    """Huella de los datos y parámetros: el mismo valor implica el mismo informe."""
    payload = {
        "params": {k: str(v) for k, v in result.params.__dict__.items()},
        "periodo": [result.start, result.end],
        "seleccion": [(s.wallet, str(s.score)) for s in selection],
        "posiciones": [
            (p.book, p.wallet, p.mint, p.opened_at, str(p.entry_price), str(p.pnl_usd))
            for p in result.positions
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def render(
    scenarios: list[Scenario],
    selection: list[Selection],
    *,
    selected: list[str],
    select_as_of: datetime | None,
    select_window: int | None,
    lookups: int,
    missing: int,
    seed: int,
    references: list[tuple[RefResult, RefResult]] | None = None,
    ref_params: RefParams | None = None,
    coverage: list[Coverage] | None = None,
) -> str:
    main = scenarios[0].result
    out = [
        "# Backtest del copy-trading",
        "",
        "Resultados **estimados** con precios históricos de operaciones de otros usuarios; "
        "no se mezclan con la simulación en vivo.",
        "",
        f"- Periodo copiado: {_date(main.start)} → {_date(main.end)} UTC",
    ]
    if select_as_of is not None:
        out.append(
            f"- Selección **fuera de muestra**: traders elegidos con sus datos hasta el "
            f"{select_as_of:%Y-%m-%d} (ventana de {select_window} días); se copian después."
        )
    else:
        out.append("- Traders elegidos a mano (no es una selección fuera de muestra).")
    out += [
        f"- Retraso: precio de las operaciones de otros entre {main.params.delay[0]} y "
        f"{main.params.delay[1]} s después (hasta 60 s si no hubo ninguna)",
        f"- Consultas nuevas a Helius: {lookups} (≈{lookups * 10:,} créditos); precios que "
        f"faltaban en caché sin consultar: {missing}",
        f"- Semilla del bootstrap: {seed}; huella de los datos: `{data_hash(main, selection)}`",
        "",
        "## Traders",
        "",
        "| Trader | Puntuación a la fecha de selección | Operaciones | Rentabilidad prudente "
        "| ¿Copiado? |",
        "|---|---|---|---|---|",
    ]
    others = 0
    for s in selection:
        if s.wallet not in selected and not (s.score or 0) > 0:
            others += 1
            continue
        prudent = None if s.prudent_mean_return is None else s.prudent_mean_return * 100
        score = "—" if s.score is None else f"{s.score:.1f}"
        copied = "sí" if s.wallet in selected else "no"
        out.append(
            f"| `{_short(s.wallet)}` | {score} | {s.round_trips} | {_pct(prudent)} | {copied} |"
        )
    if others:
        out.append(f"\n{others} traders más sin ventaja a esa fecha (puntuación 0 o excluidos).")
    out += ["", "## Resultados por escenario", ""]
    out.append(
        "| Escenario | Libro | Operaciones | Resultado | Media por operación (IC 95 %) "
        "| Acierto | Profit factor | P(media > 0) |"
    )
    out.append("|---|---|---|---|---|---|---|---|")
    for sc in scenarios:
        for book, label in ((SIGNALS, "señales"), (PORTFOLIO, "cartera")):
            st = book_stats(sc.result.book(book), seed)
            ci = (
                f"{_pct(st.mean_return_pct)} ({_pct(st.ci_low_pct)} a {_pct(st.ci_high_pct)})"
                if st.n
                else "—"
            )
            out.append(
                f"| {sc.name} | {label} | {st.n} | {_usd(st.total_usd)} | {ci} | "
                f"{'—' if st.win_rate is None else f'{st.win_rate:.0%}'} | "
                f"{'—' if st.profit_factor is None else f'{st.profit_factor:.2f}'} | "
                f"{'—' if st.prob_mean_positive is None else f'{st.prob_mean_positive:.0%}'} |"
            )
    out.append("")
    for sc in scenarios:
        r = sc.result
        final = r.equity_curve[-1][1] if r.equity_curve else r.initial_usd
        halts = "; ".join(f"{_date(t)}: {why}" for t, why in r.halts) or "ninguna"
        warning = book_stats(r.book(SIGNALS), seed).warning
        out.append(
            f"- **{sc.name}**: cartera {r.initial_usd:,.0f} $ → {final:,.2f} $ "
            f"(caída máxima {max_drawdown_pct(r.equity_curve):.1f} %); paradas: {halts}."
            + (f" Aviso: {warning}." if warning else "")
        )

    if references or coverage:
        out += _references_section(scenarios, references or [], ref_params, coverage or [])

    sig = main.book(SIGNALS)
    comparable = [p for p in sig if p.status != "open" and p.theo_pnl_usd is not None]
    slippages = [p.entry_slippage_pct for p in sig if p.entry_slippage_pct is not None]
    out += [
        "",
        f"## Copia realista frente a teórica ({scenarios[0].name}, libro de señales)",
        "",
    ]
    if comparable:
        real = sum((p.pnl_usd for p in comparable), ZERO)
        theo = sum((p.theo_pnl_usd for p in comparable), ZERO)  # type: ignore[misc]
        out.append(
            f"- Sobre {len(comparable)} operaciones con precio del trader: realista "
            f"{_usd(real)} frente a teórica {_usd(theo)} (diferencia {_usd(real - theo)})."
        )
    if slippages:
        q = statistics.quantiles([float(x) for x in slippages], n=4) if len(slippages) > 1 else None
        median = statistics.median([float(x) for x in slippages])
        out.append(
            f"- Precio de entrada frente al del trader: mediana {median:+.1f} %"
            + (f" (cuartiles {q[0]:+.1f} % y {q[2]:+.1f} %)" if q else "")
            + ". Es lo que se paga de más por llegar 4-8 s tarde, antes de nuestros costes."
        )
    out += ["", "## Por trader (libro de señales)", ""]
    out.append(
        "| Trader | Operaciones | Realista | Teórica | Entrada frente al trader (mediana) "
        "| Media por operación |"
    )
    out.append("|---|---|---|---|---|---|")
    per: dict[str, list[CopyPosition]] = defaultdict(list)
    for p in sig:
        per[p.wallet].append(p)
    for wallet in sorted(per, key=lambda w: sum((p.pnl_usd for p in per[w]), ZERO)):
        ps = per[wallet]
        theo = [p.theo_pnl_usd for p in ps if p.theo_pnl_usd is not None]
        slips = [float(p.entry_slippage_pct) for p in ps if p.entry_slippage_pct is not None]
        st = book_stats(ps, seed)
        out.append(
            f"| `{_short(wallet)}` | {st.n} | {_usd(sum((p.pnl_usd for p in ps), ZERO))} | "
            f"{_usd(sum(theo, ZERO)) if theo else '—'} | "
            f"{f'{statistics.median(slips):+.1f} %' if slips else '—'} | "
            f"{_pct(st.mean_return_pct)} |"
        )
    if main.skipped:
        out += ["", "## Compras no copiadas", ""]
        for why, n in sorted(main.skipped.items(), key=lambda kv: -kv[1]):
            out.append(f"- {n} × {why}")
    return "\n".join(out) + "\n"


def _references_section(
    scenarios: list[Scenario],
    refs: list[tuple[RefResult, RefResult]],
    params: RefParams | None,
    coverage: list[Coverage],
) -> list[str]:
    out = [
        "",
        "## Frente a otras inversiones (mismo periodo y mismo capital)",
        "",
        "| Estrategia | Activo | Final | Rentabilidad | Caída máxima | Operaciones "
        "| Final con costes al doble |",
        "|---|---|---|---|---|---|---|",
    ]
    for sc in scenarios:
        r = sc.result
        final = r.equity_curve[-1][1] if r.equity_curve else r.initial_usd
        ret = (final / r.initial_usd - 1) * 100
        out.append(
            f"| copy-trading: {sc.name} | memecoins | {final:,.2f} $ | {_pct(ret)} | "
            f"{max_drawdown_pct(r.equity_curve):.1f} % | {len(r.book(PORTFOLIO))} | — |"
        )
    for base, doubled in sorted(refs, key=lambda pair: -pair[0].final_usd):
        out.append(
            f"| {base.strategy} | {base.asset} | {base.final_usd:,.2f} $ | "
            f"{_pct(base.return_pct)} | {base.max_drawdown_pct:.1f} % | {base.trades} | "
            f"{doubled.final_usd:,.2f} $ |"
        )
    if scenarios:
        initial = scenarios[0].result.initial_usd
        out.append(f"| dejarlo en USDC | — | {initial:,.2f} $ | +0.0 % | 0.0 % | 0 | — |")
    out.append("")
    if params is not None:
        out.append(
            f"- Costes de las referencias: {params.cost_pct} % por operación (comisión del "
            f"pool y deslizamiento) y {params.tx_fee_usd} $ de red; al final se vende todo. "
            "Precios: el primer swap de cada hora en pools contra USDC de Solana (cbBTC para "
            "BTC, ETH de Wormhole, SPYx de xStocks, que solo sigue al S&P 500 con la bolsa "
            "abierta). La tendencia decide con el precio de una hora y opera en la siguiente."
        )
    for cov in coverage:
        if cov.hours_with_price < cov.hours:
            out.append(
                f"- {cov.asset}: precio en {cov.hours_with_price} de {cov.hours} horas"
                + (" (sin datos: no se compara)" if not cov.hours_with_price else "")
                + "."
            )
    return out

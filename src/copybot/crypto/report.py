"""Informe diario «Análisis del mercado» en castellano llano (maqueta 10.1 del diseño).

Nunca promete beneficios: dice cuál es **la mejor puntuada hoy según la regla v1** (o
«hoy ninguna: quédate en USDT»), con lo que esa regla hizo antes y en vivo, y sus riesgos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from copybot.crypto import explain as ex
from copybot.crypto.analysis import Analysis, CoinInfo
from copybot.crypto.strategy import STRATEGY_V1

REGISTERED_ON = "22-oct-2026"
TOP = 10
WHY = 5


@dataclass
class PaperInfo:
    """Lo que hizo hoy la cartera de prueba (ya ejecutado)."""

    holdings: list[str]
    sells: list[tuple[str, str, float | None, float | None]]  # moneda, motivo, $, %
    buys: list[tuple[str, float]]  # moneda, importe
    rejected: list[tuple[str, str]] = field(default_factory=list)
    equity: float | None = None
    btc_equity: float | None = None
    cash: float | None = None
    day_loss: float | None = None  # pérdida del día (0,012 = 1,2 %)
    drawdown: float | None = None
    positions: dict[str, float] = field(default_factory=dict)  # moneda → valor
    status: str = "RUNNING"


@dataclass
class HistoryInfo:
    """«¿Acertó?» con los análisis emitidos en vivo."""

    week_ago: list[tuple[str, float]] = field(default_factory=list)  # moneda, rentabilidad
    week_ago_btc: float | None = None
    top1_beats: int = 0
    top1_total: int = 0


@dataclass
class BacktestInfo:
    run_day: date | None = None
    excess_annual: float | None = None
    ci: tuple[float, float] | None = None
    null_percentile: float | None = None
    light: str | None = None
    k: int = 3
    rows: list[tuple[str, float | None, float | None, float | None, float | None]] = field(
        default_factory=list
    )  # periodo, regla, BTC, BTC con filtro, cesta


def _coin_name(c: CoinInfo) -> str:
    return c.base


def render(
    a: Analysis,
    *,
    generated_at: datetime,
    paper: PaperInfo | None = None,
    history: HistoryInfo | None = None,
    backtest: BacktestInfo | None = None,
    semaforo_line: str | None = None,
    confidence: str = "SIN DATOS",
) -> str:
    v = a.view
    reg = v.regime
    ranked = a.ranked
    out = [
        f"ANÁLISIS DEL MERCADO — {ex.fmt_date_long(a.day + timedelta(days=1))}",
        f"{ex.cutoff_text(a.day)} · Regla v1 (registrada el {REGISTERED_ON})",
        f"{len(v.universe.members)} monedas evaluadas (de {a.pairs} pares con USDT) · "
        + ("Datos completos" if a.status == "ok" else "DATOS INCOMPLETOS: hoy no se decide"),
        "",
    ]
    if backtest and backtest.excess_annual is not None and backtest.excess_annual <= 0:
        out += ["**En el pasado, esta regla lo hizo peor que comprar BTC y esperar.**", ""]
    out.append("RESPUESTA CORTA")
    if reg is None:
        out.append("Mercado: SIN DATOS de BTC hoy. No se decide nada.")
    elif reg.favorable:
        out.append(
            f"Mercado: FAVORABLE. Bitcoin ({ex.fmt_usd(reg.close, 0)}) cerró por encima de su "
            f"media de 100 días ({ex.fmt_usd(reg.sma, 0)})."
        )
    else:
        out.append(
            f"**Hoy ninguna: quédate en USDT.** BTC cerró por debajo de su media de 100 días "
            f"({ex.fmt_usd(reg.close, 0)} frente a {ex.fmt_usd(reg.sma, 0)}). La cartera vende "
            "lo que tenga y espera."
        )
    eligible = [c for c in ranked if c.entry_eligible]
    if reg is not None and reg.favorable:
        if eligible:
            best = eligible[0]
            out.append(
                f"La mejor puntuada hoy según la regla v1: {_coin_name(best)} "
                f"({ex.fmt_points(best.score)} de 100)."
            )
        else:
            out.append("Hoy ninguna de las primeras cumple la tendencia: quédate en USDT.")
    if paper is not None:
        held = ", ".join(paper.holdings) if paper.holdings else "nada (todo en USDT)"
        out.append(f"La cartera de prueba tiene: {held}.")
    out += [
        "Nadie puede saber de antemano qué moneda ganará más: esto es lo que elige una regla fija.",
        "Abajo tienes cómo le ha ido a esa regla.",
        ex.confidence_text(confidence),
        "",
    ]
    title = (
        "LAS 10 MEJOR PUNTUADAS"
        if reg is not None and reg.favorable
        else "LAS MÁS FUERTES (solo informativo: hoy la regla no compra)"
    )
    out += [
        title,
        "",
        "| # | Moneda | Puntos | 7 días | 30 días | 90 días | Se mueve al día | Tendencia "
        "| Frente a BTC (30 d) | Volumen/día | Riesgo | Cartera |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    held_set = set(paper.holdings) if paper else set()
    bought = {s for s, _ in paper.buys} if paper else set()
    sold = {s for s, *_ in paper.sells} if paper else set()
    for c in ranked[:TOP]:
        if c.base in bought:
            action = "compra"
        elif c.base in sold:
            action = "vende"
        elif c.base in held_set:
            action = "mantiene"
        else:
            action = "—"
        vs = "—" if c.base == "BTC" else ex.fmt_pp(c.vs_btc_30d)
        out.append(
            f"| {c.rank} | {c.base} | {ex.fmt_points(c.score)} | {ex.fmt_pct(c.ret_7d, 0)} | "
            f"{ex.fmt_pct(c.ret_30d, 0)} | {ex.fmt_pct(c.ret_90d, 0)} | "
            f"{ex.fmt_vol(c.vol_daily)} | {ex.fmt_trend(c.trend_share)} | {vs} | "
            f"{ex.fmt_musd(c.median_qv)} | {c.risk_level or '—'} | {action} |"
        )
    out += [
        "",
        "Puntos = posición frente a las demás (0-100) en subida ajustada por lo que suele "
        "moverse. No es la probabilidad de que suba.",
        "",
        "POR QUÉ",
    ]
    size = 18.0
    for c in ranked[:WHY]:
        out.append(
            ex.why_text(
                c.base,
                ret_30d=c.ret_30d,
                momentum_pct=c.momentum_pct,
                above_sma=c.above_sma,
                median_qv=c.median_qv or 0.0,
                order_usd=size,
                best_share=c.best_day_share,
                p5=c.p5_week,
            )
        )
    if paper is not None:
        out += ["", "QUÉ HACE HOY LA CARTERA DE PRUEBA (100 $ ficticios)"]
        for coin, why, pnl, pnl_pct in paper.sells:
            res = (
                ""
                if pnl is None
                else f" Resultado: {ex.fmt_usd(pnl, sign=True)} ({ex.fmt_pct(pnl_pct)})."
            )
            out.append(f"Vende {coin}: {why}.{res}")
        for coin, amount in paper.buys:
            out.append(f"Compra {coin} por {ex.fmt_usd(amount)}.")
        for coin, why in paper.rejected:
            out.append(f"No se pudo operar {coin}: {why}.")
        keep = [h for h in paper.holdings if h not in bought]
        if keep:
            out.append(f"Mantiene {', '.join(keep)}.")
        if not (paper.sells or paper.buys or keep):
            out.append("No tiene nada y hoy no compra: todo en USDT.")
        if paper.equity is not None:
            line = f"Valor: {ex.fmt_usd(paper.equity)}"
            if paper.btc_equity is not None:
                line += f" · Con BTC comprado el mismo día: {ex.fmt_usd(paper.btc_equity)}"
            if paper.cash is not None:
                line += f" · Efectivo: {ex.fmt_usd(paper.cash)}"
            out.append(line + ".")
    out += ["", "¿ACERTÓ? (solo análisis emitidos en vivo)"]
    if history is None or not (history.week_ago or history.top1_total):
        out.append("Aún no hay análisis en vivo de hace 7 días.")
    else:
        if history.week_ago:
            picks = ", ".join(f"{s} {ex.fmt_pct(r)}" for s, r in history.week_ago)
            out.append(f"Hace 7 días eligió: {picks}. BTC: {ex.fmt_pct(history.week_ago_btc)}.")
        if history.top1_total:
            out.append(
                f"De las últimas {history.top1_total} nº 1, {history.top1_beats} ganaron a BTC "
                "en los 7 días siguientes. Muestra pequeña: no demuestra nada."
            )
    out += ["", "QUÉ HIZO ESTA REGLA EN EL PASADO (backtest con costes; pseudo-fuera de muestra*)"]
    if backtest is None or backtest.excess_annual is None:
        out.append("Aún no hay backtest registrado de esta versión.")
    else:
        if backtest.rows:
            out += [
                "| Periodo (al año) | Regla v1 | BTC | BTC con filtro | 10 más negociadas |",
                "|---|---|---|---|---|",
            ]
            for name, r1, r2, r3, r4 in backtest.rows:
                out.append(
                    f"| {name} | {ex.fmt_pct(r1)} | {ex.fmt_pct(r2)} | {ex.fmt_pct(r3)} | "
                    f"{ex.fmt_pct(r4)} |"
                )
        ci = (
            ""
            if backtest.ci is None
            else f" (margen de error: {ex.fmt_pct(backtest.ci[0])} a {ex.fmt_pct(backtest.ci[1])})"
        )
        out.append(f"Diferencia con BTC: {ex.fmt_pct(backtest.excess_annual)} al año{ci}.")
        if backtest.null_percentile is not None:
            out.append(
                f"Frente a elegir al azar: mejor que el {ex.fmt_num(backtest.null_percentile)} % "
                "de 1.000 carteras al azar (hace falta ≥ 95 para decir que elegir aporta)."
            )
        out.append(
            f"Versiones probadas: {backtest.k} (la regla y 2 variantes informativas). "
            "* Quien eligió la regla ya conocía esos años. Lo pasado no garantiza lo futuro."
        )
    if paper is not None:
        out += ["", "RIESGOS DE HOY", f"- {ex.crash_text(paper.positions, paper.cash or 0.0)}"]
        out.append(
            "- Las monedas suelen moverse con BTC: repartir entre altcoins no protege en un "
            "desplome."
        )
        if paper.day_loss is not None and paper.drawdown is not None:
            out.append(
                f"- Pérdida de hoy: {ex.fmt_pct(paper.day_loss, sign=False)} "
                "(sin compras al 20 %). "
                f"Caída desde el máximo: {ex.fmt_pct(paper.drawdown, 0, sign=False)} "
                "(la cartera se para al 50 %)."
            )
    if semaforo_line:
        out += ["", semaforo_line]
    out += [
        "",
        "Esto no es una recomendación de inversión. Ninguna regla sabe qué moneda subirá.",
        f"(Generado {ex.fmt_date_short(generated_at, year=True)} a las "
        f"{ex.fmt_clock(generated_at)}, hora de Madrid.)",
    ]
    text = "\n".join(out) + "\n"
    return ex.check_text(text)


def to_json(a: Analysis) -> dict:
    reg = a.view.regime
    return {
        "dia": a.day.isoformat(),
        "estado": a.status,
        "mercado": None
        if reg is None
        else {"btc": reg.close, "media100": reg.sma, "favorable": reg.favorable},
        "evaluadas": len(a.view.universe.members),
        "mejores": [
            {
                "puesto": c.rank,
                "moneda": c.base,
                "puntos": round(c.score or 0, 2),
                "tendencia": c.trend_share,
                "compra_hoy": c.entry_eligible,
                "riesgo": c.risk_level,
            }
            for c in a.ranked[:TOP]
        ],
    }


def coin_text(a: Analysis, symbol: str) -> str:
    """Situación de una moneda (``copybot crypto coin``)."""
    c = a.coins.get(symbol) or a.coins.get(symbol + "USDT")
    if c is None:
        return f"{symbol}: no cotizaba contra USDT el {ex.fmt_date_short(a.day, year=True)}."
    status = ex.coin_status(
        exclusion_reason=c.exclusion_reason,
        rank=c.rank,
        n=len(a.view.universe.members),
        score=c.score,
        trend_share=c.trend_share,
    )
    return f"{c.base} ({ex.fmt_date_short(a.day, year=True)}): {status}"


STRATEGY = STRATEGY_V1

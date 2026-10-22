"""Informe del backtest de la regla v1, en castellano y tal como salga."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import date

from copybot.crypto.backtest import BacktestResult, Excess, curve_metrics
from copybot.crypto.strategy import STRATEGY_VERSION, VARIANT_NAMES

STRESS_NAMES = {
    "costes_x2": "Costes ×2 (obligatoria)",
    "costes_x3": "Costes ×3 (informativa)",
    "un_dia_tarde": "Ejecución con un día de retraso",
    "sin_top3": "Sin las 3 monedas que más aportaron",
    "solo_supervivientes": "Solo monedas que hoy siguen cotizando",
    "retirada_0": "Retiradas sin pérdida",
    "retirada_100": "Retiradas con pérdida total",
}
BENCH_ORDER = ("btc", "btc_filtro", "btc90", "cesta10", "usdt")


def num(x: float | None, decimals: int = 1) -> str:
    if x is None:
        return "—"
    text = f"{x:,.{decimals}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def pct(x: float | None, decimals: int = 1, sign: bool = True) -> str:
    if x is None:
        return "—"
    s = num(x * 100, decimals)
    if sign and x > 0:
        s = "+" + s
    return f"{s} %"


def usd(x: float | None) -> str:
    return "—" if x is None else f"{num(x, 2)} $"


def _ci(e: Excess) -> str:
    return f"{pct(e.annual)} (margen de error: {pct(e.ci_low)} a {pct(e.ci_high)})"


def render(
    r: BacktestResult,
    *,
    k: int,
    dsr: float | None,
    min_trl: float | None,
    commit: str | None,
    semaforo_md: str | None = None,
    adhoc: bool = False,
) -> str:
    a = r.curve_a
    m_a = curve_metrics(a.curve, a.worst)
    sizes = list(r.universe_sizes.values())
    out = [f"# Backtest de la regla {STRATEGY_VERSION}", ""]
    if adhoc:
        out += ["**PRUEBA AD HOC — NO CUENTA.** Sube K y no sirve para el semáforo.", ""]
    if r.excess_a.annual <= 0:
        out += ["**En el pasado, esta regla lo hizo peor que comprar BTC y esperar.**", ""]
    out += [
        f"- Periodo: {r.start:%d-%m-%Y} → {r.end:%d-%m-%Y} ({len(r.days)} días). Empieza el "
        f"primer día con al menos 20 monedas evaluables.",
        f"- Monedas evaluables por día: mediana {statistics.median(sizes):.0f} "
        f"(de {min(sizes)} a {max(sizes)}).",
        "- **Pseudo-fuera de muestra:** los parámetros no se ajustaron a estos datos, pero "
        "quien los eligió ya conocía esos años. Lo único fuera de muestra de verdad será la "
        "prueba en vivo desde el preregistro.",
        f"- Configuraciones probadas (K): {k}. Semilla: {r.seed}. Commit: "
        f"`{(commit or 'desconocido')[:12]}`.",
        f"- Huellas: datos `{r.data_hash[:16]}`, regla `{r.params_hash[:16]}`, costes "
        f"`{r.cost_table_hash[:16]}`.",
        "- Costes: 0,10 % de comisión por lado más el diferencial y deslizamiento de su tramo "
        "de liquidez. Decisión con la vela cerrada; ejecución en la apertura siguiente.",
        "",
        "## Resultado frente a BTC",
        "",
        "| Estrategia | 100 $ se convierten en | Al año | Caída máxima | Sharpe | Peor mes |",
        "|---|---|---|---|---|---|",
        f"| **Regla v1** (método, tamaño en %) | {usd(a.final)} | {pct(m_a['anual'])} | "
        f"{pct(m_a['caida_maxima'], sign=False)} | {num(m_a['sharpe'], 2)} | "
        f"{pct(m_a['peor_mes'])} |",
    ]
    m_b = curve_metrics(r.curve_b.curve, r.curve_b.worst)
    out.append(
        f"| Tu cartera de 100 $ (topes vigentes) | {usd(r.curve_b.final)} | {pct(m_b['anual'])} "
        f"| {pct(m_b['caida_maxima'], sign=False)} | {num(m_b['sharpe'], 2)} | "
        f"{pct(m_b['peor_mes'])} |"
    )
    for key in BENCH_ORDER:
        b = r.benchmarks[key]
        m = curve_metrics(b.curve)
        out.append(
            f"| {b.name} | {usd(b.final)} | {pct(m['anual'])} | "
            f"{pct(m['caida_maxima'], sign=False)} | {num(m['sharpe'], 2)} | "
            f"{pct(m['peor_mes'])} |"
        )
    e = r.excess_a
    out += [
        "",
        f"- **Diferencia con BTC:** {_ci(e)} al año. El margen es el más ancho de tres "
        "bootstrap estacionarios (bloques de 7, 28 y 56 días).",
        f"- Ratio de información: {num(e.ir, 2)}. Sharpe deflactado con K = {k}: "
        f"{num(dsr, 3)} (hace falta ≥ 0,95).",
        "- Días necesarios para poder afirmarlo: "
        + (
            "no se puede demostrar: en el pasado no ganó a BTC."
            if min_trl is None
            else f"unos {num(min_trl, 0)}."
        ),
    ]
    if r.curve_b.halted_on is not None:
        out.append(
            f"- **La cartera de 100 $ se habría parado** el {r.curve_b.halted_on:%d-%m-%Y} "
            "(caída del 50 %) y no habría vuelto a comprar sin tu permiso."
        )
    if a.max_dd_reached_50:
        out.append("- El método llegó a caer un 50 % o más desde un máximo.")
    out += _periods(r) + _variants(r, k) + _stress(r) + _null(r) + _signals(r)
    out += _calibration(r) + _starts(r) + _trades(r) + _survival(r)
    if semaforo_md:
        out += ["", semaforo_md]
    out += [
        "",
        "## Limitaciones",
        "",
        "- Velas diarias: no se ve lo que pasa dentro del día; una caída y su rebote en "
        "horas no disparan nada.",
        "- Los costes son de la tabla calibrada con fotos recientes del libro; en los "
        "desplomes los diferenciales se abren más.",
        "- Los pares retirados que Binance ya no muestra no están; eso favorece a la "
        "estrategia (sesgo de supervivencia no corregido del todo).",
        "- Lo pasado no garantiza lo futuro.",
    ]
    return "\n".join(out) + "\n"


def _periods(r: BacktestResult) -> list[str]:
    out = [
        "",
        "## Por periodos (al año)",
        "",
        "| Periodo | Regla v1 | BTC | BTC con filtro | 10 más negociadas | Diferencia con BTC |",
        "|---|---|---|---|---|---|",
    ]
    for name, p in r.subperiods.items():
        out.append(
            f"| {name} ({p['desde']:%m/%Y}-{p['hasta']:%m/%Y}) | {pct(p['regla']['anual'])} | "
            f"{pct(p['btc']['anual'])} | {pct(p['btc_filtro']['anual'])} | "
            f"{pct(p['cesta10']['anual'])} | {pct(p['exceso'])} |"
        )
    return out


def _variants(r: BacktestResult, k: int) -> list[str]:
    out = [
        "",
        f"## Variantes registradas (informativas; K = {k})",
        "",
        "| Variante | 100 $ se convierten en | Diferencia con BTC al año |",
        "|---|---|---|",
    ]
    for key, run in r.variants.items():
        out.append(f"| {VARIANT_NAMES[key]} | {usd(run.final)} | {_ci(r.excess_variants[key])} |")
    out.append("")
    out.append(
        "Ninguna variante sustituye a la regla: para cambiarla hace falta una versión nueva "
        "que empieza de cero en vivo."
    )
    return out


def _stress(r: BacktestResult) -> list[str]:
    out = [
        "",
        "## Pruebas de estrés (fijadas de antemano)",
        "",
        "| Prueba | 100 $ se convierten en | Diferencia con BTC al año | ¿Supera? |",
        "|---|---|---|---|",
    ]
    for key, run in r.stress.items():
        ex = r.excess_stress[key]
        out.append(
            f"| {STRESS_NAMES.get(key, key)} | {usd(run.final)} | {pct(ex.annual)} | "
            f"{'sí' if ex.annual > 0 else 'no'} |"
        )
    if r.top3:
        out.append(f"\nLas 3 que más aportaron: {', '.join(s[:-4] for s in r.top3)}.")
    return out


def _null(r: BacktestResult) -> list[str]:
    if not r.null_finals:
        return []
    finals = sorted(r.null_finals)
    p95 = finals[int(0.95 * (len(finals) - 1))]
    return [
        "",
        "## ¿Elegir moneda aporta algo?",
        "",
        f"Con las mismas reglas pero eligiendo las monedas **al azar** ({len(finals)} carteras), "
        f"la regla queda mejor que el {num(r.null_percentile, 0)} % de ellas (hace falta ≥ 95). "
        f"Mediana al azar: {usd(statistics.median(finals))}; percentil 95: {usd(p95)}; "
        f"regla: {usd(r.curve_a.final)}.",
    ]


def _signals(r: BacktestResult) -> list[str]:
    s = r.signals_excess
    if "exceso_medio" not in s:
        return ["", "## Libro de señales", "", "Muestra insuficiente."]
    lo7, hi7 = s["ic"][7]
    lo28, hi28 = s["ic"][28]
    return [
        "",
        "## Libro de señales (las 5 primeras cada día, 7 días, 10 $ cada una)",
        "",
        f"- {s['operaciones']} operaciones en {s['dias']} días. Ganaron a BTC el "
        f"{pct(s['bate_btc'], 0, sign=False)} de las veces.",
        f"- Exceso medio por operación frente a BTC (logarítmico): {pct(s['exceso_medio'], 2)}; "
        f"margen con bloques de 7 días {pct(lo7, 2)} a {pct(hi7, 2)}, de 28 días "
        f"{pct(lo28, 2)} a {pct(hi28, 2)}.",
        "- Las operaciones de días seguidos se solapan: no son independientes.",
    ]


def _calibration(r: BacktestResult) -> list[str]:
    out = [
        "",
        "## Calibración por puesto (7 días, frente a BTC, con costes)",
        "",
        "| Puesto | Días | Exceso medio | Margen (bloques de 28 días) | Bate a BTC |",
        "|---|---|---|---|---|",
    ]
    order = ["1", "2-5", "6-12", "13-30", "31+"]
    for key in order:
        c = r.calibration.get(key)
        if c is None:
            continue
        lo, hi = c["ic"]
        out.append(
            f"| {key} | {c['dias']} | {pct(c['exceso_medio'], 2)} | {pct(lo, 2)} a "
            f"{pct(hi, 2)} | {pct(c['bate_btc'], 0, sign=False)} |"
        )
    if r.ic:
        lo, hi = r.ic["ic"]
        out.append(
            f"\nIC de rangos (Spearman entre puntos y rentabilidad a 7 días): media "
            f"{num(r.ic['media'], 3)} (margen {num(lo, 3)} a {num(hi, 3)}); positivo el "
            f"{pct(r.ic['dias_positivo'], 0, sign=False)} de los días."
        )
    if r.quintiles:
        out.append(
            "Rentabilidad media a 7 días por quintil de puntos (de peor a mejor): "
            + " · ".join(pct(q, 2) for q in r.quintiles)
            + ". Solo describe: no se usa para elegir nada."
        )
    return out


def _starts(r: BacktestResult) -> list[str]:
    if not r.starts:
        return []
    out = [
        "",
        "## Si hubieras lanzado una prueba cualquier lunes (cartera de 100 $)",
        "",
        "| Duración | Pruebas | Pierden dinero | Mediana | Peor | P5-P95 | Baten a BTC "
        "| Parada diaria | Parada del 50 % |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for h, s in sorted(r.starts.items()):
        out.append(
            f"| {h} días | {s['arranques']} | {pct(s['pierden'], 0, sign=False)} | "
            f"{pct(s['mediana'])} | {pct(s['peor'])} | {pct(s['p5'])} a {pct(s['p95'])} | "
            f"{pct(s['baten_btc'], 0, sign=False)} | "
            f"{pct(s['tocan_parada_diaria'], 0, sign=False)} | "
            f"{pct(s['tocan_parada_50'], 0, sign=False)} |"
        )
    out.append("\nLas pruebas se solapan: describe, no demuestra.")
    return out


def _trades(r: BacktestResult) -> list[str]:
    closed = [t for t in r.curve_a.trades if t.exit_day is not None]
    if not closed:
        return []
    days_held = [(t.exit_day - t.entry_day).days for t in closed]
    purposes = Counter(t.exit_purpose for t in closed)
    names = {
        "regimen": "mercado bajista",
        "fuera_universo": "fuera del universo",
        "stop": "stop del −25 %",
        "tendencia": "pierde la tendencia",
        "puesto": "baja del puesto 12",
        "retirada": "retirada de Binance",
    }
    years = max(len(r.days), 1) / 365
    return [
        "",
        "## Operaciones (método)",
        "",
        f"- {len(closed)} cerradas ({num(len(closed) / years, 0)} al año); duración mediana "
        f"{statistics.median(days_held):.0f} días; aciertos "
        f"{pct(sum(1 for t in closed if t.pnl > 0) / len(closed), 0, sign=False)}.",
        "- Motivos de salida: "
        + ", ".join(f"{names.get(k, k)} {v}" for k, v in purposes.most_common())
        + ".",
        "- Días con alguna posición: "
        f"{pct(r.curve_a.invested_days / max(len(r.days), 1), 0, sign=False)}.",
        f"- Costes pagados: {usd(r.curve_a.costs_paid)} (sobre un patrimonio que va creciendo).",
    ]


def _survival(r: BacktestResult) -> list[str]:
    s = r.survival
    return [
        "",
        "## Monedas retiradas",
        "",
        f"- Monedas que estuvieron alguna vez en el universo: {s['en_universo_alguna_vez']}; "
        f"de ellas, hoy ya no cotizan {s['ya_no_cotizan']}.",
        f"- Días con alguna posición en una moneda hoy retirada: "
        f"{pct(s['dias_con_retiradas'], 0, sign=False)}.",
    ]


def generated_on(day: date) -> str:
    return f"Generado el {day:%d-%m-%Y}."


COLORS = {"verde": "VERDE", "ambar": "ÁMBAR", "rojo": "ROJO", "gris": "GRIS"}


def semaforo_section(sem) -> str:
    """Luz del backtest con sus criterios (la luz en vivo sale en el informe diario)."""
    lines = [
        f"## Semáforo «¿Es rentable?»: {COLORS.get(sem.semaforo, sem.semaforo)}",
        "",
        f"- Backtest: **{COLORS.get(sem.luces.backtest, sem.luces.backtest)}** · En vivo: "
        f"{COLORS.get(sem.luces.en_vivo, sem.luces.en_vivo)}.",
        "",
        "| Criterio | Qué mide | Valor | Hace falta | ¿Cumple? |",
        "|---|---|---|---|---|",
    ]
    for c in sem.criterios:
        ok = "sí" if c.cumple else "no"
        lines.append(f"| {c.id} | {c.texto} | {c.valor} | {c.umbral} | {ok} |")
    lines.append("")
    lines += [f"- {m}" for m in sem.mensajes]
    lines.append(f"- {sem.aviso}")
    return "\n".join(lines)

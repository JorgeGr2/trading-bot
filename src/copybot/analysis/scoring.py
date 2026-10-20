"""Puntuación de traders: configurable, explicada y versionada.

Primero se aplican **filtros de exclusión** (muestra insuficiente, no copiable, datos
incompletos…). Solo las wallets que los superan reciben una puntuación de 0 a 100:

    puntuación = 100 × ventaja × calidad

* **Ventaja** (0 a 1): rentabilidad media por operación, estimada con prudencia y
  descontando el coste de copiar. Sin ventaja la puntuación es 0, por bien que puntúe
  lo demás: copiar a alguien que pierde por operación solo puede perder.
* **Calidad** (0 a 1): media ponderada de consistencia, facilidad para copiarlo,
  riesgo y calidad de los datos (pesos configurables).

Cada decisión va acompañada de su explicación en texto.

La puntuación es una **hipótesis** sobre qué traders merece la pena simular, no una
predicción de beneficios: se validará con la simulación (Fases 4, 5 y 8). Sus
parámetros se fijan antes de ver resultados y no se ajustan para que salgan bonitos.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict

from copybot.analysis.metrics import ANALYZER_VERSION, AnalysisParams, WindowMetrics
from copybot.config import GroupSettings

# 2: ventaja y consistencia con la serie prudente (incluye las posiciones abandonadas) y
#    robustez «sin las 3 mejores» por operación, no en dólares; el riesgo usa la pérdida
#    en las abandonadas (coste menos valor de venta).
SCORING_VERSION = "2"


class ScoringParams(GroupSettings):
    """Parámetros de la puntuación (variables ``COPYBOT_SCORING_*``)."""

    model_config = SettingsConfigDict(
        env_prefix="COPYBOT_SCORING_", env_file=".env", env_file_encoding="utf-8", extra="forbid"
    )

    window_days: int = Field(default=30, gt=0, le=90)
    recent_window_days: int = Field(default=7, gt=0, le=30)
    # --- Filtros de exclusión ---
    min_round_trips: int = Field(default=20, ge=1)
    # Si la mediana de tiempo hasta la primera venta es menor, no da tiempo a copiar.
    min_median_first_sell_seconds: int = Field(default=60, ge=0)
    max_early_buy_share: Decimal = Field(default=Decimal("0.3"), ge=0, le=1)
    min_launch_checks: int = Field(default=10, ge=1)
    max_unmatched_share: Decimal = Field(default=Decimal("0.25"), ge=0, le=1)
    max_trades_per_day: Decimal = Field(default=Decimal("300"), gt=0)
    # --- Componentes ---
    # Coste estimado de copiar una operación completa (comisiones, slippage y llegar
    # tarde). Es un supuesto: se calibrará con la simulación de la Fase 4.
    copy_cost: Decimal = Field(default=Decimal("0.03"), ge=0, lt=1)
    # Ventaja (tras el coste) a partir de la cual el componente de ventaja vale 1.
    target_edge: Decimal = Field(default=Decimal("0.10"), gt=0, le=10)
    # Prudencia de la estimación: media − z · error típico.
    z: Decimal = Field(default=Decimal("1"), ge=0, le=4)
    good_first_sell_seconds: int = Field(default=600, gt=0)
    comfortable_trades_per_day: Decimal = Field(default=Decimal("50"), gt=0)
    drawdown_for_zero: Decimal = Field(default=Decimal("0.6"), gt=0, le=1)
    # Pesos de la calidad (deben sumar 1). La ventaja no tiene peso: multiplica.
    weight_consistency: Decimal = Decimal("0.25")
    weight_copyability: Decimal = Decimal("0.35")
    weight_risk: Decimal = Decimal("0.25")
    weight_data: Decimal = Decimal("0.15")

    @model_validator(mode="after")
    def _check(self) -> ScoringParams:
        weights = self.weights()
        if any(w < 0 for w in weights.values()) or sum(weights.values()) != 1:
            raise ValueError("los pesos deben ser ≥ 0 y sumar 1")
        if self.recent_window_days > self.window_days:
            raise ValueError("recent_window_days no puede superar window_days")
        if self.good_first_sell_seconds <= self.min_median_first_sell_seconds:
            raise ValueError("good_first_sell_seconds debe superar el mínimo")
        if self.comfortable_trades_per_day >= self.max_trades_per_day:
            raise ValueError("comfortable_trades_per_day debe ser menor que el máximo")
        return self

    def weights(self) -> dict[str, Decimal]:
        """Pesos de los componentes de la calidad."""
        return {
            "consistencia": self.weight_consistency,
            "copiable": self.weight_copyability,
            "riesgo": self.weight_risk,
            "datos": self.weight_data,
        }


def _digest(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def metrics_params_hash(analysis: AnalysisParams) -> str:
    return _digest({"analyzer": ANALYZER_VERSION, "analysis": analysis.model_dump(mode="json")})


def score_params_hash(analysis: AnalysisParams, scoring: ScoringParams) -> str:
    return _digest(
        {
            "analyzer": ANALYZER_VERSION,
            "analysis": analysis.model_dump(mode="json"),
            "scoring_version": SCORING_VERSION,
            "scoring": scoring.model_dump(mode="json"),
        }
    )


@dataclass(frozen=True)
class Flag:
    code: str
    confidence: str  # alta | media | baja
    detail: str

    def to_json(self) -> dict[str, str]:
        return {"codigo": self.code, "confianza": self.confidence, "detalle": self.detail}


@dataclass
class ScoreResult:
    eligible: bool
    score: Decimal | None
    exclusions: list[str] = field(default_factory=list)
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    flags: list[Flag] = field(default_factory=list)
    quality: Decimal | None = None

    def explanation(self) -> list[str]:
        lines: list[str] = []
        if self.eligible:
            lines.append(f"Puntuación {self.score}/100.")
        else:
            lines.append("Excluido de la puntuación:")
            lines += [f"  - {reason}" for reason in self.exclusions]
        for name, comp in self.components.items():
            if comp["peso"] is None:
                head = f"{name.capitalize()} ({comp['valor']:.2f}, multiplica a la calidad)"
            else:
                head = f"{name.capitalize()} ({comp['valor']:.2f} × peso {comp['peso']})"
            lines.append(f"{head}: {comp['detalle']}")
        if self.quality is not None:
            lines.append(
                f"Calidad {self.quality:.2f}: media ponderada de consistencia, copiable, "
                "riesgo y datos."
            )
        for flag in self.flags:
            lines.append(f"Aviso ({flag.confidence}): {flag.detail}")
        return lines

    def components_json(self) -> dict[str, Any]:
        return {
            name: {
                "valor": str(c["valor"]),
                "peso": None if c["peso"] is None else str(c["peso"]),
                "detalle": c["detalle"],
            }
            for name, c in self.components.items()
        }


def _clamp(value: Decimal) -> Decimal:
    return max(Decimal(0), min(Decimal(1), value))


def _pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:+.1f} %"


def _secs(value: Decimal | None) -> str:
    if value is None:
        return "—"
    if value < 120:
        return f"{value:.0f} s"
    if value < 7200:
        return f"{value / 60:.1f} min"
    return f"{value / 3600:.1f} h"


def edge_lower_bound(m: WindowMetrics, z: Decimal) -> Decimal | None:
    """Estimación prudente de la rentabilidad media por operación (antes de costes).

    Usa la serie prudente: operaciones cerradas y posiciones abandonadas (a 0).
    """
    n = m.prudent_round_trips
    if m.prudent_mean_return is None or m.prudent_stdev_return is None or n < 2:
        return None
    return m.prudent_mean_return - z * m.prudent_stdev_return / Decimal(n).sqrt()


def score_wallet(m: WindowMetrics, recent: WindowMetrics, p: ScoringParams) -> ScoreResult:
    """Puntúa con las métricas de la ventana principal (``m``) y la reciente."""
    result = ScoreResult(eligible=True, score=None)
    trades = m.buys + m.sells

    # --- Filtros de exclusión ---------------------------------------------------
    if not m.coverage_complete:
        result.exclusions.append(
            "el historial importado no cubre todo el periodo analizado (o no está al día)"
        )
    if m.round_trips < p.min_round_trips:
        result.exclusions.append(
            f"muestra insuficiente: {m.round_trips} operaciones cerradas con coste conocido "
            f"(mínimo {p.min_round_trips})"
        )
    first_sell = m.median_seconds_to_first_sell
    if first_sell is not None and first_sell < p.min_median_first_sell_seconds:
        result.exclusions.append(
            f"no copiable: vende una mediana de {_secs(first_sell)} después de comprar "
            f"(mínimo {p.min_median_first_sell_seconds} s)"
        )
    if (
        m.early_buy_share is not None
        and m.launch_checked >= p.min_launch_checks
        and m.early_buy_share > p.max_early_buy_share
    ):
        result.exclusions.append(
            f"compra en el lanzamiento (sniping) en el {m.early_buy_share:.0%} de las entradas "
            f"comprobadas ({m.early_buys} de {m.launch_checked})"
        )
    if m.unmatched_sell_share is not None and m.unmatched_sell_share > p.max_unmatched_share:
        result.exclusions.append(
            f"datos incompletos: el {m.unmatched_sell_share:.0%} de lo vendido no tiene compra "
            "conocida"
        )
    if m.trades_per_day > p.max_trades_per_day:
        result.exclusions.append(
            f"frecuencia imposible de seguir: {m.trades_per_day:.0f} compraventas al día"
        )

    # --- Componentes ------------------------------------------------------------
    weights = p.weights()
    lb = edge_lower_bound(m, p.z)
    net = lb - p.copy_cost if lb is not None else None
    result.components["ventaja"] = {
        "valor": _clamp(net / p.target_edge) if net is not None else Decimal(0),
        "peso": None,
        "detalle": (
            f"rentabilidad media por operación cerrada {_pct(m.mean_return)}; con un tope "
            f"por operación (media robusta): {_pct(m.clipped_mean_return)}; incluyendo "
            f"{m.abandoned_round_trips} posiciones abandonadas a su valor de venta actual "
            f"({m.abandoned_priced} con precio, el resto a 0): {_pct(m.prudent_mean_return)}; "
            f"estimación prudente {_pct(lb)}; tras restar el "
            f"coste estimado de copiar ({p.copy_cost:.0%}): {_pct(net)} "
            f"(vale 1 con {_pct(p.target_edge)})"
        ),
    }

    checks = {
        "primera mitad del periodo en positivo": m.first_half_mean_return,
        "segunda mitad en positivo": m.second_half_mean_return,
        f"últimos {recent.window_days} días en positivo": recent.prudent_mean_return,
        "en positivo sin sus 3 mejores operaciones": m.prudent_mean_without_top3,
    }
    passed = [name for name, value in checks.items() if value is not None and value > 0]
    result.components["consistencia"] = {
        "valor": Decimal(len(passed)) / len(checks),
        "peso": weights["consistencia"],
        "detalle": "; ".join(f"{name}: {'sí' if name in passed else 'no'}" for name in checks),
    }

    if first_sell is None:
        hold_value = Decimal(0)
    else:
        low = max(p.min_median_first_sell_seconds, 1)
        ratio = math.log(max(float(first_sell), 1e-9) / low) / math.log(
            p.good_first_sell_seconds / low
        )
        hold_value = _clamp(Decimal(str(round(ratio, 6))))
    freq_value = _clamp(
        1
        - (m.trades_per_day - p.comfortable_trades_per_day)
        / (p.max_trades_per_day - p.comfortable_trades_per_day)
    )
    early_value = Decimal(1) - (m.early_buy_share or Decimal(0))
    result.components["copiable"] = {
        "valor": hold_value * freq_value * early_value,
        "peso": weights["copiable"],
        "detalle": (
            f"mediana hasta la primera venta {_secs(first_sell)} ({hold_value:.2f}; vale 1 "
            f"desde {_secs(Decimal(p.good_first_sell_seconds))}); {m.trades_per_day:.1f} "
            f"compraventas al día ({freq_value:.2f}); entradas en el lanzamiento "
            f"{_share_text(m.early_buys, m.launch_checked)} ({early_value:.2f})"
        ),
    }

    dd_value = (
        _clamp(1 - m.max_drawdown_pct / p.drawdown_for_zero)
        if m.max_drawdown_pct is not None
        else Decimal(0)
    )
    stale_loss = max(m.stale_open_cost_usd - m.abandoned_value_usd, Decimal(0))
    stale_ratio = _clamp(stale_loss / m.buy_volume_usd) if m.buy_volume_usd > 0 else Decimal(0)
    result.components["riesgo"] = {
        "valor": dd_value * (1 - stale_ratio),
        "peso": weights["riesgo"],
        "detalle": (
            f"caída máxima del PnL realizado {m.max_drawdown_usd:.2f} $ "
            f"({_pct_plain(m.max_drawdown_pct)} del capital máximo invertido, "
            f"{m.peak_open_cost_usd:.2f} $); pérdida en posiciones abandonadas "
            f"{stale_loss:.2f} $ ({stale_ratio:.0%} de lo comprado)"
        ),
    }

    bad = m.ambiguous + m.non_usd_trades + m.medium_confidence
    bad_share = Decimal(bad) / (trades + m.ambiguous) if trades + m.ambiguous else Decimal(1)
    unmatched = m.unmatched_sell_share or Decimal(0)
    result.components["datos"] = {
        "valor": _clamp(1 - unmatched - bad_share),
        "peso": weights["datos"],
        "detalle": (
            f"{_pct_plain(m.unmatched_sell_share)} de lo vendido sin compra conocida; "
            f"{bad} eventos ambiguos, de confianza media o no pagados en USDC/USDT"
        ),
    }

    result.flags = _flags(m, p)
    if result.exclusions:
        result.eligible = False
        return result
    result.quality = sum(
        (c["valor"] * c["peso"] for c in result.components.values() if c["peso"] is not None),
        Decimal(0),
    )
    edge = result.components["ventaja"]["valor"]
    result.score = (100 * edge * result.quality).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return result


def _pct_plain(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.1f} %"


def _share_text(part: int, total: int) -> str:
    return f"{part} de {total} comprobadas" if total else "sin comprobar"


def _flags(m: WindowMetrics, p: ScoringParams) -> list[Flag]:
    flags: list[Flag] = []
    trades = m.buys + m.sells
    if m.launch_checked < p.min_launch_checks:
        flags.append(
            Flag(
                "sniping_sin_comprobar",
                "baja",
                f"solo se conoce la fecha de creación de {m.launch_checked} de los tokens en "
                f"los que entró ({m.launch_unchecked} sin comprobar): no se puede descartar "
                "que compre en el lanzamiento",
            )
        )
    elif m.early_buy_share is not None and m.early_buy_share > Decimal("0.1"):
        flags.append(
            Flag(
                "compras_en_lanzamiento",
                "alta" if m.launch_checked >= 30 else "media",
                f"entra en los primeros segundos de vida del token en el {m.early_buy_share:.0%} "
                f"de los casos comprobados ({m.early_buys} de {m.launch_checked})",
            )
        )
    if m.quick_exit_share is not None and m.quick_exit_share > Decimal("0.5"):
        flags.append(
            Flag(
                "salidas_rapidas",
                "alta",
                f"en el {m.quick_exit_share:.0%} de sus operaciones empieza a vender antes de "
                "un minuto: copiarlas con retraso es muy difícil",
            )
        )
    if m.trades_per_day > p.comfortable_trades_per_day:
        flags.append(
            Flag(
                "alta_frecuencia",
                "alta",
                f"{m.trades_per_day:.0f} compraventas al día: con 5 posiciones como máximo solo "
                "se podría copiar una parte y los costes se acumulan",
            )
        )
    if m.sell_volume_usd > 0 and m.free_tokens_sold_usd > Decimal("0.05") * m.sell_volume_usd:
        flags.append(
            Flag(
                "vende_tokens_recibidos",
                "media",
                f"vende tokens que recibió por transferencia, no comprados "
                f"({m.free_tokens_sold_usd:.2f} $). Pueden venir de otra wallet suya, de "
                "repartos automáticos o del creador del token: no se puede saber con certeza",
            )
        )
    if m.same_slot_exits:
        flags.append(
            Flag(
                "misma_ranura",
                "media",
                f"compra y vende el mismo token en el mismo bloque {m.same_slot_exits} veces "
                "(típico de bots)",
            )
        )
    if m.top1_profit_share is not None and m.top1_profit_share > Decimal("0.5"):
        flags.append(
            Flag(
                "beneficio_concentrado",
                "alta",
                f"una sola operación aporta el {m.top1_profit_share:.0%} de las ganancias; sin "
                f"ella el PnL de sus operaciones sería {m.pnl_without_best_usd:.2f} $",
            )
        )
    if m.buy_volume_usd > 0 and m.stale_open_cost_usd > Decimal("0.2") * m.buy_volume_usd:
        flags.append(
            Flag(
                "posiciones_abandonadas",
                "media",
                f"mantiene sin vender {m.stale_open_cost_usd:.2f} $ comprados hace más de 72 h, "
                f"que hoy valdrían unos {m.abandoned_value_usd:.2f} $ al venderlos: su PnL "
                f"realizado ({m.realized_pnl_usd:.2f} $) es engañoso; contándolas sería "
                f"{m.conservative_pnl_usd:.2f} $",
            )
        )
    if m.unmatched_sell_share is not None and m.unmatched_sell_share > Decimal("0.1"):
        flags.append(
            Flag(
                "ventas_sin_compra",
                "alta",
                f"el {m.unmatched_sell_share:.0%} de lo vendido no tiene compra conocida en el "
                "periodo importado",
            )
        )
    if p.min_round_trips <= m.round_trips < 30:
        flags.append(
            Flag(
                "muestra_pequeña",
                "alta",
                f"solo {m.round_trips} operaciones cerradas: las métricas son poco fiables",
            )
        )
    if trades and m.non_usd_trades:
        flags.append(
            Flag(
                "operaciones_no_usd",
                "alta",
                f"{m.non_usd_trades} compraventas pagadas en SOL u otra moneda: no se valoran en "
                "USD y quedan fuera del PnL",
            )
        )
    return flags

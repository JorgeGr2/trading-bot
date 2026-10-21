"""Datos del backtest del copy-trading: operaciones de los traders, precios retrasados
(con su caché), comisiones de transferencia y selección de traders a una fecha."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic_settings import BaseSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.analysis.metrics import AnalysisParams
from copybot.analysis.scoring import ScoringParams
from copybot.analysis.service import TraderAnalyzer
from copybot.backtest.copy import Quote, TraderEvent
from copybot.backtest.delayed import USD_QUOTES, DelayedPrices
from copybot.db.models import CandidateWallet, HistoryImport, TokenSafetyCheck
from copybot.db.models import WalletEvent as WalletEventRow
from copybot.db.repo import Repository

# Universos de traders entre los que se elige. La leaderboard de FOMO se capturó el
# 19-oct con el ranking de los 30 días anteriores: elegir entre ella para copiar antes de
# esa fecha es elegir sabiendo quién ganó (sesgo de supervivencia). Solo sirve como cota
# optimista. Los demás se descubrieron por actividad, no por lo que ganaban.
UNIVERSES = ("actividad", "leaderboard", "todos")


def with_defaults[S: BaseSettings](cls: type[S], **overrides: Any) -> S:
    """Parámetros con los valores del código, sin que el entorno ni ``.env`` cambien
    nada: el backtest debe dar lo mismo en cualquier máquina."""
    values = {
        name: f.get_default(call_default_factory=True)
        for name, f in cls.model_fields.items()
        if not f.is_required()
    }
    return cls(**{**values, **overrides})


async def universe(sessions: async_sessionmaker[AsyncSession], name: str) -> list[str]:
    """Wallets con historial importado del universo ``name`` (ver ``UNIVERSES``)."""
    if name not in UNIVERSES:
        raise ValueError(f"universo desconocido: {name}")
    stmt = (
        select(HistoryImport.wallet, CandidateWallet.source)
        .outerjoin(CandidateWallet, CandidateWallet.wallet == HistoryImport.wallet)
        .order_by(HistoryImport.wallet)
    )
    async with sessions() as s:
        rows = (await s.execute(stmt)).all()
    if name == "todos":
        return [w for w, _ in rows]
    leaderboard = name == "leaderboard"
    return [w for w, source in rows if (source == "leaderboard") == leaderboard]


async def load_events(
    sessions: async_sessionmaker[AsyncSession], wallets: Iterable[str], until: datetime
) -> list[TraderEvent]:
    """Compraventas y transferencias de las wallets hasta ``until``, en el orden de la
    cadena (bloque e índice). Las anteriores al periodo copiado sirven para conocer el
    saldo del trader. Solo se copian las compraventas de confianza alta; las demás se
    cargan como transferencias para que la fracción que vende el trader sea correcta."""
    e = WalletEventRow
    stmt = (
        select(e)
        .where(
            e.wallet.in_(list(wallets)),
            e.block_time.is_not(None),
            e.block_time < until,
            e.mint.is_not(None),
            e.kind.in_(["buy", "sell", "transfer_in", "transfer_out"]),
        )
        .order_by(e.block_time, e.slot, e.tx_index, e.id)
    )
    async with sessions() as s:
        rows = list(await s.scalars(stmt))
    out = []
    for order, r in enumerate(rows):
        kind = r.kind
        if kind in ("buy", "sell") and r.confidence != "high":
            kind = "transfer_in" if kind == "buy" else "transfer_out"
        quote = abs(Decimal(r.quote_delta))
        out.append(
            TraderEvent(
                wallet=r.wallet,
                mint=r.mint,
                kind=kind,
                amount=abs(Decimal(r.token_delta)),
                usd=quote if r.quote_symbol in USD_QUOTES else None,
                sol=quote if r.quote_symbol == "SOL" else None,
                at=int(r.block_time.timestamp()),
                order=order,
            )
        )
    return out


class DelayedQuotes:
    """Adapta ``DelayedPrices`` al backtest, con un tope de consultas a Helius."""

    def __init__(self, prices: DelayedPrices, *, fetch: bool, max_lookups: int) -> None:
        self._prices = prices
        self._fetch = fetch
        self._max = max_lookups

    @property
    def lookups(self) -> int:
        return self._prices.lookups

    @property
    def missing(self) -> int:
        return self._prices.missing

    async def quote(self, mint: str, at: int, window: tuple[int, int]) -> Quote | None:
        fetch = self._fetch and self._prices.lookups < self._max
        row = await self._prices.get(mint, at, window, fetch=fetch)
        if row is None:
            return None
        return Quote(row.price_usd, row.sol_usd)


async def transfer_fees(
    sessions: async_sessionmaker[AsyncSession], mints: Iterable[str], start: datetime
) -> dict[str, int]:
    """Comisión de transferencia de cada token según los filtros de seguridad: la última
    comprobación anterior a ``start`` o, si no la hay, la primera posterior. Los tokens
    nunca comprobados no aparecen (se supone 0)."""
    mints = sorted(set(mints))
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    async with sessions() as s:
        rows = await s.scalars(
            select(TokenSafetyCheck)
            .where(TokenSafetyCheck.mint.in_(mints))
            .order_by(TokenSafetyCheck.checked_at, TokenSafetyCheck.id)
        )
        for row in rows:
            bps = (row.details or {}).get("comision_transferencia_bps")
            if bps is None:
                continue
            if row.checked_at <= start:
                before[row.mint] = int(bps)
            else:
                after.setdefault(row.mint, int(bps))
    return {**after, **before}


@dataclass(frozen=True)
class Selection:
    wallet: str
    score: Decimal | None
    eligible: bool
    round_trips: int
    prudent_mean_return: Decimal | None


async def select_traders(
    repo: Repository, wallets: Iterable[str], as_of: datetime, window_days: int
) -> list[Selection]:
    """Analiza cada wallet **a fecha** ``as_of`` (solo con datos anteriores) y devuelve
    su puntuación. No guarda nada: es la selección que se habría hecho ese día."""
    analyzer = TraderAnalyzer(
        repo,
        analysis=with_defaults(AnalysisParams),
        scoring=with_defaults(
            ScoringParams, window_days=window_days, recent_window_days=min(7, window_days)
        ),
    )
    out = []
    for wallet in wallets:
        result = await analyzer.analyze(wallet, as_of, save=False)
        m = result.main
        out.append(
            Selection(
                wallet=wallet,
                score=result.score.score,
                eligible=result.score.eligible,
                round_trips=m.round_trips,
                prudent_mean_return=m.prudent_mean_return,
            )
        )
    return sorted(out, key=lambda s: (s.score is None, -(s.score or 0), s.wallet))

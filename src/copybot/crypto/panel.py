"""Velas diarias en memoria para el análisis y el backtest.

Todo el cálculo de señales trabaja con números de coma flotante (son estadísticas, no
dinero); el dinero de la cartera simulada se lleva en ``Decimal``. Un día ``d`` es la vela
que abre a las 00:00 UTC de ``d`` y cierra a las 00:00 del día siguiente: su cierre se
conoce a partir de ese momento.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copybot.crypto.sync import EXCHANGE
from copybot.db.models import MarketBar, MarketSymbol

EPISODE_GAP_DAYS = 7


@dataclass
class Series:
    symbol: str
    base: str
    status: str  # estado actual en Binance (TRADING, BREAK…)
    days: list[date] = field(default_factory=list)
    open: list[float] = field(default_factory=list)
    high: list[float] = field(default_factory=list)
    low: list[float] = field(default_factory=list)
    close: list[float] = field(default_factory=list)
    quote_volume: list[float] = field(default_factory=list)
    trades: list[int] = field(default_factory=list)
    _episode: list[int] = field(default_factory=list, repr=False)

    def index(self, day: date) -> int | None:
        """Posición de la última vela con fecha ≤ ``day``; ``None`` si aún no cotizaba."""
        i = bisect_right(self.days, day) - 1
        return None if i < 0 else i

    def at(self, day: date) -> int | None:
        """Posición de la vela de exactamente ``day``."""
        i = self.index(day)
        return i if i is not None and self.days[i] == day else None

    def traded(self, i: int) -> bool:
        """Hubo negociación ese día (una vela con 0 operaciones cuenta como hueco)."""
        return self.trades[i] > 0

    def episode_start(self, i: int) -> int:
        """Primera vela del episodio de ``i``: un hueco de más de ``EPISODE_GAP_DAYS`` días
        sin negociación abre un episodio nuevo (p. ej. LUNA clásica y LUNA 2.0)."""
        if len(self._episode) != len(self.days):
            self._episode = _episodes(self.days, self.trades)
        return self._episode[i]

    @property
    def first_day(self) -> date:
        return self.days[0]

    @property
    def last_day(self) -> date:
        return self.days[-1]


def _episodes(days: list[date], trades: list[int]) -> list[int]:
    out: list[int] = []
    start = 0
    last_traded: date | None = None
    for i, (day, n) in enumerate(zip(days, trades, strict=True)):
        if n > 0:
            if last_traded is not None and (day - last_traded).days > EPISODE_GAP_DAYS + 1:
                start = i
            last_traded = day
        out.append(start)
    return out


@dataclass
class Panel:
    series: dict[str, Series]
    reference: str = "BTCUSDT"
    _gaps: frozenset[date] | None = field(default=None, repr=False)

    @property
    def days(self) -> list[date]:
        return sorted({d for s in self.series.values() for d in s.days})

    def global_gaps(self) -> frozenset[date]:
        """Días sin vela de BTC: fallo de datos general, no hueco de cada par."""
        if self._gaps is None:
            self._gaps = self._compute_gaps()
        return self._gaps

    def _compute_gaps(self) -> frozenset[date]:
        ref = self.series.get(self.reference)
        if ref is None or not ref.days:
            return frozenset()
        have = set(ref.days)
        first, last = ref.days[0], ref.days[-1]
        return frozenset(
            first + timedelta(days=k)
            for k in range((last - first).days + 1)
            if first + timedelta(days=k) not in have
        )

    def trading_on(self, day: date) -> list[Series]:
        """Pares con vela ese día (cotizaban)."""
        return [s for s in self.series.values() if s.at(day) is not None]


async def load_panel(
    sessions: async_sessionmaker[AsyncSession],
    *,
    quote: str = "USDT",
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
) -> Panel:
    """Velas diarias de los pares contra ``quote`` (todas, también de pares retirados)."""
    m, b = MarketSymbol, MarketBar
    stmt = (
        select(b.symbol, b.open_time, b.open, b.high, b.low, b.close, b.quote_volume, b.trades)
        .join(m, (m.exchange == b.exchange) & (m.symbol == b.symbol))
        .where(b.exchange == EXCHANGE, b.interval == "1d", m.quote == quote)
        .order_by(b.symbol, b.open_time)
    )
    if start is not None:
        stmt = stmt.where(b.open_time >= datetime(start.year, start.month, start.day, tzinfo=UTC))
    if end is not None:
        stmt = stmt.where(b.open_time <= datetime(end.year, end.month, end.day, 23, 59, tzinfo=UTC))
    if symbols is not None:
        stmt = stmt.where(b.symbol.in_(symbols))
    async with sessions() as s:
        meta = {
            row.symbol: row
            for row in await s.scalars(select(m).where(m.exchange == EXCHANGE, m.quote == quote))
        }
        rows = (await s.execute(stmt)).all()
    out: dict[str, Series] = {}
    by_symbol: dict[str, list] = defaultdict(list)
    for r in rows:
        by_symbol[r.symbol].append(r)
    for symbol, bars in by_symbol.items():
        info = meta[symbol]
        series = Series(symbol, info.base, info.status)
        for r in bars:
            series.days.append(r.open_time.date())
            series.open.append(float(r.open))
            series.high.append(float(r.high))
            series.low.append(float(r.low))
            series.close.append(float(r.close))
            series.quote_volume.append(float(r.quote_volume))
            series.trades.append(int(r.trades))
        out[symbol] = series
    return Panel(out)

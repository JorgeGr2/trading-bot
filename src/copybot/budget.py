"""Control de gasto en créditos del proveedor de datos.

El análisis (descubrimiento, historiales, fechas de creación de tokens) solo puede usar
una parte del presupuesto mensual de créditos. Al llegar al límite se detiene **antes**
de enviar la petición, y el seguimiento en vivo de las wallets (que no usa este
control) sigue funcionando. Nunca se sube de plan automáticamente.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol


class BudgetExceededError(RuntimeError):
    """La petición superaría el presupuesto de créditos asignado."""


class UsageSource(Protocol):
    async def monthly_credits(self, provider: str, now: datetime) -> int: ...


class CreditBudget:
    def __init__(
        self,
        usage: UsageSource,
        *,
        provider: str,
        monthly_limit: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if monthly_limit <= 0:
            raise ValueError("monthly_limit debe ser positivo")
        self._usage = usage
        self._provider = provider
        self.monthly_limit = monthly_limit
        self._clock = clock

    async def remaining(self) -> int:
        used = await self._usage.monthly_credits(self._provider, self._clock())
        return self.monthly_limit - used

    async def check(self, method: str, credits: int) -> None:
        """Se llama antes de cada petición (``SolanaRpc(budget_check=...)``)."""
        left = await self.remaining()
        if credits > left:
            raise BudgetExceededError(
                f"{method} necesita {credits} créditos y quedan {max(left, 0)} de "
                f"{self.monthly_limit} este mes para el análisis"
            )

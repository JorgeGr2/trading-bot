"""Costes de una operación simulada en Solana."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

LAMPORTS_PER_SOL = Decimal(1_000_000_000)
# Renta de una cuenta de token (medida on-chain en la Fase 2/3): se recupera al cerrarla.
RENT_LAMPORTS = {"spl-token": 1_488_440, "spl-token-2022": 1_513_840}


@dataclass(frozen=True)
class CostModel:
    sol_usd: Decimal
    base_fee_lamports: int
    priority_fee_lamports: int
    mev_haircut_bps: int

    def lamports_usd(self, lamports: int) -> Decimal:
        return Decimal(lamports) / LAMPORTS_PER_SOL * self.sol_usd

    def tx_fee_usd(self) -> Decimal:
        """Comisión de red más la prioritaria de una transacción (se paga aunque falle)."""
        return self.lamports_usd(self.base_fee_lamports + self.priority_fee_lamports)

    def after_mev(self, amount: Decimal) -> Decimal:
        return amount * (1 - Decimal(self.mev_haircut_bps) / 10_000)

    @staticmethod
    def after_transfer_fee(amount: Decimal, fee_bps: int) -> Decimal:
        """Lo que llega tras la comisión de transferencia de Token-2022 (sin tope: prudente)."""
        return amount * (1 - Decimal(fee_bps) / 10_000)

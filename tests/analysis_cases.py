"""Escenario sintético para los tests del analizador, con resultados calculados a mano.

Wallet ficticia; ``as_of`` = 10-oct-2026 00:00 UTC; ventana de 7 días (desde el 3-oct).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from copybot.analysis.positions import Trade

AS_OF = datetime(2026, 10, 10, tzinfo=UTC)


def at(day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def trade(sig, slot, time, kind, mint, amount, usd=None, confidence="high") -> Trade:
    return Trade(
        signature=sig,
        slot=slot,
        tx_index=0,
        time=time,
        kind=kind,
        mint=mint,
        amount=Decimal(amount),
        usd=Decimal(usd) if usd is not None else None,
        confidence=confidence,
    )


SCENARIO = [
    # F: compra antes de la ventana, sigue abierta.
    trade("T11", 50, at(1), "buy", "F", "100", "5"),
    # G: compra dentro de la ventana hace más de 72 h y sigue abierta: «abandonada».
    trade("T12", 80, at(3, 12), "buy", "G", "10", "4"),
    # A: compra 1000 por 10 $; vende 400 por 6 $ (coste 4, +2) y 600 por 3 $ (coste 6, −3).
    trade("T1", 100, at(4, 10), "buy", "A", "1000", "10"),
    trade("T2", 200, at(4, 10, 5), "sell", "A", "400", "6"),
    trade("T3", 300, at(4, 10, 10), "sell", "A", "600", "3"),
    # B: dos compras (50 por 20 $ y 50 por 30 $) y venta de 99,5 por 99,5 $. Quedan 0,5
    # (≤ 1 % de 100): se cierra y su coste (0,3 $) se da por perdido.
    trade("T4", 400, at(5, 12), "buy", "B", "50", "20"),
    trade("T5", 410, at(5, 12, 0, 30), "buy", "B", "50", "30"),
    trade("T6", 420, at(5, 12, 1), "sell", "B", "99.5", "99.5"),
    # C: recibe 1000 por transferencia y los vende por 5 $: tokens «gratis».
    trade("T7", 500, at(6), "transfer_in", "C", "1000"),
    trade("T8", 510, at(6, 1), "sell", "C", "1000", "5"),
    # D: vende 100 por 8 $ sin haber comprado nada en el periodo: venta sin coste conocido.
    trade("T9", 600, at(7), "sell", "D", "100", "8"),
    # E: compra hace menos de 72 h, abierta.
    trade("T10", 700, at(8), "buy", "E", "10", "10"),
    # Posterior a as_of: nunca debe influir.
    trade("T13", 900, at(11), "sell", "E", "10", "100"),
]

# Fecha de creación de los tokens: A se compró 30 s después de crearse (sniping).
CREATED = {"A": at(4, 9, 59, 30), "B": at(1)}

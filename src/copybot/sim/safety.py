"""Filtros de seguridad de un token antes de comprarlo.

Dos partes:

* **Del token** (cuenta on-chain del mint, ``getAccountInfo``): nadie debe poder emitir
  más tokens ni congelar saldos, y se rechazan las extensiones de Token-2022 que permiten
  bloquear o quitar los tokens (hook de transferencia, delegado permanente, tokens
  intransferibles, cuentas congeladas por defecto, pausa). La comisión de transferencia
  se admite hasta un máximo y, por defecto, solo si nadie puede subirla.
* **Del mercado** (Jupiter): liquidez mínima, impacto de precio de nuestra compra y
  pérdida al comprar y vender a la vez (detecta tokens que no se pueden vender).

Las funciones de evaluación son puras; ``fetch_mint_info`` es la única que usa la red.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from copybot.sim.params import SafetyParams

# Extensiones de Token-2022 que permiten bloquear, quitar o retener los tokens.
DANGEROUS_EXTENSIONS = {
    "transferHook": "tiene un programa que puede bloquear las transferencias (transfer hook)",
    "permanentDelegate": "un delegado permanente puede mover o quemar los tokens de cualquiera",
    "nonTransferable": "los tokens no se pueden transferir",
    "pausableConfig": "el creador puede pausar las transferencias",
}


@dataclass(frozen=True)
class MintInfo:
    mint: str
    program: str  # spl-token | spl-token-2022
    decimals: int
    mint_authority: str | None
    freeze_authority: str | None
    extensions: dict[str, Any] = field(default_factory=dict)

    @property
    def transfer_fee_bps(self) -> int:
        config = self.extensions.get("transferFeeConfig") or {}
        fees = [config.get(k) or {} for k in ("olderTransferFee", "newerTransferFee")]
        return max((int(f.get("transferFeeBasisPoints", 0)) for f in fees), default=0)

    @property
    def transfer_fee_mutable(self) -> bool:
        config = self.extensions.get("transferFeeConfig") or {}
        return config.get("transferFeeConfigAuthority") is not None


def parse_mint_account(mint: str, value: dict[str, Any] | None) -> MintInfo | None:
    """Convierte la respuesta de ``getAccountInfo`` (jsonParsed) de un mint."""
    if not value:
        return None
    data = value.get("data")
    if not isinstance(data, dict) or data.get("parsed", {}).get("type") != "mint":
        return None
    info = data["parsed"]["info"]
    extensions = {
        e["extension"]: e.get("state") for e in info.get("extensions") or [] if "extension" in e
    }
    return MintInfo(
        mint=mint,
        program=data.get("program", ""),
        decimals=int(info["decimals"]),
        mint_authority=info.get("mintAuthority"),
        freeze_authority=info.get("freezeAuthority"),
        extensions=extensions,
    )


class AccountRpc(Protocol):
    async def get_account_info(self, address: str) -> dict[str, Any] | None: ...


async def fetch_mint_info(rpc: AccountRpc, mint: str) -> MintInfo | None:
    return parse_mint_account(mint, await rpc.get_account_info(mint))


def mint_rejections(info: MintInfo | None, params: SafetyParams) -> list[str]:
    """Motivos de rechazo que dependen solo del token (vacío: pasa)."""
    if info is None:
        return ["no se pudo leer la cuenta del token"]
    reasons = []
    if info.mint_authority is not None:
        reasons.append("el creador puede emitir más tokens (mint authority)")
    if info.freeze_authority is not None:
        reasons.append("el creador puede congelar los saldos (freeze authority)")
    for name, why in DANGEROUS_EXTENSIONS.items():
        state = info.extensions.get(name, False)
        if name in info.extensions and _extension_active(name, state):
            reasons.append(why)
    default_state = (info.extensions.get("defaultAccountState") or {}).get("accountState")
    if default_state == "frozen":
        reasons.append("las cuentas nuevas nacen congeladas")
    fee = info.transfer_fee_bps
    if fee > params.max_transfer_fee_bps:
        limit = params.max_transfer_fee_bps / 100
        reasons.append(f"comisión de transferencia del {fee / 100:.2f} % (máximo {limit:.2f} %)")
    elif info.transfer_fee_mutable and params.reject_mutable_transfer_fee:
        # También con comisión 0: el creador podría subirla después.
        reasons.append("el creador puede subir la comisión de transferencia")
    return reasons


def _extension_active(name: str, state: Any) -> bool:
    if name == "transferHook":
        # Sin programa pero con autoridad: el creador puede activarlo más tarde.
        return bool((state or {}).get("programId") or (state or {}).get("authority"))
    if name == "permanentDelegate":
        return bool((state or {}).get("delegate"))
    return True


@dataclass(frozen=True)
class MarketCheck:
    liquidity_usd: Decimal | None
    buy_price_impact_pct: Decimal | None  # de nuestra compra
    roundtrip_loss_pct: Decimal | None  # comprar y vender a la vez


def market_rejections(check: MarketCheck, params: SafetyParams) -> list[str]:
    reasons = []
    if check.liquidity_usd is None:
        reasons.append("liquidez desconocida")
    elif check.liquidity_usd < params.min_liquidity_usd:
        reasons.append(
            f"liquidez de {check.liquidity_usd:,.0f} $ (mínimo {params.min_liquidity_usd:,.0f} $)"
        )
    if check.buy_price_impact_pct is None:
        reasons.append("Jupiter no encuentra ruta de compra")
    elif check.buy_price_impact_pct > params.max_price_impact_pct:
        reasons.append(
            f"nuestra compra movería el precio un {check.buy_price_impact_pct:.2f} % "
            f"(máximo {params.max_price_impact_pct} %)"
        )
    if check.roundtrip_loss_pct is None:
        reasons.append("Jupiter no encuentra ruta para volver a vender")
    elif check.roundtrip_loss_pct > params.max_roundtrip_loss_pct:
        reasons.append(
            f"comprar y vender a la vez perdería un {check.roundtrip_loss_pct:.1f} % "
            f"(máximo {params.max_roundtrip_loss_pct} %)"
        )
    return reasons

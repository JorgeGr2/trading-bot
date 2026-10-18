"""Clasificación de transacciones de Solana en eventos de una wallet.

La autoría de una operación se deduce de los **cambios netos de balance de las
cuentas cuyo propietario (owner) es la wallet**, nunca de quién paga la comisión.
En FOMO, por ejemplo, una wallet de la propia FOMO firma y paga todas las
transacciones, y el trader aparece como segundo firmante.

El clasificador es una función pura: recibe el JSON de ``getTransaction``
(codificación ``jsonParsed``) y devuelve eventos. No hace llamadas de red.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from copybot.solana.constants import (
    FOMO_MARKER_ACCOUNT,
    KNOWN_SWAP_PROGRAMS,
    LAMPORTS_PER_SOL,
    QUOTE_SYMBOLS,
    WSOL_MINT,
)

# Variaciones de SOL por debajo de este valor se consideran ruido (no pago).
SOL_DUST_LAMPORTS = 10_000
# Variaciones de un token de como mucho estas unidades mínimas se consideran polvo
# (p. ej. 0,00000001 de un token intermedio que la ruta deja en la wallet).
TOKEN_DUST_RAW_UNITS = 100
# Un token que entra y sale de la wallet en la misma transacción es un paso
# intermedio de la ruta del swap. Si el resto que queda es como mucho esta fracción
# de lo que pasó por la wallet, se trata como resto de ruta y no como otra operación
# (medido en la Fase 3: restos del 0,7-0,9 % en rutas USDC → token intermedio → memecoin).
ROUTE_RESIDUE_MAX_FRACTION = Decimal("0.02")
# Si el pago es en USDC o USDT, un movimiento de SOL de como mucho esto (unas dos rentas
# de cuenta de token) es coste de cuentas o comisión, no parte del precio. Caso real
# (Fase 3): FOMO cobra 2.039.280 lamports por la cuenta nueva y devuelve 1.488.440 al
# cerrar una cuenta temporal de wSOL; el usuario queda en −550.840 lamports.
ACCOUNT_COSTS_MAX_LAMPORTS = 3_000_000
_TRANSFER_TYPES = ("transfer", "transferChecked", "transferCheckedWithFee")


class EventKind(StrEnum):
    BUY = "buy"
    SELL = "sell"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    AMBIGUOUS = "ambiguous"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MalformedTransactionError(ValueError):
    """El JSON no tiene la forma esperada de ``getTransaction`` (jsonParsed)."""


@dataclass(frozen=True)
class TxSummary:
    signature: str
    slot: int
    block_time: int | None
    fee_payer: str
    signers: tuple[str, ...]
    fee_lamports: int
    succeeded: bool
    programs: frozenset[str]
    via_fomo: bool


@dataclass(frozen=True)
class WalletEvent:
    signature: str
    slot: int
    block_time: int | None
    wallet: str
    kind: EventKind
    confidence: Confidence
    mint: str | None
    # Cantidades con signo desde el punto de vista de la wallet (+ entra, − sale).
    token_delta: Decimal
    quote_symbol: str | None
    quote_delta: Decimal
    wallet_is_signer: bool
    fee_payer: str
    via_fomo: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    counterparties: tuple[str, ...] = field(default_factory=tuple)

    @property
    def price_in_quote(self) -> Decimal | None:
        """Precio efectivo por token, comisiones de plataforma incluidas."""
        if self.kind not in (EventKind.BUY, EventKind.SELL) or self.token_delta == 0:
            return None
        return abs(self.quote_delta) / abs(self.token_delta)

    @property
    def usable_as_signal(self) -> bool:
        return self.kind in (EventKind.BUY, EventKind.SELL) and self.confidence is Confidence.HIGH


@dataclass
class _Account:
    pubkey: str
    signer: bool
    writable: bool


def _account_keys(tx: dict[str, Any]) -> list[_Account]:
    message = tx["transaction"]["message"]
    raw_keys = message["accountKeys"]
    if raw_keys and isinstance(raw_keys[0], dict):
        return [_Account(k["pubkey"], bool(k["signer"]), bool(k["writable"])) for k in raw_keys]
    # Codificación "json": claves como texto y direcciones de tablas de búsqueda aparte.
    header = message["header"]
    n_signers = header["numRequiredSignatures"]
    accounts = [_Account(k, i < n_signers, False) for i, k in enumerate(raw_keys)]
    loaded = tx["meta"].get("loadedAddresses") or {}
    accounts += [_Account(k, False, True) for k in loaded.get("writable", [])]
    accounts += [_Account(k, False, False) for k in loaded.get("readonly", [])]
    return accounts


def _programs(tx: dict[str, Any], keys: list[_Account]) -> frozenset[str]:
    found: set[str] = set()

    def add(ix: dict[str, Any]) -> None:
        if "programId" in ix:
            found.add(ix["programId"])
        elif "programIdIndex" in ix:
            found.add(keys[ix["programIdIndex"]].pubkey)

    for ix in tx["transaction"]["message"]["instructions"]:
        add(ix)
    for inner in tx["meta"].get("innerInstructions") or []:
        for ix in inner["instructions"]:
            add(ix)
    return frozenset(found)


def summarize(tx: dict[str, Any], relayers: frozenset[str] = frozenset()) -> TxSummary:
    try:
        keys = _account_keys(tx)
        meta = tx["meta"]
        signature = tx["transaction"]["signatures"][0]
        slot = int(tx["slot"])
        programs = _programs(tx, keys)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise MalformedTransactionError(f"{type(exc).__name__}: {exc}") from exc
    pubkeys = {a.pubkey for a in keys}
    return TxSummary(
        signature=signature,
        slot=slot,
        block_time=tx.get("blockTime"),
        fee_payer=keys[0].pubkey,
        signers=tuple(a.pubkey for a in keys if a.signer),
        fee_lamports=int(meta.get("fee", 0)),
        succeeded=meta.get("err") is None,
        programs=programs,
        via_fomo=keys[0].pubkey in relayers or FOMO_MARKER_ACCOUNT in pubkeys,
    )


@dataclass
class _TokenAccountState:
    owner: str | None
    mint: str
    decimals: int
    pre: int = 0
    post: int = 0
    in_pre: bool = False
    in_post: bool = False


def _token_accounts(meta: dict[str, Any]) -> dict[int, _TokenAccountState]:
    states: dict[int, _TokenAccountState] = {}
    for phase in ("pre", "post"):
        for bal in meta.get(f"{phase}TokenBalances") or []:
            idx = bal["accountIndex"]
            amount = int(bal["uiTokenAmount"]["amount"])
            decimals = int(bal["uiTokenAmount"]["decimals"])
            state = states.get(idx)
            if state is None:
                state = _TokenAccountState(bal.get("owner"), bal["mint"], decimals)
                states[idx] = state
            elif state.owner is None:
                state.owner = bal.get("owner")
            if phase == "pre":
                state.pre, state.in_pre = amount, True
            else:
                state.post, state.in_post = amount, True
    return states


def _parsed_instructions(tx: dict[str, Any]) -> list[dict[str, Any]]:
    out = [ix for ix in tx["transaction"]["message"]["instructions"] if "parsed" in ix]
    for inner in tx["meta"].get("innerInstructions") or []:
        out += [ix for ix in inner["instructions"] if "parsed" in ix]
    return [ix for ix in out if isinstance(ix["parsed"], dict)]


def _rent_flows(tx: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Quién financia la creación de cada cuenta y quién recibe los lamports al cerrarla."""
    funder_of: dict[str, str] = {}
    close_destination_of: dict[str, str] = {}
    for ix in _parsed_instructions(tx):
        kind = ix["parsed"].get("type")
        info = ix["parsed"].get("info") or {}
        if kind == "createAccount" and "newAccount" in info:
            funder_of.setdefault(info["newAccount"], info.get("source", ""))
        elif kind in ("create", "createIdempotent") and "account" in info and "source" in info:
            funder_of.setdefault(info["account"], info["source"])
        elif kind == "closeAccount" and "account" in info:
            close_destination_of[info["account"]] = info.get("destination", "")
    return funder_of, close_destination_of


def _gross_token_flows(
    tx: dict[str, Any], keys: list[_Account], token_accounts: dict[int, _TokenAccountState]
) -> dict[tuple[str, str], list[int]]:
    """Entradas y salidas brutas por (owner, mint) según las instrucciones de transferencia.

    A diferencia de los balances netos, permite ver que un token ha pasado por la
    wallet (entra y sale) aunque al final apenas cambie su saldo.
    """
    account_of: dict[str, _TokenAccountState] = {
        keys[idx].pubkey: st for idx, st in token_accounts.items()
    }
    flows: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for ix in _parsed_instructions(tx):
        if ix.get("program") not in ("spl-token", "spl-token-2022"):
            continue
        if ix["parsed"].get("type") not in _TRANSFER_TYPES:
            continue
        info = ix["parsed"].get("info") or {}
        src = account_of.get(info.get("source", ""))
        dst = account_of.get(info.get("destination", ""))
        amount_info = info.get("tokenAmount") or {}
        raw = amount_info.get("amount", info.get("amount"))
        if raw is None or (src is None and dst is None):
            continue
        amount = int(raw)
        if src is not None and src.owner is not None:
            flows[(src.owner, src.mint)][1] += amount
        if dst is not None and dst.owner is not None:
            flows[(dst.owner, dst.mint)][0] += amount
    return flows


def classify(
    tx: dict[str, Any],
    wallets: set[str] | None = None,
    relayers: frozenset[str] = frozenset(),
) -> list[WalletEvent]:
    """Devuelve los eventos de ``wallets`` en la transacción.

    Si ``wallets`` es ``None`` (modo descubrimiento) se analizan los firmantes que
    no son relayers conocidos ni el pagador de la comisión. Un JSON con forma
    inesperada produce ``MalformedTransactionError``, nunca otro tipo de error.
    """
    try:
        return _classify(tx, wallets, relayers)
    except MalformedTransactionError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise MalformedTransactionError(f"{type(exc).__name__}: {exc}") from exc


def _classify(
    tx: dict[str, Any],
    wallets: set[str] | None,
    relayers: frozenset[str],
) -> list[WalletEvent]:
    summary = summarize(tx, relayers)
    if not summary.succeeded:
        return []
    meta = tx["meta"]
    keys = _account_keys(tx)
    index_of = {a.pubkey: i for i, a in enumerate(keys)}
    signer_set = set(summary.signers)
    pre_lamports: list[int] = meta["preBalances"]
    post_lamports: list[int] = meta["postBalances"]
    token_accounts = _token_accounts(meta)
    funder_of, close_destination_of = _rent_flows(tx)

    if wallets is None:
        candidates = [
            s for s in summary.signers if s not in relayers and s != summary.fee_payer
        ] or [summary.fee_payer]
    else:
        candidates = sorted(wallets)

    # Cambios por (owner, mint) en unidades mínimas.
    by_owner: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    decimals_of: dict[str, int] = {}
    for st in token_accounts.values():
        decimals_of[st.mint] = st.decimals
        if st.owner is not None:
            by_owner[st.owner][st.mint] += st.post - st.pre

    has_swap_program = bool(summary.programs & KNOWN_SWAP_PROGRAMS.keys())
    gross_flows: dict[tuple[str, str], list[int]] | None = None
    events: list[WalletEvent] = []
    for wallet in candidates:
        if wallet not in index_of and wallet not in by_owner:
            continue
        reasons: list[str] = []
        is_signer = wallet in signer_set

        # --- SOL: nativo + wSOL, sin comisión de red ni renta de cuentas ---
        sol = 0
        if wallet in index_of:
            i = index_of[wallet]
            sol += post_lamports[i] - pre_lamports[i]
            if wallet == summary.fee_payer:
                sol += summary.fee_lamports
        sol += sum(
            st.post - st.pre
            for st in token_accounts.values()
            if st.owner == wallet and st.mint == WSOL_MINT
        )
        # Renta que la wallet inmoviliza al crear cuentas de token o recupera al
        # cerrarlas: no forma parte del precio. Para wSOL, los lamports de la cuenta
        # incluyen el saldo envuelto, que ya se ha contado arriba.
        for idx, st in token_accounts.items():
            # Solo la renta de cuentas propias es recuperable por la wallet. La de una
            # cuenta ajena (p. ej. la de comisiones de FOMO) no es inventario suyo; además
            # la instrucción ``create`` puede nombrar como pagadora a la wallet aunque los
            # lamports los ponga otro (caso real `4X7ciHuu…`, Fase 3).
            if st.owner != wallet:
                continue
            pubkey = keys[idx].pubkey
            content_pre = st.pre if st.mint == WSOL_MINT else 0
            content_post = st.post if st.mint == WSOL_MINT else 0
            if st.in_post and not st.in_pre and funder_of.get(pubkey) == wallet:
                sol += post_lamports[idx] - content_post
            elif st.in_pre and not st.in_post and close_destination_of.get(pubkey) == wallet:
                sol -= pre_lamports[idx] - content_pre
        if abs(sol) < SOL_DUST_LAMPORTS:
            sol = 0

        quote_deltas: dict[str, Decimal] = {}
        if sol:
            quote_deltas["SOL"] = Decimal(sol) / LAMPORTS_PER_SOL
        token_deltas: dict[str, Decimal] = {}
        for mint, raw in by_owner.get(wallet, {}).items():
            if raw == 0 or mint == WSOL_MINT:
                continue
            amount = Decimal(raw).scaleb(-decimals_of[mint])
            if mint in QUOTE_SYMBOLS:
                quote_deltas[QUOTE_SYMBOLS[mint]] = amount
            elif abs(raw) <= TOKEN_DUST_RAW_UNITS:
                # Restos de tokens intermedios de la ruta o airdrops de spam.
                reasons.append(f"se ignora polvo de {mint} ({amount:+})")
            else:
                token_deltas[mint] = amount

        if not token_deltas:
            continue  # Sin cambios de memecoins: nada que copiar.

        if len(token_deltas) > 1:
            if gross_flows is None:
                gross_flows = _gross_token_flows(tx, keys, token_accounts)
            for mint in list(token_deltas):
                inflow, outflow = gross_flows.get((wallet, mint), (0, 0))
                residue = abs(by_owner[wallet][mint])
                if (
                    inflow
                    and outflow
                    and residue <= ROUTE_RESIDUE_MAX_FRACTION * max(inflow, outflow)
                ):
                    pct = Decimal(residue * 100) / max(inflow, outflow)
                    reasons.append(
                        f"se ignora el resto de ruta de {mint} ({token_deltas[mint]:+}, "
                        f"{pct:.2f} % de lo que pasó por la wallet)"
                    )
                    del token_deltas[mint]
            if not token_deltas:
                continue

        if len(token_deltas) > 1:
            events.append(
                _event(
                    summary,
                    wallet,
                    EventKind.AMBIGUOUS,
                    Confidence.LOW,
                    None,
                    Decimal(0),
                    None,
                    Decimal(0),
                    is_signer,
                    [
                        *reasons,
                        f"cambian {len(token_deltas)} tokens no estables en la misma transacción",
                    ],
                )
            )
            continue

        mint, token_delta = next(iter(token_deltas.items()))
        stables = [q for q in quote_deltas if q != "SOL"]
        if "SOL" in quote_deltas and len(stables) == 1 and abs(sol) <= ACCOUNT_COSTS_MAX_LAMPORTS:
            reasons.append(
                f"se ignora un movimiento de {quote_deltas['SOL']:+} SOL junto al pago en "
                f"{stables[0]}: renta de cuentas o comisión, no forma parte del precio"
            )
            del quote_deltas["SOL"]
        opposite = {q: d for q, d in quote_deltas.items() if (d > 0) != (token_delta > 0)}
        same_side = {q: d for q, d in quote_deltas.items() if (d > 0) == (token_delta > 0)}

        if not quote_deltas:
            kind = EventKind.TRANSFER_IN if token_delta > 0 else EventKind.TRANSFER_OUT
            counterparties = tuple(
                owner
                for owner, mints in by_owner.items()
                if owner != wallet and mints.get(mint, 0) * token_delta < 0
            )
            confidence = Confidence.HIGH if counterparties else Confidence.MEDIUM
            if not counterparties:
                reasons.append("no se encuentra la contrapartida de la transferencia")
            events.append(
                _event(
                    summary,
                    wallet,
                    kind,
                    confidence,
                    mint,
                    token_delta,
                    None,
                    Decimal(0),
                    is_signer,
                    reasons,
                    counterparties,
                )
            )
            continue

        if len(opposite) == 1 and not same_side:
            quote_symbol, quote_delta = next(iter(opposite.items()))
            kind = EventKind.BUY if token_delta > 0 else EventKind.SELL
            confidence = Confidence.HIGH
            if not is_signer:
                confidence = Confidence.MEDIUM
                reasons.append("la wallet no firma la transacción")
            if not has_swap_program:
                confidence = Confidence.MEDIUM
                reasons.append("no interviene ningún programa de swap conocido")
            events.append(
                _event(
                    summary,
                    wallet,
                    kind,
                    confidence,
                    mint,
                    token_delta,
                    quote_symbol,
                    quote_delta,
                    is_signer,
                    reasons,
                )
            )
            continue

        detail = ", ".join(f"{q} {d:+}" for q, d in quote_deltas.items())
        events.append(
            _event(
                summary,
                wallet,
                EventKind.AMBIGUOUS,
                Confidence.LOW,
                mint,
                token_delta,
                None,
                Decimal(0),
                is_signer,
                [*reasons, f"los pagos no cuadran con una compra o venta simple ({detail})"],
            )
        )
    return events


def _event(
    summary: TxSummary,
    wallet: str,
    kind: EventKind,
    confidence: Confidence,
    mint: str | None,
    token_delta: Decimal,
    quote_symbol: str | None,
    quote_delta: Decimal,
    is_signer: bool,
    reasons: list[str],
    counterparties: tuple[str, ...] = (),
) -> WalletEvent:
    return WalletEvent(
        signature=summary.signature,
        slot=summary.slot,
        block_time=summary.block_time,
        wallet=wallet,
        kind=kind,
        confidence=confidence,
        mint=mint,
        token_delta=token_delta,
        quote_symbol=quote_symbol,
        quote_delta=quote_delta,
        wallet_is_signer=is_signer,
        fee_payer=summary.fee_payer,
        via_fomo=summary.via_fomo,
        reasons=tuple(reasons),
        counterparties=counterparties,
    )

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from copybot.solana.classifier import (
    Confidence,
    EventKind,
    MalformedTransactionError,
    classify,
    summarize,
)
from copybot.solana.constants import (
    FOMO_MARKER_ACCOUNT,
    TOKEN_PROGRAM,
    USDC_MINT,
    WSOL_MINT,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tx"
RELAYER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
RELAYERS = frozenset({RELAYER})


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


# --- Transacciones reales de FOMO --------------------------------------------------

REAL_CASES = [
    # (fixture, wallet, tipo, mint, delta del token, delta de USDC)
    (
        "fomo_sell_pumpswap_clmm",
        "7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm",
        EventKind.SELL,
        "7kASdbzxWLdioHhzYvXwtM3tRztAbFmeLKF4bDW8pump",
        "-32555.916793",
        "2.195302",
    ),
    (
        "fomo_buy_pumpfun",
        "Gvg5vebyNUYgDP6U5jJLS3wAgbkhgJ19xHVpBLHtmGSy",
        EventKind.BUY,
        "JBxMJxRxW3SJhsWrruzZhYEpSq55DdVPArbzHNmSpump",
        "1212619.561654",
        "-11.000000",
    ),
    (
        "fomo_sell_all_pumpfun",
        "H2G6xK3oHaj7MgmV79nKk1vxh75MwsjvsPbDuHRqNbqF",
        EventKind.SELL,
        "8j5rxz1fWxdaJpqcWUgxUsW5aKQGcbJ64YoaGikfpump",
        "-12622466.097825",
        "76.805399",
    ),
    (
        "fomo_sell_jupiter",
        "HtbRGAFB4iPW6B7MfFb3sd6RKBSwYfKu71hakahAfmKn",
        EventKind.SELL,
        "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh",
        "-0.01419432",
        "2.086276",
    ),
    # Token-2022 con comisión de transferencia del 1 %: cuenta lo que realmente recibe
    # el usuario (30279.536642), no lo que envían los pools (30585.390548).
    # La ruta deja 0,00000001 de un token intermedio en la wallet: es polvo, no otra compra.
    (
        "fomo_buy_with_route_dust",
        "4E528fvewR7ySEfb6WMnv9jBDZC63ku26Cf2UrQ7wqLM",
        EventKind.BUY,
        "BfZCMNKLQkiTyTMuu7gkEQyJD2wKpzkQWhPGsGWgpump",
        "784959.184917",
        "-5.171235",
    ),
    (
        "fomo_buy_token2022_transfer_fee",
        "FhvhGFa4BLCgxreKj1vHpJ8ekGa2pyL9bmVomiwk99CC",
        EventKind.BUY,
        "DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP",
        "30279.536642",
        "-487.571354",
    ),
    # Rutas USDC → token intermedio → memecoin que dejan en la wallet un resto del
    # intermedio mayor que el polvo (0,69 % y 0,89 % de lo que pasó por ella). Con el
    # clasificador v1 quedaban como ambiguas; son compras normales.
    (
        "fomo_buy_route_residue_token2022",
        "7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm",
        EventKind.BUY,
        "DKYcjobF7HPtL9RyQBRdeRQ2iWvizpfbgWwPWNXdpump",
        "176163.768233",
        "-25.000000",
    ),
    (
        "fomo_buy_route_residue_9bb6",
        "7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm",
        EventKind.BUY,
        "DrzAtxfTZQtvfCr45NM4H6gYoSridMWWusxn7pmzpump",
        "107538.895215",
        "-12.946971",
    ),
    # La wallet figura como pagadora de la cuenta de comisiones de otra wallet, pero los
    # lamports los pone el relayer. El clasificador v1 le atribuía esa renta y la
    # compra quedaba ambigua (SOL +0,00148844 junto a USDC −444).
    (
        "fomo_buy_fee_account_rent",
        "FpYqABXnVFF8jg2FBLvgFwNxa3Sm5dB2oqrtGLu2tvjT",
        EventKind.BUY,
        "BnhSAHEfQU1VaczcnmiqioJG1xnU7tfhX91wCkCopump",
        "16378797.554909",
        "-444.000000",
    ),
    # FOMO cobra 2.039.280 lamports por la cuenta nueva y devuelve 1.488.440 al cerrar
    # una cuenta temporal de wSOL: el usuario queda en −0,00055084 SOL además de los 50
    # USDC. Es coste de cuentas, no parte del precio (1.724 casos en la Fase 3).
    (
        "fomo_buy_account_cost_sol",
        "GqvndQ2bxjNL2Sn7dcxo2ESPAWSDaR9nsB7x3LZ6W7Jd",
        EventKind.BUY,
        "GTBxUiw6wJdmmkCGZgRHLyYxqu1vG4KtRpeox6yDpump",
        "15946.910770",
        "-50.000000",
    ),
]


@pytest.mark.parametrize(("fixture", "wallet", "kind", "mint", "token", "usdc"), REAL_CASES)
def test_real_fomo_trades_discovery_mode(fixture, wallet, kind, mint, token, usdc):
    events = classify(load(fixture), None, RELAYERS)
    assert len(events) == 1
    ev = events[0]
    assert ev.wallet == wallet
    assert ev.kind is kind
    assert ev.confidence is Confidence.HIGH
    assert ev.usable_as_signal
    assert ev.mint == mint
    assert ev.token_delta == Decimal(token)
    assert ev.quote_symbol == "USDC"
    assert ev.quote_delta == Decimal(usdc)
    assert ev.wallet_is_signer
    assert ev.fee_payer == RELAYER
    assert ev.via_fomo


@pytest.mark.parametrize(("fixture", "wallet", "kind", "mint", "token", "usdc"), REAL_CASES)
def test_real_fomo_trades_watched_wallet(fixture, wallet, kind, mint, token, usdc):
    events = classify(load(fixture), {wallet}, RELAYERS)
    assert [(e.kind, e.mint, e.token_delta) for e in events] == [(kind, mint, Decimal(token))]


def test_relayer_is_never_attributed_as_trader():
    for fixture, *_ in REAL_CASES:
        assert classify(load(fixture), {RELAYER}, RELAYERS) == []


def test_unrelated_wallet_gets_no_events():
    assert classify(load("fomo_buy_pumpfun"), {RELAYER[::-1][:44]}, RELAYERS) == []


def test_deposit_is_not_a_trade():
    tx = load("fomo_deposit_not_a_trade")
    assert classify(tx, None, RELAYERS) == []
    summary = summarize(tx, RELAYERS)
    assert summary.via_fomo
    assert summary.fee_payer == RELAYER


def test_summary_of_real_trade():
    summary = summarize(load("fomo_sell_pumpswap_clmm"), RELAYERS)
    assert summary.signers == (RELAYER, "7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm")
    assert summary.fee_lamports == 410_000
    assert summary.succeeded
    assert "DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH" in summary.programs


def test_price_includes_platform_fee():
    ev = classify(load("fomo_sell_pumpswap_clmm"), None, RELAYERS)[0]
    assert ev.price_in_quote == Decimal("2.195302") / Decimal("32555.916793")


# --- Transacciones sintéticas (construidas en el test) -----------------------------

W = "7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm"  # wallet seguida
X = "Gvg5vebyNUYgDP6U5jJLS3wAgbkhgJ19xHVpBLHtmGSy"  # otra wallet
POOL = "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"
MEME = "7kASdbzxWLdioHhzYvXwtM3tRztAbFmeLKF4bDW8pump"
MEME2 = "JBxMJxRxW3SJhsWrruzZhYEpSq55DdVPArbzHNmSpump"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def synthetic_tx(
    accounts: list[tuple[str, bool]],
    lamports: list[tuple[int, int]],
    token_balances: list[tuple[int, str, str, int | None, int | None]],
    programs: tuple[str, ...] = (PUMPSWAP,),
    inner: list[dict[str, Any]] | None = None,
    fee: int = 5_000,
    err: Any = None,
) -> dict[str, Any]:
    """Construye una transacción mínima con la forma de ``jsonParsed``.

    ``token_balances``: (índice de cuenta, owner, mint, importe antes, importe después);
    ``None`` indica que la cuenta no existía antes o ya no existe después.
    """
    keys = [
        {"pubkey": k, "signer": s, "writable": True, "source": "transaction"} for k, s in accounts
    ]
    keys += [
        {"pubkey": p, "signer": False, "writable": False, "source": "transaction"} for p in programs
    ]
    pre_bal = [a for a, _ in lamports] + [1] * len(programs)
    post_bal = [b for _, b in lamports] + [1] * len(programs)
    pre_tok, post_tok = [], []
    for idx, owner, mint, before, after in token_balances:
        for amount, bucket in ((before, pre_tok), (after, post_tok)):
            if amount is not None:
                bucket.append(
                    {
                        "accountIndex": idx,
                        "mint": mint,
                        "owner": owner,
                        "programId": TOKEN_PROGRAM,
                        "uiTokenAmount": {"amount": str(amount), "decimals": 6},
                    }
                )
    return {
        "slot": 1,
        "blockTime": 1_700_000_000,
        "version": 0,
        "transaction": {
            "signatures": ["synthetic"],
            "message": {
                "accountKeys": keys,
                "instructions": [{"programId": p, "accounts": [], "data": ""} for p in programs],
            },
        },
        "meta": {
            "err": err,
            "fee": fee,
            "preBalances": pre_bal,
            "postBalances": post_bal,
            "preTokenBalances": pre_tok,
            "postTokenBalances": post_tok,
            "innerInstructions": [{"index": 0, "instructions": inner or []}],
            "logMessages": [],
        },
    }


def test_synthetic_transfer_between_wallets():
    tx = synthetic_tx(
        accounts=[
            (W, True),
            ("WAcc1111111111111111111111111111111111111111", False),
            ("XAcc1111111111111111111111111111111111111111", False),
        ],
        lamports=[(10**9, 10**9 - 5_000), (2_000_000, 2_000_000), (2_000_000, 2_000_000)],
        token_balances=[(1, W, MEME, 500_000_000, 0), (2, X, MEME, 0, 500_000_000)],
        programs=(TOKEN_PROGRAM,),
    )
    [ev] = classify(tx, {W})
    assert ev.kind is EventKind.TRANSFER_OUT
    assert ev.counterparties == (X,)
    assert ev.confidence is Confidence.HIGH
    assert not ev.usable_as_signal  # una transferencia no es una venta


def test_synthetic_token_to_token_swap_is_ambiguous():
    tx = synthetic_tx(
        accounts=[
            (W, True),
            ("WAcc1111111111111111111111111111111111111111", False),
            ("WAcc2222222222222222222222222222222222222222", False),
        ],
        lamports=[(10**9, 10**9 - 5_000), (2_000_000, 2_000_000), (2_000_000, 2_000_000)],
        token_balances=[(1, W, MEME, 500, 0), (2, W, MEME2, 0, 900)],
    )
    [ev] = classify(tx, {W})
    assert ev.kind is EventKind.AMBIGUOUS
    assert ev.confidence is Confidence.LOW
    assert not ev.usable_as_signal


def test_synthetic_failed_transaction_has_no_events():
    tx = synthetic_tx(
        accounts=[(W, True), ("WAcc1111111111111111111111111111111111111111", False)],
        lamports=[(10**9, 10**9 - 5_000), (2_000_000, 2_000_000)],
        token_balances=[(1, W, USDC_MINT, 10_000_000, 0)],
        err={"InstructionError": [0, "Custom"]},
    )
    assert classify(tx, {W}) == []


def test_synthetic_sol_buy_excludes_network_fee_and_rent():
    """La wallet paga la comisión de red y la renta de su nueva cuenta de token.

    Nada de eso es parte del precio: el pago en SOL debe ser exactamente 0,5 SOL.
    """
    rent = 1_488_440
    new_ata = "WAta1111111111111111111111111111111111111111"
    paid = 500_000_000
    tx = synthetic_tx(
        accounts=[(W, True), (new_ata, False), (POOL, False)],
        lamports=[(10**9, 10**9 - 5_000 - rent - paid), (0, rent), (5 * 10**9, 5 * 10**9 + paid)],
        token_balances=[(1, W, MEME, None, 777_000_000)],
        inner=[
            {
                "program": "spl-associated-token-account",
                "programId": "ATok",
                "parsed": {
                    "type": "createIdempotent",
                    "info": {"account": new_ata, "source": W, "wallet": W, "mint": MEME},
                },
            }
        ],
    )
    [ev] = classify(tx, {W})
    assert ev.kind is EventKind.BUY
    assert ev.quote_symbol == "SOL"
    assert ev.quote_delta == Decimal("-0.5")
    assert ev.token_delta == Decimal("777")


def test_synthetic_sell_to_wsol_closing_accounts():
    """Venta cobrada en wSOL que se desenvuelve: se cierra la cuenta wSOL y la del token."""
    rent = 1_488_440
    proceeds = 250_000_000
    wsol_acc = "WSol1111111111111111111111111111111111111111"
    meme_acc = "WAcc1111111111111111111111111111111111111111"
    tx = synthetic_tx(
        accounts=[(W, True), (meme_acc, False), (wsol_acc, False), (POOL, False)],
        # El pool paga en la cuenta wSOL de la wallet (vacía antes); al cerrar las dos
        # cuentas, la wallet recupera su renta y el SOL de la venta.
        lamports=[
            (10**9, 10**9 - 5_000 + rent + rent + proceeds),
            (rent, 0),
            (rent, 0),
            (5 * 10**9, 5 * 10**9 - proceeds),
        ],
        token_balances=[(1, W, MEME, 1_000_000, None), (2, W, WSOL_MINT, 0, None)],
        inner=[
            {
                "program": "spl-token",
                "programId": TOKEN_PROGRAM,
                "parsed": {
                    "type": "closeAccount",
                    "info": {"account": acc, "destination": W, "owner": W},
                },
            }
            for acc in (meme_acc, wsol_acc)
        ],
    )
    [ev] = classify(tx, {W})
    assert ev.kind is EventKind.SELL
    assert ev.quote_symbol == "SOL"
    assert ev.quote_delta == Decimal("0.25")


def test_synthetic_non_signer_trade_is_medium_confidence():
    tx = synthetic_tx(
        accounts=[
            (X, True),
            ("WAcc1111111111111111111111111111111111111111", False),
            ("WUsd1111111111111111111111111111111111111111", False),
            (W, False),
        ],
        lamports=[
            (10**9, 10**9 - 5_000),
            (2_000_000, 2_000_000),
            (2_000_000, 2_000_000),
            (10**8, 10**8),
        ],
        token_balances=[(1, W, MEME, 0, 1_000_000), (2, W, USDC_MINT, 5_000_000, 0)],
    )
    [ev] = classify(tx, {W})
    assert ev.kind is EventKind.BUY
    assert ev.confidence is Confidence.MEDIUM
    assert not ev.usable_as_signal
    assert "la wallet no firma la transacción" in ev.reasons


def test_synthetic_trade_without_known_swap_program_is_medium_confidence():
    tx = synthetic_tx(
        accounts=[
            (W, True),
            ("WAcc1111111111111111111111111111111111111111", False),
            ("WUsd1111111111111111111111111111111111111111", False),
        ],
        lamports=[(10**9, 10**9 - 5_000), (2_000_000, 2_000_000), (2_000_000, 2_000_000)],
        token_balances=[(1, W, MEME, 0, 1_000_000), (2, W, USDC_MINT, 5_000_000, 0)],
        programs=(TOKEN_PROGRAM,),
    )
    [ev] = classify(tx, {W})
    assert ev.kind is EventKind.BUY
    assert ev.confidence is Confidence.MEDIUM


def test_synthetic_fomo_marker_detected_without_relayer_list():
    tx = synthetic_tx(
        accounts=[(W, True), (FOMO_MARKER_ACCOUNT, False)],
        lamports=[(10**9, 10**9 - 5_000), (1, 1)],
        token_balances=[],
    )
    assert summarize(tx).via_fomo


def test_malformed_transaction_is_rejected():
    with pytest.raises(MalformedTransactionError):
        classify({"transaction": {}}, None)


def test_route_dust_is_reported_not_traded():
    [ev] = classify(load("fomo_buy_with_route_dust"), None, RELAYERS)
    assert any("polvo" in r for r in ev.reasons)


def test_route_residue_is_reported_not_traded():
    [ev] = classify(load("fomo_buy_route_residue_token2022"), None, RELAYERS)
    assert ev.reasons == (
        "se ignora el resto de ruta de TTWofwAge91oFhZs7kpQdyrVRkmevgM88xijGvQFbKo "
        "(+0.000813, 0.69 % de lo que pasó por la wallet)",
    )


def test_small_sol_next_to_a_stable_payment_is_an_account_cost():
    [ev] = classify(load("fomo_buy_account_cost_sol"), None, RELAYERS)
    assert ev.reasons == (
        "se ignora un movimiento de -0.00055084 SOL junto al pago en USDC: renta de cuentas "
        "o comisión, no forma parte del precio",
    )


def test_larger_sol_next_to_a_stable_payment_stays_ambiguous():
    """Comprar a la vez el token y algo de SOL con el mismo USDC no es una compra simple."""
    tx = synthetic_tx(
        accounts=[(W, True), (W_USDC, False), (W_MEME, False)],
        lamports=[(10**9, 10**9 + 8_000_000), (2_000_000,) * 2, (2_000_000,) * 2],
        token_balances=[(1, W, USDC_MINT, 100_000_000, 0), (2, W, MEME, 0, 5_000_000)],
    )
    [ev] = classify(tx, {W})
    assert ev.kind is EventKind.AMBIGUOUS


def test_pumpfun_fee_distribution_is_a_transfer_not_a_buy():
    """Reparto automático de pump.fun a los poseedores (firma y paga otra wallet)."""
    [ev] = classify(load("pumpfun_fee_distribution_transfer_in"), {W}, RELAYERS)
    assert ev.kind is EventKind.TRANSFER_IN
    assert ev.token_delta == Decimal("4.701609")
    assert ev.counterparties == ("9nwP5sM7wfx9P7EbSDHTg6LUmYwSwujzAGbscYAVd3HZ",)
    assert not ev.wallet_is_signer
    assert not ev.usable_as_signal


def _transfer(source: str, destination: str, amount: int) -> dict[str, Any]:
    return {
        "program": "spl-token",
        "programId": TOKEN_PROGRAM,
        "parsed": {
            "type": "transfer",
            "info": {"source": source, "destination": destination, "amount": str(amount)},
        },
    }


W_USDC = "WAcc1111111111111111111111111111111111111111"
W_MID = "WAcc2222222222222222222222222222222222222222"
W_MEME = "WAcc3333333333333333333333333333333333333333"
POOL_A = "PAcc1111111111111111111111111111111111111111"
POOL_B = "PAcc2222222222222222222222222222222222222222"


def _routed_buy(mid_in: int, mid_out: int) -> dict[str, Any]:
    """Compra USDC → MEME2 (intermedio) → MEME en la que MEME2 pasa por la wallet."""
    return synthetic_tx(
        accounts=[(W, True), (W_USDC, False), (W_MID, False), (W_MEME, False)],
        lamports=[(10**9, 10**9 - 5_000), (2_000_000,) * 2, (2_000_000,) * 2, (2_000_000,) * 2],
        token_balances=[
            (1, W, USDC_MINT, 10_000_000, 0),
            (2, W, MEME2, 0, mid_in - mid_out),
            (3, W, MEME, 0, 5_000_000_000),
        ],
        inner=[
            _transfer(W_USDC, POOL_A, 10_000_000),
            _transfer(POOL_A, W_MID, mid_in),
            _transfer(W_MID, POOL_B, mid_out),
            _transfer(POOL_B, W_MEME, 5_000_000_000),
        ],
    )


def test_synthetic_small_route_residue_is_ignored():
    [ev] = classify(_routed_buy(mid_in=1_000_000, mid_out=990_000), {W})  # resto del 1 %
    assert (ev.kind, ev.confidence, ev.mint) == (EventKind.BUY, Confidence.HIGH, MEME)
    assert ev.quote_delta == Decimal("-10")


def test_synthetic_large_route_residue_stays_ambiguous():
    [ev] = classify(_routed_buy(mid_in=1_000_000, mid_out=900_000), {W})  # resto del 10 %
    assert ev.kind is EventKind.AMBIGUOUS


@pytest.mark.parametrize(
    "mutate",
    [
        lambda tx: tx["meta"].pop("preBalances"),
        lambda tx: tx["meta"]["postTokenBalances"][0].pop("uiTokenAmount"),
        lambda tx: tx["meta"].__setitem__("preBalances", None),
        lambda tx: tx["transaction"]["message"].__setitem__("accountKeys", "basura"),
        lambda tx: tx["meta"]["preTokenBalances"][0].__setitem__("accountIndex", 9999),
    ],
)
def test_unexpected_shapes_raise_malformed_only(mutate):
    tx = load("fomo_sell_pumpswap_clmm")
    mutate(tx)
    with pytest.raises(MalformedTransactionError):
        classify(tx, None, RELAYERS)

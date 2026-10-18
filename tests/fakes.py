"""Dobles de prueba: un RPC en memoria y un generador de transacciones sintéticas."""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any

from copybot.solana.constants import TOKEN_PROGRAM, USDC_MINT
from copybot.solana.rpc import RpcError

RELAYER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
MEME = "7kASdbzxWLdioHhzYvXwtM3tRztAbFmeLKF4bDW8pump"


def fomo_like_swap(
    signature: str,
    slot: int,
    wallet: str,
    *,
    token_raw_delta: int,
    usdc_raw_delta: int,
    mint: str = MEME,
    block_time: int | None = None,
    err: Any = None,
) -> dict[str, Any]:
    """Transacción SINTÉTICA con la forma de una compraventa de FOMO (jsonParsed).

    El relayer firma y paga; ``wallet`` es el segundo firmante y propietario de las
    cuentas de token que cambian.
    """
    token_acc = "TokAcc" + signature[:10].ljust(10, "1") + "1" * 28
    usdc_acc = "UsdAcc" + signature[:10].ljust(10, "1") + "1" * 28
    keys = [
        {"pubkey": RELAYER, "signer": True, "writable": True, "source": "transaction"},
        {"pubkey": wallet, "signer": True, "writable": True, "source": "transaction"},
        {"pubkey": token_acc[:44], "signer": False, "writable": True, "source": "transaction"},
        {"pubkey": usdc_acc[:44], "signer": False, "writable": True, "source": "transaction"},
        {"pubkey": PUMPSWAP, "signer": False, "writable": False, "source": "transaction"},
    ]

    def bal(idx: int, mint_: str, amount: int) -> dict[str, Any]:
        return {
            "accountIndex": idx,
            "mint": mint_,
            "owner": wallet,
            "programId": TOKEN_PROGRAM,
            "uiTokenAmount": {"amount": str(amount), "decimals": 6},
        }

    token_pre = 10**12
    usdc_pre = 10**9
    return {
        "slot": slot,
        "blockTime": block_time if block_time is not None else 1_700_000_000 + slot,
        "version": 1,
        "transaction": {
            "signatures": [signature, "x" * 88],
            "message": {
                "accountKeys": keys,
                "instructions": [{"programId": PUMPSWAP, "accounts": [], "data": ""}],
            },
        },
        "meta": {
            "err": err,
            "fee": 410_000,
            "preBalances": [10**10, 10**7, 2_039_280, 2_039_280, 1],
            "postBalances": [10**10 - 410_000, 10**7, 2_039_280, 2_039_280, 1],
            "preTokenBalances": [bal(2, mint, token_pre), bal(3, USDC_MINT, usdc_pre)],
            "postTokenBalances": [
                bal(2, mint, token_pre + token_raw_delta),
                bal(3, USDC_MINT, usdc_pre + usdc_raw_delta),
            ],
            "innerInstructions": [],
            "logMessages": [],
        },
    }


class FakeRpc:
    """RPC en memoria con la semántica de ``getSignaturesForAddress`` de Solana."""

    def __init__(self) -> None:
        self.history: dict[str, list[dict[str, Any]]] = defaultdict(list)  # más reciente primero
        self.transactions: dict[str, dict[str, Any]] = {}
        self.unavailable: set[str] = set()
        self.failing: set[str] = set()  # simulan un proveedor que responde 429
        self.calls: list[tuple[str, Any]] = []

    def add(self, tx: dict[str, Any], addresses: list[str]) -> str:
        signature = tx["transaction"]["signatures"][0]
        self.transactions[signature] = tx
        entry = {
            "signature": signature,
            "slot": tx["slot"],
            "err": tx["meta"]["err"],
            "blockTime": tx["blockTime"],
        }
        for address in addresses:
            self.history[address].insert(0, entry)
        return signature

    async def get_signatures_for_address(
        self,
        address: str,
        *,
        before: str | None = None,
        until: str | None = None,
        limit: int = 1000,
        commitment: str = "confirmed",
    ) -> list[dict[str, Any]]:
        self.calls.append(("getSignaturesForAddress", (address, before, until, limit)))
        items = self.history.get(address, [])
        if before is not None:
            index = next(i for i, e in enumerate(items) if e["signature"] == before)
            items = items[index + 1 :]
        out = []
        for entry in items:
            if entry["signature"] == until:
                break
            out.append(copy.deepcopy(entry))
            if len(out) == limit:
                break
        return out

    async def get_transaction(
        self, signature: str, commitment: str = "confirmed"
    ) -> dict[str, Any] | None:
        self.calls.append(("getTransaction", signature))
        if signature in self.failing:
            raise RpcError("getTransaction", "HTTP 429", 429)
        if signature in self.unavailable:
            return None
        return copy.deepcopy(self.transactions.get(signature))

    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)

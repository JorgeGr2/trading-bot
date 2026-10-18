"""Dobles de prueba: un RPC en memoria y un generador de transacciones sintéticas."""

from __future__ import annotations

import copy
from collections import Counter, defaultdict
from typing import Any

from copybot.solana.constants import TOKEN_PROGRAM, USDC_MINT
from copybot.solana.rpc import RpcError, TransactionPage

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


class FakeHistoryRpc:
    """``getTransactionsForAddress`` en memoria (orden, filtros de tiempo y paginación)."""

    def __init__(self, credits_per_call: int = 100) -> None:
        self.by_address: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.credits: Counter[str] = Counter()
        self.calls: list[dict[str, Any]] = []
        self.fail_after: int | None = None  # simula un corte tras N peticiones
        self._cost = credits_per_call

    def add(self, address: str, tx: dict[str, Any], tx_index: int = 0) -> None:
        item = copy.deepcopy(tx)
        item["transactionIndex"] = tx_index
        self.by_address[address].append(item)

    async def get_transactions_for_address(
        self,
        address: str,
        *,
        full: bool,
        limit: int = 1000,
        sort_order: str = "desc",
        block_time_gte: int | None = None,
        block_time_lt: int | None = None,
        pagination_token: str | None = None,
        associated_accounts: str = "balanceChanged",
        only_succeeded: bool = True,
    ) -> TransactionPage:
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RpcError("getTransactionsForAddress", "HTTP 429", 429)
        self.calls.append(
            {
                "address": address,
                "gte": block_time_gte,
                "lt": block_time_lt,
                "token": pagination_token,
                "order": sort_order,
            }
        )
        self.credits["getTransactionsForAddress"] += self._cost
        items = [
            t
            for t in self.by_address.get(address, [])
            if (block_time_gte is None or t["blockTime"] >= block_time_gte)
            and (block_time_lt is None or t["blockTime"] < block_time_lt)
            and (not only_succeeded or t["meta"]["err"] is None)
        ]
        items.sort(key=lambda t: (t["slot"], t["transactionIndex"]), reverse=sort_order == "desc")
        offset = int(pagination_token or 0)
        page = items[offset : offset + limit]
        if not full:
            page = [
                {
                    "signature": t["transaction"]["signatures"][0],
                    "slot": t["slot"],
                    "blockTime": t["blockTime"],
                    "err": t["meta"]["err"],
                }
                for t in page
            ]
        token = str(offset + limit) if len(page) == limit else None
        return TransactionPage([copy.deepcopy(t) for t in page], token)

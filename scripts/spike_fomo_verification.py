"""Verificación de la atribución de operaciones de FOMO con transacciones reales.

Compara dos métodos independientes sobre cada transacción firmada por el relayer:

* Método A (el que usará el sistema): ``copybot.solana.classifier``, basado en los
  balances de token antes y después de la transacción.
* Método B (control): reconstrucción a partir de las instrucciones de transferencia
  (``transfer``/``transferChecked``) en las que interviene la wallet del usuario.

Uso:
    uv run python scripts/spike_fomo_verification.py --cache-dir DIR [--fetch N]

Con ``--fetch N`` descarga primero las últimas N transacciones del relayer usando
``--rpc-url`` (por defecto el RPC público de Solana, muy limitado en velocidad).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from copybot.solana.classifier import EventKind, classify, summarize
from copybot.solana.constants import KNOWN_SWAP_PROGRAMS, QUOTE_SYMBOLS, USDC_MINT

RELAYER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"


def rpc(url: str, method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})  # noqa: S310
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                data = json.load(resp)
            if "error" in data:
                raise RuntimeError(data["error"])
            return data["result"]
        except urllib.error.HTTPError as exc:
            wait = float(exc.headers.get("Retry-After") or 5 * (attempt + 1))
            print(f"  HTTP {exc.code}, reintento en {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"{method}: demasiados reintentos")


def fetch(url: str, cache: Path, n: int, gap: float) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    sigs = rpc(url, "getSignaturesForAddress", [RELAYER, {"limit": min(n, 1000)}])
    (cache / "_signatures.json").write_text(json.dumps(sigs))
    for s in sigs:
        if s["err"] is not None:
            continue
        path = cache / f"{s['signature']}.json"
        if path.exists():
            continue
        tx = rpc(
            url,
            "getTransaction",
            [
                s["signature"],
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 1,
                    "commitment": "finalized",
                },
            ],
        )
        path.write_text(json.dumps(tx))
        time.sleep(gap)


def method_b(tx: dict[str, Any], wallet: str) -> dict[str, Decimal]:
    """Deltas de la wallet reconstruidos solo con instrucciones de transferencia."""
    meta = tx["meta"]
    keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
    acct_mint: dict[str, str] = {}
    acct_owner: dict[str, str] = {}
    decimals: dict[str, int] = {}
    for bal in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        acct_mint[keys[bal["accountIndex"]]] = bal["mint"]
        acct_owner[keys[bal["accountIndex"]]] = bal.get("owner", "")
        decimals[bal["mint"]] = int(bal["uiTokenAmount"]["decimals"])
    ixs = list(tx["transaction"]["message"]["instructions"])
    for inner in meta.get("innerInstructions") or []:
        ixs += inner["instructions"]
    deltas: dict[str, int] = defaultdict(int)
    for ix in ixs:
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") not in (
            "transfer",
            "transferChecked",
        ):
            continue
        if ix.get("program") not in ("spl-token", "spl-token-2022"):
            continue
        info = parsed["info"]
        mint = (
            info.get("mint") or acct_mint.get(info["source"]) or acct_mint.get(info["destination"])
        )
        if mint is None:
            continue
        amount = int(info["tokenAmount"]["amount"] if "tokenAmount" in info else info["amount"])
        if acct_owner.get(info["source"]) == wallet:
            deltas[mint] -= amount
        if acct_owner.get(info["destination"]) == wallet:
            deltas[mint] += amount
    return {m: Decimal(v).scaleb(-decimals.get(m, 0)) for m, v in deltas.items() if v}


def fomo_fee(tx: dict[str, Any], wallet: str) -> Decimal | None:
    """Comisión de FOMO: transferencia de USDC del usuario en una instrucción de primer nivel."""
    for ix in tx["transaction"]["message"]["instructions"]:
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") != "transferChecked":
            continue
        info = parsed["info"]
        if info.get("authority") == wallet and info.get("mint") == USDC_MINT:
            return Decimal(info["tokenAmount"]["amount"]).scaleb(-6)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", type=Path, required=True)
    ap.add_argument("--fetch", type=int, default=0)
    ap.add_argument("--rpc-url", default="https://api.mainnet-beta.solana.com")
    ap.add_argument("--gap", type=float, default=3.0)
    args = ap.parse_args()
    if args.fetch:
        fetch(args.rpc_url, args.cache_dir, args.fetch, args.gap)

    relayers = frozenset({RELAYER})
    files = sorted(p for p in args.cache_dir.glob("*.json") if not p.name.startswith("_"))
    stats: Counter[str] = Counter()
    programs: Counter[str] = Counter()
    fee_pct: list[float] = []
    fee_abs: list[Decimal] = []
    disagreements: list[str] = []
    wallets: set[str] = set()
    for path in files:
        tx = json.loads(path.read_text())
        summary = summarize(tx, relayers)
        stats["tx"] += 1
        stats[f"fee_payer_is_relayer={summary.fee_payer == RELAYER}"] += 1
        stats[f"signers={len(summary.signers)}"] += 1
        stats[f"via_fomo={summary.via_fomo}"] += 1
        for prog in summary.programs & KNOWN_SWAP_PROGRAMS.keys():
            programs[KNOWN_SWAP_PROGRAMS[prog]] += 1
        events = classify(tx, None, relayers)
        if not events:
            stats["sin_evento_de_memecoin"] += 1
            continue
        for ev in events:
            wallets.add(ev.wallet)
            stats[f"evento={ev.kind.value}/{ev.confidence.value}"] += 1
            b = method_b(tx, ev.wallet)
            b_tokens = {m: d for m, d in b.items() if m not in QUOTE_SYMBOLS}
            b_usdc = b.get(USDC_MINT, Decimal(0))
            ok = (
                ev.kind in (EventKind.BUY, EventKind.SELL)
                and b_tokens == {ev.mint: ev.token_delta}
                and (ev.quote_symbol != "USDC" or b_usdc == ev.quote_delta)
            )
            stats[f"coincide_metodo_B={ok}"] += 1
            if not ok:
                disagreements.append(
                    f"{summary.signature}: A={ev.kind.value} {ev.mint} {ev.token_delta} "
                    f"{ev.quote_symbol} {ev.quote_delta} | B={b}"
                )
            fee = fomo_fee(tx, ev.wallet)
            if fee is not None and ev.quote_symbol == "USDC":
                gross = (
                    abs(ev.quote_delta) + fee if ev.kind is EventKind.SELL else abs(ev.quote_delta)
                )
                fee_abs.append(fee)
                fee_pct.append(float(fee / gross * 100))

    print(f"Transacciones analizadas: {stats['tx']}")
    for key in sorted(stats):
        if key != "tx":
            print(f"  {key}: {stats[key]}")
    print(f"Wallets de usuario distintas: {len(wallets)}")
    print("Programas de swap:", dict(programs.most_common()))
    if fee_pct:
        print(
            f"Comisión FOMO observada: n={len(fee_pct)}, mediana {statistics.median(fee_pct):.2f} %, "
            f"min {min(fee_pct):.2f} %, max {max(fee_pct):.2f} %, "
            f"mínimo absoluto {min(fee_abs)} USDC"
        )
    if disagreements:
        print("Discrepancias A/B:")
        for line in disagreements:
            print("  ", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

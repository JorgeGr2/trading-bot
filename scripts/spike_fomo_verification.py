"""Verificación de la atribución de operaciones de FOMO con transacciones reales.

Compara dos métodos independientes sobre cada transacción firmada por el relayer:

* Método A (el que usará el sistema): ``copybot.solana.classifier``, basado en los
  balances de token antes y después de la transacción.
* Método B (control): reconstrucción a partir de las instrucciones de transferencia
  (``transfer``/``transferChecked``) en las que interviene la wallet del usuario.

Uso:
    uv run python scripts/spike_fomo_verification.py --cache-dir DIR [--fetch N]
    uv run python scripts/spike_fomo_verification.py --cache-dir DIR \
        --helius-windows 6 --window-days 7 --per-window 100 --seed 3

Con ``--fetch N`` descarga primero las últimas N transacciones del relayer usando
``--rpc-url`` (por defecto el RPC público de Solana, muy limitado en velocidad).

Con ``--helius-windows K`` (Fase 3) descarga con ``getTransactionsForAddress`` de
Helius K ventanas de ``--per-window`` transacciones consecutivas, que empiezan en
instantes elegidos al azar (con ``--seed``, reproducible) dentro de los últimos
``--window-days`` días. Así la muestra no depende de la hora en que se ejecuta. La
clave se lee de ``COPYBOT_HELIUS_API_KEY`` y nunca se imprime.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
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


def fetch(url: str, cache: Path, n: int, gap: float, stride: int = 1) -> None:
    """Descarga ``n`` transacciones, tomando una de cada ``stride`` para repartir la
    muestra en el tiempo (con ``stride`` 10 y ``n`` 100 cubre las últimas 1000)."""
    cache.mkdir(parents=True, exist_ok=True)
    sigs = rpc(url, "getSignaturesForAddress", [RELAYER, {"limit": min(n * stride, 1000)}])
    (cache / "_signatures.json").write_text(json.dumps(sigs))
    for s in sigs[::stride][:n]:
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


def fetch_helius_windows(
    cache: Path, windows: int, days: float, per_window: int, seed: int
) -> None:
    key = os.environ.get("COPYBOT_HELIUS_API_KEY")
    if not key:
        raise SystemExit("Falta COPYBOT_HELIUS_API_KEY")
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    cache.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)  # noqa: S311 (muestreo reproducible, no criptografía)
    now = int(time.time()) - 120
    starts = sorted(now - int(rng.random() * days * 86400) for _ in range(windows))
    (cache / "_windows.json").write_text(json.dumps({"seed": seed, "starts": starts}))
    for start in starts:
        result = rpc(
            url,
            "getTransactionsForAddress",
            [
                RELAYER,
                {
                    "transactionDetails": "full",
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 1,
                    "limit": per_window,
                    "sortOrder": "asc",
                    "filters": {"blockTime": {"gte": start}},
                },
            ],
        )
        for item in result["data"]:
            item.pop("transactionIndex", None)
            sig = item["transaction"]["signatures"][0]
            (cache / f"{sig}.json").write_text(json.dumps(item))
        print(f"  ventana {start}: {len(result['data'])} transacciones", file=sys.stderr)


def wilson_lower(successes: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = successes / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / (1 + z * z / n)


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
            "transferCheckedWithFee",
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


def mint_fee_configs(url: str, cache: Path, mints: set[str]) -> dict[str, dict[str, Any]]:
    """Configuración de comisión de transferencia (Token-2022) de cada mint, on-chain."""
    path = cache / "_mints.json"
    known: dict[str, dict[str, Any]] = json.loads(path.read_text()) if path.exists() else {}
    for mint in sorted(mints - known.keys()):
        value = rpc(url, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]
        info = value["data"]["parsed"]["info"] if isinstance(value["data"], dict) else {}
        fee = next(
            (
                e["state"]
                for e in info.get("extensions", [])
                if e["extension"] == "transferFeeConfig"
            ),
            None,
        )
        known[mint] = {"program": value["owner"], "transferFeeConfig": fee}
    path.write_text(json.dumps(known, indent=1))
    return known


def _fee_for(config: dict[str, Any] | None, slot: int, amount: int) -> int:
    if not config:
        return 0
    epoch = slot // 432_000
    newer = config["newerTransferFee"]
    current = newer if epoch >= int(newer["epoch"]) else config["olderTransferFee"]
    bps, cap = int(current["transferFeeBasisPoints"]), int(current["maximumFee"])
    return min(-(-amount * bps // 10_000), cap)  # redondeo hacia arriba, con tope


def method_b_fee_aware(
    tx: dict[str, Any], wallet: str, configs: dict[str, dict[str, Any]]
) -> dict[str, int]:
    """Como el método B, pero descontando de cada transferencia recibida la comisión de
    Token-2022 que fija la configuración on-chain del mint. Unidades mínimas."""
    meta = tx["meta"]
    keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
    acct: dict[str, tuple[str, str]] = {}
    for bal in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        acct[keys[bal["accountIndex"]]] = (bal["mint"], bal.get("owner", ""))
    ixs = list(tx["transaction"]["message"]["instructions"])
    for inner in meta.get("innerInstructions") or []:
        ixs += inner["instructions"]
    deltas: dict[str, int] = defaultdict(int)
    for ix in ixs:
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict) or ix.get("program") not in ("spl-token", "spl-token-2022"):
            continue
        if parsed.get("type") not in ("transfer", "transferChecked", "transferCheckedWithFee"):
            continue
        info = parsed["info"]
        src, dst = acct.get(info["source"]), acct.get(info["destination"])
        mint = info.get("mint") or (src or dst or (None, None))[0]
        if mint is None:
            continue
        amount = int(info["tokenAmount"]["amount"] if "tokenAmount" in info else info["amount"])
        if src and src[1] == wallet:
            deltas[mint] -= amount
        if dst and dst[1] == wallet:
            config = (configs.get(mint) or {}).get("transferFeeConfig")
            deltas[mint] += amount - _fee_for(config, int(tx["slot"]), amount)
    return {m: v for m, v in deltas.items() if v}


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
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--helius-windows", type=int, default=0)
    ap.add_argument("--window-days", type=float, default=7)
    ap.add_argument("--per-window", type=int, default=100)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument(
        "--explain-transfer-fees",
        action="store_true",
        help="consulta en Helius la configuración de comisión de los mints en discrepancia",
    )
    args = ap.parse_args()
    if args.fetch:
        fetch(args.rpc_url, args.cache_dir, args.fetch, args.gap, args.stride)
    if args.helius_windows:
        fetch_helius_windows(
            args.cache_dir, args.helius_windows, args.window_days, args.per_window, args.seed
        )

    relayers = frozenset({RELAYER})
    files = sorted(p for p in args.cache_dir.glob("*.json") if not p.name.startswith("_"))
    stats: Counter[str] = Counter()
    programs: Counter[str] = Counter()
    fee_pct: list[float] = []
    fee_abs: list[Decimal] = []
    disagreements: list[tuple[dict[str, Any], Any, dict[str, Decimal]]] = []
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
            # Los tokens que A declara como polvo o resto de ruta (en sus motivos) no se
            # comparan; el token principal y el USDC, sí.
            declared = {m for m in b if any(m in r for r in ev.reasons)}
            b_tokens = {m: d for m, d in b.items() if m not in QUOTE_SYMBOLS and m not in declared}
            b_usdc = b.get(USDC_MINT, Decimal(0))
            ok = (
                ev.kind in (EventKind.BUY, EventKind.SELL)
                and b_tokens == {ev.mint: ev.token_delta}
                and (ev.quote_symbol != "USDC" or b_usdc == ev.quote_delta)
            )
            stats[f"coincide_metodo_B={ok}"] += 1
            if ok and ev.confidence.value == "high":
                stats["coincide_metodo_B_confianza_alta"] += 1
            if declared:
                stats["con_polvo_o_resto_de_ruta_declarado"] += 1
            if not ok:
                disagreements.append((tx, ev, b))
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
    high = sum(v for k, v in stats.items() if k.startswith("evento=") and k.endswith("/high"))
    agreed = stats["coincide_metodo_B_confianza_alta"]
    print(f"Eventos de confianza alta: {high}; coinciden con B: {agreed}")
    if not disagreements:
        print(f"Cota inferior de Wilson (95 %) de la precisión: {wilson_lower(agreed, high):.1%}")
        return 0
    configs: dict[str, dict[str, Any]] = {}
    if args.explain_transfer_fees:
        key = os.environ.get("COPYBOT_HELIUS_API_KEY")
        if not key:
            raise SystemExit("Falta COPYBOT_HELIUS_API_KEY")
        mints = {m for _, _, b in disagreements for m in b if m not in QUOTE_SYMBOLS}
        configs = mint_fee_configs(
            f"https://mainnet.helius-rpc.com/?api-key={key}", args.cache_dir, mints
        )
    explained = 0
    explained_high = 0
    print("Discrepancias A/B:")
    for tx, ev, b in disagreements:
        line = (
            f"{tx['transaction']['signatures'][0]}: A={ev.kind.value} {ev.mint} "
            f"{ev.token_delta} {ev.quote_symbol} {ev.quote_delta} | B={b}"
        )
        if configs and ev.kind in (EventKind.BUY, EventKind.SELL):
            # Explicada si, descontando la comisión on-chain de cada transferencia, el
            # control coincide exactamente (en unidades mínimas) con lo que dice A.
            fee_b = method_b_fee_aware(tx, ev.wallet, configs)
            decimals = -ev.token_delta.as_tuple().exponent
            a_raw = int(ev.token_delta.scaleb(decimals))
            others = {m: v for m, v in fee_b.items() if m not in QUOTE_SYMBOLS and m != ev.mint}
            bps = {
                m: (configs.get(m) or {}).get("transferFeeConfig", {}) or {}
                for m in fee_b
                if m not in QUOTE_SYMBOLS
            }
            if fee_b.get(ev.mint) == a_raw and not others:
                explained += 1
                explained_high += ev.confidence.value == "high"
                rates = {
                    m[:8]: c.get("newerTransferFee", {}).get("transferFeeBasisPoints")
                    for m, c in bps.items()
                    if c
                }
                line += f"  → EXPLICADA: comisión de transferencia Token-2022 {rates} (pb)"
        print("  ", line)
    if configs:
        print(
            f"Discrepancias explicadas por la comisión on-chain: {explained} de {len(disagreements)}"
        )
    correct = agreed + explained_high
    print(
        f"Confianza alta correctas (coincidencias + explicadas): {correct} de {high}. "
        f"Cota inferior de Wilson (95 %) de la precisión: {wilson_lower(correct, high):.1%}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

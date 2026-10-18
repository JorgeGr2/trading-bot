"""Garantía de «solo simulación».

Estos tests fallan si alguien añade al código del sistema la capacidad de firmar o
enviar transacciones, o de manejar claves privadas. Eliminarlos o relajarlos exige
una decisión explícita (Fase 9) y revisión.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

FORBIDDEN_PATTERNS = [
    r"\bKeypair\b",
    r"sendTransaction",
    r"send_transaction",
    r"sendRawTransaction",
    r"send_raw_transaction",
    r"signTransaction",
    r"sign_transaction",
    r"\bsign_message\b",
    r"private_?key",
    r"secret_?key",
    r"seed_?phrase",
    r"\bmnemonic\b",
    r"requestAirdrop",
    # Endpoints de Jupiter que construyen o ejecutan transacciones de swap:
    # /swap/v1/swap, /swap/v1/swap-instructions, /v6/swap… y toda la API «Ultra»
    # (order/execute). La cotización (/swap/v1/quote) solo lee precios y está permitida.
    r"/swap-instructions",
    r"/v\d+/swap\b",
    r"/ultra/",
    r"/execute\b",
]

# Bibliotecas que permiten firmar transacciones de Solana.
FORBIDDEN_DEPENDENCIES = {"solana", "solders", "anchorpy", "solana-py", "pynacl", "ed25519"}


def test_no_signing_or_sending_code_in_src():
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_PATTERNS:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)}")
    assert not offenders, "Código de firma/envío no permitido:\n" + "\n".join(offenders)


def test_no_signing_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = project["project"]["dependencies"] + [
        dep for group in project.get("dependency-groups", {}).values() for dep in group
    ]
    names = {re.split(r"[<>=\[ ;]", dep, maxsplit=1)[0].lower() for dep in declared}
    assert not names & FORBIDDEN_DEPENDENCIES


def test_guard_detects_forbidden_code(tmp_path):
    """El propio detector funciona (evita un test que nunca falla)."""
    sample = "from solders.keypair import Keypair\nclient.send_transaction(tx)\n"
    hits = [p for p in FORBIDDEN_PATTERNS if re.search(p, sample, flags=re.IGNORECASE)]
    assert len(hits) >= 2


def _flagged(text: str) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in FORBIDDEN_PATTERNS)


def test_guard_blocks_every_swap_building_endpoint():
    for url in (
        "https://api.jup.ag/swap/v1/swap",
        "https://lite-api.jup.ag/swap/v1/swap-instructions",
        "https://quote-api.jup.ag/v6/swap",
        "https://quote-api.jup.ag/v6/swap-instructions",
        "https://api.jup.ag/ultra/v1/order",
        "https://api.jup.ag/ultra/v1/execute",
    ):
        assert _flagged(url), url


def test_guard_allows_read_only_quotes_and_prices():
    for url in (
        "https://api.jup.ag/swap/v1/quote",
        "https://lite-api.jup.ag/swap/v1/quote",
        "https://lite-api.jup.ag/price/v3",
    ):
        assert not _flagged(url), url

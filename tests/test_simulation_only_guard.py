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
    r"/swap-instructions",
    r"/swap\b",  # endpoint de Jupiter que construye transacciones de swap
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

"""Validación de direcciones Solana (claves públicas de 32 bytes en base58)."""

from __future__ import annotations

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


class InvalidAddressError(ValueError):
    """La cadena no es una dirección Solana válida."""


def b58decode(value: str) -> bytes:
    """Decodifica base58 (alfabeto de Bitcoin, el que usa Solana)."""
    if not value:
        raise InvalidAddressError("cadena vacía")
    num = 0
    for char in value:
        try:
            num = num * 58 + _INDEX[char]
        except KeyError:
            raise InvalidAddressError(f"carácter no válido en base58: {char!r}") from None
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    leading_zeros = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeros + body


def validate_address(value: str) -> str:
    """Devuelve la dirección si es válida; si no, lanza ``InvalidAddressError``.

    No comprueba que la dirección exista on-chain ni que esté en la curva ed25519:
    las direcciones de programa (PDA) también son válidas como cuentas.
    """
    if not isinstance(value, str):
        raise InvalidAddressError("la dirección debe ser texto")
    value = value.strip()
    if not 32 <= len(value) <= 44:
        raise InvalidAddressError("longitud fuera del rango de una dirección Solana")
    if len(b58decode(value)) != 32:
        raise InvalidAddressError("no decodifica a 32 bytes")
    return value


def is_valid_address(value: str) -> bool:
    try:
        validate_address(value)
    except InvalidAddressError:
        return False
    return True

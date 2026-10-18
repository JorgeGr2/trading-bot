from __future__ import annotations

import pytest

from copybot.solana.addresses import InvalidAddressError, is_valid_address, validate_address


@pytest.mark.parametrize(
    "address",
    [
        "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51",
        "11111111111111111111111111111111",  # programa del sistema (32 bytes a cero)
        "So11111111111111111111111111111111111111112",
        "7kASdbzxWLdioHhzYvXwtM3tRztAbFmeLKF4bDW8pump",
    ],
)
def test_valid_addresses(address):
    assert validate_address(address) == address


@pytest.mark.parametrize(
    "address",
    [
        "",
        "abc",
        "0OIl" * 11,  # caracteres fuera del alfabeto base58
        "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51AgmL",  # demasiado larga
        "1111111111111111111111111111111111111111111",  # 43 ceros: no son 32 bytes
        "'; DROP TABLE wallets; --",
        "0x52908400098527886E0F7030069857D2E4169EE7",  # dirección EVM
    ],
)
def test_invalid_addresses(address):
    with pytest.raises(InvalidAddressError):
        validate_address(address)
    assert not is_valid_address(address)


def test_surrounding_whitespace_is_stripped():
    assert validate_address("  So11111111111111111111111111111111111111112\n") == (
        "So11111111111111111111111111111111111111112"
    )

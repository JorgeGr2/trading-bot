from __future__ import annotations

from datetime import UTC, datetime

import pytest

from copybot.traders_cli import parse_as_of

NOW = datetime(2026, 10, 18, 18, 0, 0, 123456, tzinfo=UTC)


def test_as_of_defaults_to_now_without_microseconds():
    assert parse_as_of(None, NOW) == NOW.replace(microsecond=0)


def test_as_of_without_zone_is_utc():
    assert parse_as_of("2026-10-18T17:00:00", NOW) == datetime(2026, 10, 18, 17, tzinfo=UTC)
    assert parse_as_of("2026-10-18T19:00:00+02:00", NOW) == datetime(2026, 10, 18, 17, tzinfo=UTC)


def test_future_as_of_is_rejected():
    with pytest.raises(SystemExit, match="posterior a ahora"):
        parse_as_of("2026-10-18T18:10:00", NOW)


def test_activity_band_parsing():
    import argparse
    from decimal import Decimal

    from copybot.traders_cli import parse_band

    assert parse_band("0.7:15") == (Decimal("0.7"), Decimal(15))
    for bad in ("15:0.7", "abc", "1", "-1:2"):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_band(bad)


def test_wallet_file(tmp_path):
    from copybot.solana.addresses import InvalidAddressError
    from copybot.traders_cli import read_wallet_file

    good = tmp_path / "w.txt"
    good.write_text(
        "# leaderboard 30 días\n"
        "7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm, @uno puesto 1\n\n"
        "Gvg5vebyNUYgDP6U5jJLS3wAgbkhgJ19xHVpBLHtmGSy\n"
    )
    assert read_wallet_file(str(good)) == [
        ("7ab7MBy948MFi6NbRzcdoAFVU7sMycMzAo1t3RyCwFtm", "@uno puesto 1"),
        ("Gvg5vebyNUYgDP6U5jJLS3wAgbkhgJ19xHVpBLHtmGSy", None),
    ]
    bad = tmp_path / "bad.txt"
    bad.write_text("@usuario_sin_direccion\n")
    with pytest.raises(InvalidAddressError):
        read_wallet_file(str(bad))

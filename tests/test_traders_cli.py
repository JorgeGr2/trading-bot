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

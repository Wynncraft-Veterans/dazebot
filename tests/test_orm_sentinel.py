"""Tests for the UNKNOWN_LAST_ONLINE sentinel and is_last_online_unknown.

These helpers guard the "this player has hidden lastJoin via Wynncraft
privacy settings" branch across the codebase. The 24h tolerance window
around the Unix epoch is the part most likely to regress.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orm import UNKNOWN_LAST_ONLINE, is_last_online_unknown


def test_none_is_unknown():
    assert is_last_online_unknown(None) is True


def test_sentinel_itself_is_unknown():
    assert is_last_online_unknown(UNKNOWN_LAST_ONLINE) is True


def test_one_second_after_epoch_is_unknown():
    dt = UNKNOWN_LAST_ONLINE + timedelta(seconds=1)
    assert is_last_online_unknown(dt) is True


def test_exactly_24h_after_epoch_is_unknown_boundary():
    # Boundary case: `<=` includes the 86400-second mark.
    dt = datetime.fromtimestamp(86400, tz=timezone.utc)
    assert is_last_online_unknown(dt) is True


def test_24h_plus_one_second_after_epoch_is_known():
    dt = datetime.fromtimestamp(86401, tz=timezone.utc)
    assert is_last_online_unknown(dt) is False


def test_recent_timestamp_is_known():
    dt = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    assert is_last_online_unknown(dt) is False

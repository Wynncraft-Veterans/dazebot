"""Tests for vanity-date parsing and vanity-role cutoff lookup.

* ``parse_vanity_date`` is the input parser for ``/vanity <date>``. The
  three accepted shapes (year, year-month, year-month-day) and the
  separator alternatives (``-`` / ``/``) come straight from the docstring.
* ``get_vanity_role_id`` walks ``VanityRolesConfig.CUTOFFS`` and returns
  the first role whose cutoff is *after* the supplied timestamp. The walk
  order matters; a regression could return the wrong era's role.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from lib.discord_utils.vanity_roles import get_vanity_role_id
from lib.mc.resolve import parse_vanity_date


# ---------------------------------------------------------------------------
# parse_vanity_date
# ---------------------------------------------------------------------------


def test_parse_year_only_defaults_to_jan_1():
    assert parse_vanity_date("2014") == date(2014, 1, 1)


def test_parse_year_month_defaults_to_day_1():
    assert parse_vanity_date("2014-03") == date(2014, 3, 1)


def test_parse_full_ymd():
    assert parse_vanity_date("2014-03-12") == date(2014, 3, 12)


def test_parse_accepts_slash_separator():
    assert parse_vanity_date("2014/3/12") == date(2014, 3, 12)


def test_parse_strips_surrounding_whitespace():
    assert parse_vanity_date("  2014-03-12  ") == date(2014, 3, 12)


def test_parse_single_digit_month_and_day():
    assert parse_vanity_date("2014-3-5") == date(2014, 3, 5)


def test_parse_rejects_garbage_with_user_facing_message():
    with pytest.raises(ValueError, match="Could not parse date"):
        parse_vanity_date("not-a-date")


def test_parse_rejects_partial_garbage():
    with pytest.raises(ValueError):
        parse_vanity_date("20")


def test_parse_rejects_out_of_range_month():
    # Regex accepts the shape, but date() rejects month 13.
    with pytest.raises(ValueError):
        parse_vanity_date("2014-13-01")


# ---------------------------------------------------------------------------
# get_vanity_role_id
# ---------------------------------------------------------------------------


class _FakeVanityConfig:
    """Minimal stand-in for ``Config.VanityRolesConfig``. The function only
    reads ``config.VanityRolesConfig.CUTOFFS``."""

    class VanityRolesConfig:
        CUTOFFS = [
            (datetime(2014, 1, 1, tzinfo=timezone.utc), "role_pre_2014"),
            (datetime(2016, 1, 1, tzinfo=timezone.utc), "role_2014_2015"),
            (datetime(2020, 1, 1, tzinfo=timezone.utc), "role_2016_2019"),
        ]


def test_vanity_role_returns_first_bucket_for_oldest_date():
    ts = datetime(2013, 6, 1, tzinfo=timezone.utc)
    assert get_vanity_role_id(ts, _FakeVanityConfig) == "role_pre_2014"


def test_vanity_role_returns_middle_bucket():
    ts = datetime(2015, 3, 14, tzinfo=timezone.utc)
    assert get_vanity_role_id(ts, _FakeVanityConfig) == "role_2014_2015"


def test_vanity_role_returns_last_bucket_just_before_final_cutoff():
    ts = datetime(2019, 12, 31, tzinfo=timezone.utc)
    assert get_vanity_role_id(ts, _FakeVanityConfig) == "role_2016_2019"


def test_vanity_role_after_last_cutoff_returns_none():
    """Dates past the final cutoff are too recent to qualify for any
    vanity role — the function returns ``None`` and ``/vanity`` becomes a
    no-op."""
    ts = datetime(2025, 6, 1, tzinfo=timezone.utc)
    assert get_vanity_role_id(ts, _FakeVanityConfig) is None


def test_vanity_role_at_exact_cutoff_returns_next_bucket():
    """Cutoffs are exclusive on the lower side (``<``, not ``<=``) — a
    timestamp exactly at a cutoff belongs to the *next* bucket."""
    ts = datetime(2014, 1, 1, tzinfo=timezone.utc)
    # Not strictly less than 2014-01-01, so first bucket is skipped.
    assert get_vanity_role_id(ts, _FakeVanityConfig) == "role_2014_2015"

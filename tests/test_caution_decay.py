"""Tests for the caution-point decay logic in
``lib/staff/staff_actions.py``.

The replay function is pure: feed it a chronological list of entries
plus a ``now`` and assert on the resulting live count, next-expiry, and
per-entry breakdown. We never touch the DB here -- a ``SimpleNamespace``
duck-types as a ``StaffActionEntry`` for ``_replay``'s purposes
(``created_at`` + ``points`` are the only attributes read).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from lib.staff.staff_actions import _expiry_days, _replay


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _entry(offset_days: float, points: int) -> SimpleNamespace:
    """A throwaway stand-in for ``StaffActionEntry``; the replay
    function only reads ``created_at`` and ``points``.
    """
    return SimpleNamespace(
        created_at=T0 + timedelta(days=offset_days),
        points=points,
    )


# ---------------------------------------------------------------------------
# Formula
# ---------------------------------------------------------------------------


def test_expiry_days_first_point_is_21():
    assert _expiry_days(1) == pytest.approx(21.0)


def test_expiry_days_fifth_point_is_182():
    # y(5) = 21 * (26/3)^1 = 21 * 26/3 = 182 exactly.
    assert _expiry_days(5) == pytest.approx(182.0)


def test_expiry_days_is_strictly_increasing():
    prev = 0.0
    for x in range(1, 11):
        cur = _expiry_days(x)
        assert cur > prev, f"y({x})={cur} not > y({x - 1})={prev}"
        prev = cur


def test_expiry_days_third_point():
    # y(3) = 21 * (26/3)^(1/2) = 21 * sqrt(26/3) ~= 21 * 2.944 ~= 61.82
    assert _expiry_days(3) == pytest.approx(61.82, abs=0.02)


# ---------------------------------------------------------------------------
# Replay: single-entry shapes
# ---------------------------------------------------------------------------


def test_replay_empty_history_is_zero():
    result = _replay([], T0)
    assert result.live_count == 0
    assert result.next_expiry is None
    assert result.per_entry_live == ()


def test_replay_one_caution_just_before_expiry():
    entries = [_entry(0, 1)]
    now = T0 + timedelta(days=20)
    result = _replay(entries, now)
    assert result.live_count == 1
    assert result.per_entry_live == (1,)
    assert result.next_expiry == T0 + timedelta(days=21)


def test_replay_one_caution_just_after_expiry():
    entries = [_entry(0, 1)]
    now = T0 + timedelta(days=21, seconds=1)
    result = _replay(entries, now)
    assert result.live_count == 0
    assert result.per_entry_live == (0,)
    assert result.next_expiry is None


def test_replay_warning_three_points_partial_decay():
    # Warning (+3) at T0, all three live at +20d.
    entries = [_entry(0, 3)]
    just_before = _replay(entries, T0 + timedelta(days=20))
    assert just_before.live_count == 3
    assert just_before.per_entry_live == (3,)

    # First point dies at +21d (ordinal 1), still two live.
    after_first = _replay(entries, T0 + timedelta(days=22))
    assert after_first.live_count == 2
    assert after_first.per_entry_live == (2,)

    # Second point dies at +36d (ordinal 2), one left.
    after_second = _replay(entries, T0 + timedelta(days=40))
    assert after_second.live_count == 1
    assert after_second.per_entry_live == (1,)

    # Third dies at +62d, fully expired.
    after_third = _replay(entries, T0 + timedelta(days=70))
    assert after_third.live_count == 0
    assert after_third.per_entry_live == (0,)


def test_replay_five_same_day_points_longest_lives_182_days():
    # Five points issued at T0 expire at y(1..5)d after T0. The longest-
    # lived is the 5th point at y(5)=182d -- after that they're all gone.
    entries = [_entry(0, 5)]
    at_180 = _replay(entries, T0 + timedelta(days=180))
    at_183 = _replay(entries, T0 + timedelta(days=183))
    assert at_180.live_count == 1  # only y(5)=182d point still alive
    assert at_183.live_count == 0


# ---------------------------------------------------------------------------
# Replay: ordinal-at-issue depends on LIVE count, not lifetime count
# ---------------------------------------------------------------------------


def test_replay_caution_after_first_expired_resets_ordinal():
    # Caution at T0 (ordinal 1, expires +21d). 30 days later, caution
    # again -- the first is gone, so the new point also takes ordinal 1
    # (expires +21d from its own created_at, i.e. T0+30d+21d = T0+51d).
    entries = [_entry(0, 1), _entry(30, 1)]
    now = T0 + timedelta(days=35)
    result = _replay(entries, now)
    assert result.live_count == 1
    # Only the second entry contributes:
    assert result.per_entry_live == (0, 1)
    assert result.next_expiry == T0 + timedelta(days=30) + timedelta(days=21)


def test_replay_two_back_to_back_cautions_get_ordinals_1_and_2():
    # Same-day cautions: the second takes ordinal 2 (slower expiry).
    entries = [_entry(0, 1), _entry(0, 1)]
    # At +22d the first is gone but the second (y(2) ~= 36d) is alive.
    result = _replay(entries, T0 + timedelta(days=22))
    assert result.live_count == 1
    assert result.per_entry_live == (0, 1)


# ---------------------------------------------------------------------------
# Replay: adjustments
# ---------------------------------------------------------------------------


def test_replay_negative_adjustment_retires_oldest_first():
    # Warning (+3) at T0 -> three live with expiries y(1), y(2), y(3).
    # Then adjustment -2 immediately after -> 1 live remains, and it's
    # the *latest* ordinal (the one with the largest expiry, y(3)).
    entries = [_entry(0, 3), _entry(0, -2)]
    result = _replay(entries, T0 + timedelta(days=1))
    assert result.live_count == 1
    # The warning contributes 1 point still; the adjustment contributes 0.
    assert result.per_entry_live == (1, 0)
    # Remaining point has the y(3) expiry.
    assert result.next_expiry == T0 + timedelta(days=_expiry_days(3))


def test_replay_negative_adjustment_with_excess_clamps_at_zero():
    entries = [_entry(0, 1), _entry(0, -10)]
    result = _replay(entries, T0 + timedelta(days=1))
    assert result.live_count == 0
    assert result.per_entry_live == (0, 0)


def test_replay_positive_adjustment_uses_current_live_ordinals():
    # Caution at T0, then adjustment +2 at T0+10 -> the new points take
    # ordinals 2 and 3 (because the first caution is still live).
    entries = [_entry(0, 1), _entry(10, 2)]
    # At T0+30: first caution (y(1)=21d after T0) is gone.
    #   Second entry's points were issued at T0+10 with ordinals 2 and 3
    #   relative to live-at-issue, so they expire at T0+10+y(2)~=46d and
    #   T0+10+y(3)~=71.8d respectively. Both still live at T0+30d.
    result = _replay(entries, T0 + timedelta(days=30))
    assert result.live_count == 2
    assert result.per_entry_live == (0, 2)


def test_replay_warnings_set_to_zero_clears_history():
    # Five same-day points then -5 adjustment empties live count.
    entries = [_entry(0, 5), _entry(0, -5)]
    result = _replay(entries, T0 + timedelta(days=1))
    assert result.live_count == 0
    assert result.per_entry_live == (0, 0)


# ---------------------------------------------------------------------------
# Replay: tz-naive datetimes (sqlite + tortoise can return them)
# ---------------------------------------------------------------------------


def test_replay_tolerates_tz_naive_created_at():
    naive = datetime(2026, 1, 1)
    entry = SimpleNamespace(created_at=naive, points=1)
    result = _replay([entry], datetime(2026, 1, 10, tzinfo=timezone.utc))
    assert result.live_count == 1

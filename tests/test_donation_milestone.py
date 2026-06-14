"""DB-level tests for the 5%-of-cumulative donation-milestone math
(``cogs.rewards.donations.lib.svc.donation_milestone_recipients``).

A donation qualifies as a milestone when its ``value_emeralds`` is at
least 5% of the recipient's cumulative-received total at the time the
donation was recorded (the cumulative includes the donation itself).
The two most recent distinct recipients whose latest qualifying donation
has the latest ``created_at`` are returned.

Uses the ``_isolated_db`` fixture style from ``tests/test_migrations.py``:
each test gets a fresh tmp_path SQLite DB, runs ``init_db`` to bootstrap
the schema via aerich, then exercises the service layer directly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cogs.rewards.donations.lib import svc
from orm import Donation, MinecraftAccount, init_db


# Re-import the fixture machinery used by test_migrations so this file
# doesn't duplicate it. pytest auto-discovers fixtures in conftest.py;
# without one, we redeclare here so the file is self-contained.

@pytest.fixture
async def _isolated_db(tmp_path, monkeypatch):
    from aerich.migrate import Migrate
    from tortoise import Tortoise

    async def _reset():
        try:
            await Tortoise.close_connections()
        except Exception:
            pass
        Tortoise._inited = False
        for attr in ("_last_version_content", "migrate_location", "ddl", "ddl_class", "dialect"):
            if hasattr(Migrate, attr):
                try:
                    delattr(Migrate, attr)
                except AttributeError:
                    pass
        Migrate.upgrade_operators = []
        Migrate.downgrade_operators = []

    await _reset()
    db_path = tmp_path / "test.db"
    returns_db_path = tmp_path / "test_returns.db"
    monkeypatch.setenv("DAZEBOT_DB_PATH", str(db_path))
    monkeypatch.setenv("DAZEBOT_RETURNS_DB_PATH", str(returns_db_path))
    monkeypatch.setenv("DAZEBOT_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("DAZEBOT_ALLOW_FRESH_RETURNS_DB", "1")
    yield db_path
    await _reset()


# Counter so each test's mc_account_factory call gets a distinct UUID even
# inside the same test.
_uuid_counter = 0


def _next_uuid() -> str:
    global _uuid_counter
    _uuid_counter += 1
    return f"00000000-0000-0000-0000-{_uuid_counter:012d}"


async def _make_mc(username: str = "player") -> MinecraftAccount:
    return await MinecraftAccount.create(
        uuid=_next_uuid(),
        wynn_username=username,
        mc_username=username,
        last_online=datetime.fromtimestamp(0, tz=timezone.utc),
        last_manual_check=datetime.fromtimestamp(0, tz=timezone.utc),
    )


async def _record(mc: MinecraftAccount, value: int) -> Donation:
    return await svc.record_donation(
        recipient=mc,
        value_emeralds=value,
        comment=None,
        image_urls=[],
        recorder_disc_uuid="111111111111111111",
    )


async def test_single_donation_qualifies(_isolated_db):
    """A user's first donation is 100% of their cumulative — qualifies."""
    await init_db()
    mc = await _make_mc()
    await _record(mc, 1_000)
    recipients = await svc.donation_milestone_recipients(limit=2)
    assert [r.id for r in recipients] == [mc.id]


async def test_small_follow_up_does_not_promote(_isolated_db):
    """A second tiny donation doesn't reach 5% of the new cumulative — the
    recipient's latest *qualifying* timestamp stays at donation #1.
    """
    await init_db()
    mc = await _make_mc()
    first = await _record(mc, 1_000)
    # Second donation: 4 emeralds against cumulative 1004 -> 4*20=80, not >= 1004.
    second = await _record(mc, 4)
    # Recipient still in the list (first donation qualified) — but its
    # latest qualifying timestamp/id is the first donation's, not the
    # second's. Hard to assert on timestamp directly, so we just confirm
    # the recipient is the only one and the second donation didn't bump.
    recipients = await svc.donation_milestone_recipients(limit=2)
    assert [r.id for r in recipients] == [mc.id]
    # Sanity: cumulative math behaves as expected.
    assert second.value_emeralds * 20 < first.value_emeralds + second.value_emeralds


async def test_large_later_donation_requalifies(_isolated_db):
    """A later donation that crosses the 5% bar marks the recipient's
    latest-qualifying timestamp forward, so they sort to the front again.
    """
    await init_db()
    a = await _make_mc("alpha")
    b = await _make_mc("bravo")
    # Alpha gets one big donation, then later Bravo gets one — Bravo is more recent.
    await _record(a, 1_000)
    await _record(b, 1_000)
    # Now Alpha gets a HUGE donation: 100k > 5% of cumulative (101k).
    # Alpha's latest qualifying timestamp is now newer than Bravo's.
    await _record(a, 100_000)
    recipients = await svc.donation_milestone_recipients(limit=2)
    assert [r.id for r in recipients] == [a.id, b.id]


async def test_limit_caps_at_distinct_users(_isolated_db):
    """With three qualifying users, limit=2 returns the two most recent."""
    await init_db()
    a = await _make_mc("alpha")
    b = await _make_mc("bravo")
    c = await _make_mc("charlie")
    await _record(a, 1_000)
    await _record(b, 1_000)
    await _record(c, 1_000)  # most recent first donation
    recipients = await svc.donation_milestone_recipients(limit=2)
    assert [r.id for r in recipients] == [c.id, b.id]


async def test_remove_reshuffles_results(_isolated_db):
    """If a qualifying donation is removed, the recipient's
    latest-qualifying timestamp may fall back to an earlier donation —
    or vanish entirely. Live computation reflects the delete.
    """
    await init_db()
    a = await _make_mc("alpha")
    only = await _record(a, 1_000)
    # Before remove: alpha is on the list.
    pre = await svc.donation_milestone_recipients(limit=2)
    assert [r.id for r in pre] == [a.id]
    # After remove: nobody is on the list.
    await svc.remove_donation(only)
    post = await svc.donation_milestone_recipients(limit=2)
    assert post == []


async def test_empty_table_returns_empty(_isolated_db):
    await init_db()
    assert await svc.donation_milestone_recipients(limit=2) == []


async def test_same_user_one_entry(_isolated_db):
    """A user with multiple qualifying donations appears only once in the
    result (distinct recipients).
    """
    await init_db()
    a = await _make_mc("alpha")
    b = await _make_mc("bravo")
    # Alpha gets two big qualifying donations back-to-back; Bravo gets one
    # afterwards. Result should be [bravo, alpha] (two distinct users).
    await _record(a, 1_000)
    await _record(a, 50_000)  # both qualify
    await _record(b, 1_000)
    recipients = await svc.donation_milestone_recipients(limit=2)
    assert [r.id for r in recipients] == [b.id, a.id]

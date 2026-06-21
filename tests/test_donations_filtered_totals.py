"""DB-level tests for
``cogs.rewards.donations.lib.svc.leaderboard_totals_for_mc_ids`` — the
mc_id-restricted variant of ``leaderboard_totals`` used by
``/api/internal/glinted`` slot 6 ("top cumulative donor among
currently-online Returners").

Fixture style mirrors ``tests/test_donation_milestone.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cogs.rewards.donations.lib import svc
from orm import Donation, MinecraftAccount, init_db


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


async def test_empty_mc_ids_returns_empty(_isolated_db):
    """Empty set must not degenerate into "all recipients" — slot 6
    asks "of these online accounts, who has received the most" and an
    empty online set means "nobody is online", not "show everyone"."""
    await init_db()
    a = await _make_mc("alpha")
    await _record(a, 1_000)
    assert await svc.leaderboard_totals_for_mc_ids(set()) == []


async def test_single_recipient_sums_donations(_isolated_db):
    await init_db()
    a = await _make_mc("alpha")
    await _record(a, 100)
    await _record(a, 250)
    await _record(a, 50)
    result = await svc.leaderboard_totals_for_mc_ids({a.id})
    assert len(result) == 1
    mc, total = result[0]
    assert mc.id == a.id
    assert total == 400


async def test_ordered_by_total_desc(_isolated_db):
    await init_db()
    a = await _make_mc("alpha")
    b = await _make_mc("bravo")
    c = await _make_mc("charlie")
    await _record(a, 100)
    await _record(b, 500)
    await _record(c, 300)
    result = await svc.leaderboard_totals_for_mc_ids({a.id, b.id, c.id})
    assert [mc.id for mc, _t in result] == [b.id, c.id, a.id]
    assert [t for _mc, t in result] == [500, 300, 100]


async def test_excludes_non_matching_mc_ids(_isolated_db):
    """Recipients outside the supplied id set are dropped, even if they
    have larger totals than recipients in the set."""
    await init_db()
    a = await _make_mc("alpha")
    b = await _make_mc("bravo")
    c = await _make_mc("charlie")
    d = await _make_mc("delta")
    e = await _make_mc("echo")
    await _record(a, 10_000)  # outside set — must be excluded despite being biggest
    await _record(b, 100)
    await _record(c, 9_000)   # outside set
    await _record(d, 200)
    await _record(e, 50)      # outside set
    result = await svc.leaderboard_totals_for_mc_ids({b.id, d.id})
    assert [mc.id for mc, _t in result] == [d.id, b.id]
    assert all(mc.id in {b.id, d.id} for mc, _t in result)

"""DB-level tests for
``cogs.rewards.donations.lib.svc.top_eligible_donors`` — the eligibility-
filtered, N-capped variant of ``leaderboard_totals`` that feeds the new
``/api/internal/donor_candidates`` endpoint (vetsmod's client-side
"instant slot 6" pool).

Eligibility (MEMBER / WAITLISTED / HONOURARY) is resolved via
``is_eligible_member(discord.Member)`` against a live Discord guild; the
test monkeypatches both the guild lookup and the eligibility check so the
helper can run without a real bot.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cogs.rewards.donations.lib import svc
from orm import DiscordAccount, Donation, MinecraftAccount, init_db


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


async def _make_disc(disc_uuid: str, mc: MinecraftAccount) -> DiscordAccount:
    return await DiscordAccount.create(
        disc_uuid=disc_uuid,
        minecraft_account=mc,
    )


async def _record(mc: MinecraftAccount, value: int) -> Donation:
    return await svc.record_donation(
        recipient=mc,
        value_emeralds=value,
        comment=None,
        image_urls=[],
        recorder_disc_uuid="111111111111111111",
    )


class _FakeMember:
    """Minimal stand-in for ``discord.Member`` — only needs a stable
    identifier the patched eligibility check can key off."""
    def __init__(self, disc_uuid: str):
        self.disc_uuid_str = disc_uuid


class _FakeGuild:
    def get_member(self, did_int):
        return _FakeMember(str(did_int))


class _FakeBot:
    def get_guild(self, _gid):
        return _FakeGuild()


def _patch_eligibility(monkeypatch, eligible_disc_uuids: set):
    """Replace ``is_eligible_member`` with one that consults the set."""
    def _fake(member):
        return getattr(member, "disc_uuid_str", None) in eligible_disc_uuids
    monkeypatch.setattr(
        "cogs.rewards.ctp.lib.glints.is_eligible_member", _fake
    )


async def test_empty_donations_returns_empty(_isolated_db, monkeypatch):
    await init_db()
    _patch_eligibility(monkeypatch, set())
    result = await svc.top_eligible_donors(_FakeBot(), limit=20)
    assert result == []


async def test_limit_cap_respected(_isolated_db, monkeypatch):
    """With 25 eligible donors and limit=20, exactly 20 are returned."""
    await init_db()
    eligible = set()
    for i in range(25):
        mc = await _make_mc(f"player{i:02d}")
        disc_uuid = str(2000 + i)
        await _make_disc(disc_uuid, mc)
        eligible.add(disc_uuid)
        # Each gets a distinct donation amount so ordering is deterministic.
        await _record(mc, 1_000 + i * 10)
    _patch_eligibility(monkeypatch, eligible)
    result = await svc.top_eligible_donors(_FakeBot(), limit=20)
    assert len(result) == 20


async def test_eligibility_filter_excludes_non_members(_isolated_db, monkeypatch):
    """Ineligible recipients are skipped even if they have higher totals.
    The result must include exactly the eligible recipients."""
    await init_db()
    mcs = []
    discs_uuids = []
    for i in range(5):
        mc = await _make_mc(f"player{i}")
        disc_uuid = str(3000 + i)
        await _make_disc(disc_uuid, mc)
        mcs.append(mc)
        discs_uuids.append(disc_uuid)
    # player0 = 500 (highest), player1 = 400, ..., player4 = 100 (lowest).
    for i, mc in enumerate(mcs):
        await _record(mc, 500 - i * 100)
    # Only player1 and player3 are eligible.
    eligible = {discs_uuids[1], discs_uuids[3]}
    _patch_eligibility(monkeypatch, eligible)
    result = await svc.top_eligible_donors(_FakeBot(), limit=20)
    # player1 (400) > player3 (200): order preserved.
    assert [mc.uuid for mc, _t in result] == [mcs[1].uuid, mcs[3].uuid]


async def test_ordered_by_total_desc(_isolated_db, monkeypatch):
    """All-eligible case: top-down by cumulative donation total."""
    await init_db()
    a = await _make_mc("alpha")
    b = await _make_mc("bravo")
    c = await _make_mc("charlie")
    a_disc = await _make_disc("4001", a)
    b_disc = await _make_disc("4002", b)
    c_disc = await _make_disc("4003", c)
    await _record(a, 100)
    await _record(b, 500)
    await _record(c, 300)
    _patch_eligibility(monkeypatch, {a_disc.disc_uuid, b_disc.disc_uuid, c_disc.disc_uuid})
    result = await svc.top_eligible_donors(_FakeBot(), limit=20)
    assert [mc.uuid for mc, _t in result] == [b.uuid, c.uuid, a.uuid]
    assert [t for _mc, t in result] == [500, 300, 100]


async def test_no_guild_returns_empty(_isolated_db, monkeypatch):
    """If ``bot.get_guild()`` returns None (bot not in the guild),
    every candidate is skipped — defensive behaviour mirrors the
    ``/api/internal/glinted`` slot 7-8 fallback."""
    await init_db()
    a = await _make_mc("alpha")
    await _make_disc("5001", a)
    await _record(a, 1_000)

    class _NoGuildBot:
        def get_guild(self, _gid):
            return None
    _patch_eligibility(monkeypatch, {"5001"})
    result = await svc.top_eligible_donors(_NoGuildBot(), limit=20)
    assert result == []

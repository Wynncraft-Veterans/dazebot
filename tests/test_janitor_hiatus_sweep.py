"""Tests for janitor reconciler (H), ``reconcile_hiatus_guild``.

Two things are under test, and the second matters as much as the first:

* **Correctness** — a HIATUS-role holder who is in a guild gets the
  transition they're owed (MEMBER if they're back in Returners,
  REGISTERED if they're in someone else's guild), and a genuinely
  guildless holder is left alone.
* **Rate budget** — dazebot shares a single Wynncraft API token, so the
  sweep's request count is a hard design constraint, not an
  implementation detail: exactly one roster call per tick regardless of
  cohort size, at most ``JANITOR_HIATUS_SWEEP_MAX`` player calls, all at
  background priority, and a rotating cursor so the cohort is still
  covered in full across ticks.

Every upstream is patched; nothing here touches the network or the ORM.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import cogs.maintenance.janitor as jan
from lib.role_state import Trigger


def _roster(*uuids: str):
    """A ``get_guild`` return whose ``.members.all_members()`` yields these."""
    g = MagicMock()
    g.members.all_members.return_value = [SimpleNamespace(uuid=u) for u in uuids]
    return g


def _player(guild_name: str | None):
    return SimpleNamespace(guild=SimpleNamespace(name=guild_name) if guild_name else None)


@pytest.fixture(autouse=True)
def _reset_cursor():
    jan._HIATUS_SWEEP_CURSOR = 0
    yield
    jan._HIATUS_SWEEP_CURSOR = 0


@pytest.fixture
def env(monkeypatch):
    """Patch the four seams (H) touches and hand back the spies."""
    hiatus = AsyncMock(return_value=set())
    guild = AsyncMock(return_value=_roster())
    player = AsyncMock(return_value=_player(None))
    fire = AsyncMock()
    acct = SimpleNamespace(uuid="x", mc_username="someone", guild=None, save=AsyncMock())
    monkeypatch.setattr(jan, "hiatus_member_uuids", hiatus)
    monkeypatch.setattr(jan, "get_guild", guild)
    monkeypatch.setattr(jan, "get_player_stats", player)
    monkeypatch.setattr(jan, "fire_trigger_for_mc_uuids", fire)
    monkeypatch.setattr(jan.MinecraftAccount, "get_or_none", AsyncMock(return_value=acct))
    monkeypatch.setattr(jan.CurrConfig, "JANITOR_HIATUS_SWEEP_MAX", 15, raising=False)
    return SimpleNamespace(hiatus=hiatus, guild=guild, player=player, fire=fire, acct=acct)


# ---------------------------------------------------------------------------
# correctness
# ---------------------------------------------------------------------------


async def test_no_hiatus_holders_spends_nothing(env):
    r = await jan.reconcile_hiatus_guild(MagicMock(), enforce=True)
    assert (r.scanned, r.flagged, r.repaired) == (0, 0, 0)
    env.guild.assert_not_awaited()
    env.player.assert_not_awaited()


async def test_holder_on_returners_roster_is_free_and_becomes_member(env):
    env.hiatus.return_value = {"u1"}
    env.guild.return_value = _roster("u1", "other")
    r = await jan.reconcile_hiatus_guild(MagicMock(), enforce=True)
    assert (r.flagged, r.repaired) == (1, 1)
    # Resolved by set intersection — no per-player request at all.
    env.player.assert_not_awaited()
    assert env.fire.await_args.args[2] is Trigger.JOINED_VETS


async def test_guildless_holder_is_left_alone(env):
    env.hiatus.return_value = {"u1"}
    env.player.return_value = _player(None)
    r = await jan.reconcile_hiatus_guild(MagicMock(), enforce=True)
    assert (r.flagged, r.repaired) == (0, 0)
    env.fire.assert_not_awaited()


async def test_holder_in_other_guild_is_demoted_and_guild_column_written(env):
    env.hiatus.return_value = {"u1"}
    env.player.return_value = _player("KongoBoys")
    r = await jan.reconcile_hiatus_guild(MagicMock(), enforce=True)
    assert (r.flagged, r.repaired) == (1, 1)
    assert env.acct.guild == "KongoBoys"
    env.acct.save.assert_awaited_once()
    assert env.fire.await_args.args[2] is Trigger.JOINED_OTHER_GUILD


async def test_stale_returners_from_player_endpoint_is_ignored(env):
    # Roster is authoritative and says they're not in Returners, so a
    # "Returners" from the ~12h-stale player endpoint must not count.
    env.hiatus.return_value = {"u1"}
    env.guild.return_value = _roster("someone-else")
    env.player.return_value = _player("Returners")
    r = await jan.reconcile_hiatus_guild(MagicMock(), enforce=True)
    assert (r.flagged, r.repaired) == (0, 0)
    env.fire.assert_not_awaited()


async def test_log_only_mode_flags_without_mutating(env):
    env.hiatus.return_value = {"u1"}
    env.player.return_value = _player("Pavilion")
    r = await jan.reconcile_hiatus_guild(MagicMock(), enforce=False)
    assert (r.flagged, r.repaired) == (1, 0)
    env.fire.assert_not_awaited()
    env.acct.save.assert_not_awaited()
    assert "would fix" in r.samples[0]


async def test_roster_failure_skips_the_tick_without_player_calls(env):
    env.hiatus.return_value = {"u1", "u2"}
    env.guild.side_effect = RuntimeError("wapi down")
    r = await jan.reconcile_hiatus_guild(MagicMock(), enforce=True)
    assert r.errors == 1
    env.player.assert_not_awaited()


async def test_one_bad_lookup_does_not_abort_the_sweep(env):
    env.hiatus.return_value = {"u1", "u2"}
    env.player.side_effect = [RuntimeError("boom"), _player("Eden")]
    r = await jan.reconcile_hiatus_guild(MagicMock(), enforce=True)
    assert r.errors == 1 and r.flagged == 1 and r.repaired == 1


# ---------------------------------------------------------------------------
# rate budget
# ---------------------------------------------------------------------------


async def test_one_roster_call_regardless_of_cohort_size(env):
    env.hiatus.return_value = {f"u{i}" for i in range(60)}
    await jan.reconcile_hiatus_guild(MagicMock(), enforce=True)
    env.guild.assert_awaited_once()


async def test_player_calls_are_capped_and_backgrounded(env, monkeypatch):
    monkeypatch.setattr(jan.CurrConfig, "JANITOR_HIATUS_SWEEP_MAX", 5, raising=False)
    env.hiatus.return_value = {f"u{i:02d}" for i in range(40)}
    await jan.reconcile_hiatus_guild(MagicMock(), enforce=True)
    assert env.player.await_count == 5
    assert all(c.kwargs.get("background") is True for c in env.player.await_args_list)


async def test_cursor_rotates_across_ticks_and_wraps(env, monkeypatch):
    monkeypatch.setattr(jan.CurrConfig, "JANITOR_HIATUS_SWEEP_MAX", 4, raising=False)
    env.hiatus.return_value = {f"u{i:02d}" for i in range(10)}

    async def _tick() -> list[str]:
        env.player.reset_mock()
        await jan.reconcile_hiatus_guild(MagicMock(), enforce=True)
        return [c.args[0] for c in env.player.await_args_list]

    first, second, third = await _tick(), await _tick(), await _tick()
    assert len(first) == len(second) == len(third) == 4
    # Consecutive ticks must not re-poll the same accounts...
    assert not set(first) & set(second)
    # ...and three ticks of 4 over a cohort of 10 wrap around to the start.
    assert len(set(first) | set(second) | set(third)) == 10
    assert set(third) & set(first)


async def test_cohort_smaller_than_cap_polls_each_once(env, monkeypatch):
    monkeypatch.setattr(jan.CurrConfig, "JANITOR_HIATUS_SWEEP_MAX", 15, raising=False)
    env.hiatus.return_value = {"u1", "u2", "u3"}
    await jan.reconcile_hiatus_guild(MagicMock(), enforce=True)
    polled = [c.args[0] for c in env.player.await_args_list]
    assert sorted(polled) == ["u1", "u2", "u3"]

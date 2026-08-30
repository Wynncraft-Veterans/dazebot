"""Tests for ``lib.mc.resolve.live_guilds_for``.

Two contracts, and both exist because the single-account
``refresh_mc_guild`` gets them wrong for a *set*:

* **1+N, not 2N.** One Returners roster fetch for the whole batch, then a
  player call only for the uuids the roster doesn't name. Looping
  ``refresh_mc_guild`` would re-fetch the roster every time — the exact
  trap ``cogs/maintenance/janitor.py`` reconciler (H) documents and
  ``tests/test_janitor_hiatus_sweep.py`` locks down for the sweep.
* **Failure is reported.** ``refresh_mc_guild`` is best-effort: on an API
  failure it hands back the account with its stored value untouched, which
  a caller cannot distinguish from a successful "still guildless". Callers
  that must not act on a guess get None here instead.

Both upstreams are patched; nothing here touches the network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import lib.mc.resolve as resolve

A = "aaaaaaaa-0000-4000-8000-00000000000a"
B = "bbbbbbbb-0000-4000-8000-00000000000b"


def _roster(*uuids: str):
    g = MagicMock()
    g.members.all_members.return_value = [SimpleNamespace(uuid=u) for u in uuids]
    return g


def _player(guild_name: str | None):
    return SimpleNamespace(guild=SimpleNamespace(name=guild_name) if guild_name else None)


@pytest.fixture
def env(monkeypatch):
    state = SimpleNamespace(roster=_roster(), players={}, roster_error=None, player_error=None)

    async def _get_guild(name):
        if state.roster_error:
            raise state.roster_error
        return state.roster

    async def _get_player(uuid, **kw):
        if state.player_error:
            raise state.player_error
        state.player_calls.append((uuid, kw))
        return _player(state.players.get(uuid))

    state.player_calls = []
    guild_spy = AsyncMock(side_effect=_get_guild)
    monkeypatch.setattr(resolve, "get_guild", guild_spy)
    monkeypatch.setattr(resolve, "get_player_stats", AsyncMock(side_effect=_get_player))
    state.guild_spy = guild_spy
    return state


async def test_empty_input_costs_nothing(env):
    assert await resolve.live_guilds_for([]) == {}
    env.guild_spy.assert_not_awaited()


async def test_roster_members_resolve_without_a_player_call(env):
    """The free half: a roster hit is authoritative and already in hand."""
    env.roster = _roster(A, B)
    assert await resolve.live_guilds_for([A, B]) == {A: "Returners", B: "Returners"}
    assert env.player_calls == []


async def test_one_roster_call_regardless_of_batch_size(env):
    env.players = {A: None, B: "KongoBoys"}
    await resolve.live_guilds_for([A, B])
    env.guild_spy.assert_awaited_once()


async def test_non_roster_members_fall_through_to_the_player_endpoint(env):
    env.players = {A: None, B: "KongoBoys"}
    assert await resolve.live_guilds_for([A, B]) == {A: None, B: "KongoBoys"}


async def test_player_calls_can_be_backgrounded(env):
    env.players = {A: None}
    await resolve.live_guilds_for([A], background=True)
    assert env.player_calls[0][1]["background"] is True


async def test_a_stale_returners_claim_is_coerced_away(env):
    """WYNN-STALE-WORKAROUND: the roster is on the officer-write
    invalidation path and already said no, so a player endpoint still
    naming Returners is provably stale."""
    env.roster = _roster()  # A is NOT on the roster
    env.players = {A: "Returners"}
    assert await resolve.live_guilds_for([A]) == {A: None}


async def test_a_roster_failure_reports_none(env):
    env.roster_error = RuntimeError("503")
    assert await resolve.live_guilds_for([A, B]) is None


async def test_a_player_failure_reports_none(env):
    """Partial truth is worse than no truth for a caller that has to fail
    closed — so one bad lookup discards the batch."""
    env.player_error = RuntimeError("timeout")
    assert await resolve.live_guilds_for([A]) is None

"""Tests for ``hiatus_watcher``'s login-edge derivation.

``newly_online`` is a diff against ``_prev_online``, an in-memory set that
starts empty on every process start. That is fine for the channel alert,
which has a 24h cooldown behind it, but it means that after any restart
the diff names *everyone currently online* rather than anyone who just
logged in. Handing that to the "welcome back" DM would mail the entire
online HIATUS cohort on every redeploy.

``MinecraftAccount.online_seen_at`` is the persisted heartbeat that tells
the two apart, so what's under test here is: the prior values are read
*before* the bulk write that destroys them, and a player we were watching
moments ago is not treated as a fresh login.

Every seam is patched; no network, no ORM.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import cogs.activity.hiatus_watcher as hw

A = "aaaaaaaa-0000-0000-0000-000000000001"
B = "bbbbbbbb-0000-0000-0000-000000000002"


@pytest.fixture
def env(monkeypatch):
    """Patch the four seams ``poll`` touches and hand back the spies."""
    state = SimpleNamespace(
        cohort={A, B},
        online={A: "EU32", B: "NA11"},
        warm=[],            # uuids whose persisted online_seen_at is recent
        calls=[],           # ordered log of ORM interactions
    )

    def _filter(**kw):
        q = MagicMock()
        if "online_seen_at__gt" in kw:
            state.calls.append("read-prior")
            q.values_list = AsyncMock(return_value=list(state.warm))
        else:
            q.values_list = AsyncMock(return_value=[])

        async def _update(**ukw):
            state.calls.append("write-heartbeat")
            state.updated = (set(kw.get("uuid__in", [])), ukw)
            return len(kw.get("uuid__in", []))

        q.update = AsyncMock(side_effect=_update)
        return q

    res = MagicMock()
    res.status = 200
    res.json = AsyncMock(side_effect=lambda: {"players": dict(state.online)})
    requestor = MagicMock()
    requestor.get = AsyncMock(return_value=res)

    monkeypatch.setattr(hw, "hiatus_member_uuids", AsyncMock(side_effect=lambda b: state.cohort))
    monkeypatch.setattr(hw, "Requestor", MagicMock(return_value=requestor))
    monkeypatch.setattr(hw.MinecraftAccount, "filter", MagicMock(side_effect=_filter))
    alert = AsyncMock(return_value=True)
    monkeypatch.setattr(hw, "maybe_alert_hiatus", alert)

    state.alert = alert
    return state


def _cog(prev_online=frozenset()):
    """A stand-in for the cog. ``poll`` is a ``tasks.Loop``; ``.coro`` is
    the undecorated coroutine, so we can drive one tick without starting
    the loop."""
    bot = MagicMock()
    bot.config.HIATUS_ALERTS_ENABLED = True
    bot.config.HIATUS_RETURN_DM_LOGOUT_GAP_MINUTES = 10.0
    return SimpleNamespace(bot=bot, _prev_online=set(prev_online))


def _edges(alert) -> dict[str, bool]:
    return {c.args[1]: c.kwargs["login_edge"] for c in alert.await_args_list}


async def test_cold_cohort_is_a_real_login(env):
    """Nothing seen recently: they genuinely just logged in."""
    cog = _cog()
    await hw.HiatusWatcher.poll.coro(cog)
    assert _edges(env.alert) == {A: True, B: True}


async def test_restart_does_not_manufacture_logins(env):
    """The regression this column exists for: ``_prev_online`` is empty
    after a restart, so both are 'newly online' — but we were watching
    them 30 seconds ago, so neither is a login."""
    env.warm = [A, B]
    cog = _cog()
    await hw.HiatusWatcher.poll.coro(cog)
    assert _edges(env.alert) == {A: False, B: False}
    # The channel alert still fires for both — its behaviour is unchanged.
    assert env.alert.await_count == 2


async def test_edges_are_per_player(env):
    env.warm = [A]
    cog = _cog()
    await hw.HiatusWatcher.poll.coro(cog)
    assert _edges(env.alert) == {A: False, B: True}


async def test_prior_heartbeat_is_read_before_it_is_overwritten(env):
    """Ordering is the whole trick: the bulk write below destroys exactly
    the evidence the read above needs."""
    cog = _cog()
    await hw.HiatusWatcher.poll.coro(cog)
    assert env.calls == ["read-prior", "write-heartbeat"]


async def test_heartbeat_covers_the_whole_online_cohort(env):
    """Not just the newly-online slice — a player who stays online must
    keep their heartbeat fresh, or the next tick after they *do* leave
    would read a stale hole and call the return a login."""
    cog = _cog(prev_online={A})
    await hw.HiatusWatcher.poll.coro(cog)
    uuids, kwargs = env.updated
    assert uuids == {A, B}
    assert isinstance(kwargs["online_seen_at"], datetime)


async def test_already_online_players_are_not_re_alerted(env):
    cog = _cog(prev_online={A, B})
    await hw.HiatusWatcher.poll.coro(cog)
    env.alert.assert_not_awaited()
    # ...but their heartbeat is still refreshed.
    assert env.updated[0] == {A, B}


async def test_kill_switch_skips_the_tick_entirely(env):
    cog = _cog(prev_online={A})
    cog.bot.config.HIATUS_ALERTS_ENABLED = False
    await hw.HiatusWatcher.poll.coro(cog)
    assert cog._prev_online == set()
    assert env.calls == []
    env.alert.assert_not_awaited()

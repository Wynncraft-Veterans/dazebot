"""Tests for the guild gate on hiatus-spotted alerts.

HIATUS means "ex-member, currently guildless" (``.claude/role_state.md``
— *in a guild -> never Hiatus*), so ``maybe_alert_hiatus`` must not post
for a player who has joined a guild. The stored ``MinecraftAccount.guild``
is unreliable for exactly this cohort — nothing in the periodic path
scans a guild that no Returners member is in — so the gate re-reads it
live before posting, and heals the stale HIATUS role so the same player
doesn't stay in the watchers' poll scope.

A detection now feeds two sinks — the ``#activity`` channel post and the
"welcome back" DM — and the tests at the bottom pin the property that
makes that safe: they share the guild gate and its single live crosscheck,
and nothing else. In particular the channel's 24h cooldown must not
suppress a due DM, and a due DM must not make a suppressed channel post
happen.

The ORM and the Wynncraft API are patched out; what's under test is the
ordering and the branch conditions, not persistence.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import lib.mc.hiatus_alerts as ha
import lib.mc.hiatus_return_dm as hrd
import lib.mc.resolve as resolve
import lib.role_state as role_state
from lib.role_state import Trigger


@pytest.fixture
def account():
    """A guildless HIATUS-role account, as the watchers would hand it over."""
    return SimpleNamespace(
        uuid="2290f9f9-525f-49c9-9d2c-f01621a90824",
        mc_username="sowurf",
        guild=None,
        last_seen_server="AS19",
    )


@pytest.fixture
def bot():
    b = MagicMock()
    b.config.HIATUS_ALERTS_ENABLED = True
    b.config.HIATUS_SPOTTED_ALERT_CHANNEL = 999
    channel = MagicMock()
    channel.send = AsyncMock()
    b.get_channel.return_value = channel
    return b


@pytest.fixture
def patched(monkeypatch, account):
    """Patch the ORM/API seams and hand back the spies.

    ``refresh_mc_guild`` and ``fire_trigger_for_mc_uuids`` are imported
    *inside* the functions under test (import-cycle avoidance), so they
    must be patched on their defining modules, not on ``hiatus_alerts``.
    """
    monkeypatch.setattr(
        ha.MinecraftAccount, "get_or_none", AsyncMock(return_value=account)
    )
    cooldown_hit = MagicMock()
    cooldown_hit.exists = AsyncMock(return_value=False)
    monkeypatch.setattr(
        ha.HiatusSpottedAlert, "filter", MagicMock(return_value=cooldown_hit)
    )
    monkeypatch.setattr(ha.HiatusSpottedAlert, "create", AsyncMock())

    refresh = AsyncMock(side_effect=lambda mc: mc)
    monkeypatch.setattr(resolve, "refresh_mc_guild", refresh)
    fire = AsyncMock()
    monkeypatch.setattr(role_state, "fire_trigger_for_mc_uuids", fire)

    # The DM sink is off unless a test opts in. Pinned rather than relying
    # on the shipped default, so flipping that default can't silently
    # change what the channel-sink assertions below mean.
    plan = AsyncMock(return_value=None)
    verify = AsyncMock(return_value=True)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(hrd, "plan_return_dm", plan)
    monkeypatch.setattr(hrd, "verify_return_dm", verify)
    monkeypatch.setattr(hrd, "send_return_dm", send)

    return SimpleNamespace(
        refresh=refresh, fire=fire, cooldown=cooldown_hit,
        plan=plan, verify=verify, send=send,
    )


def _live_guild(refresh, name: str | None):
    """Make the live crosscheck report ``name`` as the player's guild."""

    async def _fake(mc):
        mc.guild = name
        return mc

    refresh.side_effect = _fake


# ---------------------------------------------------------------------------
# the happy path still works
# ---------------------------------------------------------------------------


async def test_alerts_when_guildless_both_stored_and_live(bot, patched):
    assert await ha.maybe_alert_hiatus(bot, "u", server="AS19") is True
    bot.get_channel.return_value.send.assert_awaited_once()
    assert "AS19" in bot.get_channel.return_value.send.await_args.args[0]
    patched.fire.assert_not_awaited()


# ---------------------------------------------------------------------------
# guild gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("guild_name", ["Returners", "KongoBoys", "Grah"])
async def test_stored_guild_suppresses_without_api_spend(bot, patched, account, guild_name):
    account.guild = guild_name
    assert await ha.maybe_alert_hiatus(bot, "u") is False
    bot.get_channel.return_value.send.assert_not_awaited()
    # Fast path: we already know they're in a guild, so no live re-read.
    patched.refresh.assert_not_awaited()


async def test_live_other_guild_suppresses_and_heals(bot, patched, account):
    _live_guild(patched.refresh, "KongoBoys")
    assert await ha.maybe_alert_hiatus(bot, account.uuid) is False
    bot.get_channel.return_value.send.assert_not_awaited()
    ha.HiatusSpottedAlert.create.assert_not_awaited()
    patched.fire.assert_awaited_once()
    _, uuids, trigger = patched.fire.await_args.args
    assert uuids == {account.uuid}
    assert trigger is Trigger.JOINED_OTHER_GUILD


async def test_live_returners_suppresses_and_heals_to_member(bot, patched, account):
    _live_guild(patched.refresh, "Returners")
    assert await ha.maybe_alert_hiatus(bot, account.uuid) is False
    bot.get_channel.return_value.send.assert_not_awaited()
    assert patched.fire.await_args.args[2] is Trigger.JOINED_VETS


async def test_api_failure_leaves_stored_value_and_still_alerts(bot, patched):
    # refresh_mc_guild is best-effort: on an API failure it returns the
    # account untouched (guild stays None) and we alert on what we have.
    assert await ha.maybe_alert_hiatus(bot, "u") is True
    bot.get_channel.return_value.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# ordering: the cheap checks stay ahead of the live crosscheck
# ---------------------------------------------------------------------------


async def test_cooldown_short_circuits_before_refresh(bot, patched):
    patched.cooldown.exists = AsyncMock(return_value=True)
    assert await ha.maybe_alert_hiatus(bot, "u") is False
    patched.refresh.assert_not_awaited()
    bot.get_channel.return_value.send.assert_not_awaited()


async def test_kill_switch_short_circuits_before_refresh(bot, patched):
    bot.config.HIATUS_ALERTS_ENABLED = False
    assert await ha.maybe_alert_hiatus(bot, "u") is False
    patched.refresh.assert_not_awaited()


# ---------------------------------------------------------------------------
# two sinks, one guild gate
# ---------------------------------------------------------------------------


def _dm_due(patched):
    """Make the DM sink want this detection."""
    patched.plan.return_value = MagicMock(name="ReturnDmPlan")


async def test_a_due_dm_survives_the_channel_cooldown(bot, patched):
    """The channel cooldown used to return early. If it still did, the
    snooze button's "next time you log in" could never be honoured inside
    a day."""
    patched.cooldown.exists = AsyncMock(return_value=True)
    _dm_due(patched)

    # Return value is the *channel* result, unchanged from before the DM
    # existed — both watcher call sites depend on that.
    assert await ha.maybe_alert_hiatus(bot, "u", login_edge=True) is False
    bot.get_channel.return_value.send.assert_not_awaited()
    patched.send.assert_awaited_once()


async def test_a_due_channel_post_does_not_force_a_dm(bot, patched):
    assert await ha.maybe_alert_hiatus(bot, "u") is True
    bot.get_channel.return_value.send.assert_awaited_once()
    patched.send.assert_not_awaited()


async def test_neither_sink_due_spends_no_api(bot, patched):
    """The whole point of evaluating both cheap gates before the live
    crosscheck: a detection nobody wants must stay free."""
    patched.cooldown.exists = AsyncMock(return_value=True)
    assert await ha.maybe_alert_hiatus(bot, "u", login_edge=True) is False
    patched.refresh.assert_not_awaited()
    patched.send.assert_not_awaited()


async def test_login_edge_reaches_the_dm_sink_only(bot, patched):
    await ha.maybe_alert_hiatus(bot, "u", login_edge=True)
    assert patched.plan.await_args.kwargs["login_edge"] is True
    # ...and the channel post is indifferent to it.
    bot.get_channel.return_value.send.assert_awaited_once()


async def test_live_guild_suppresses_both_sinks(bot, patched, account):
    _dm_due(patched)
    _live_guild(patched.refresh, "KongoBoys")
    assert await ha.maybe_alert_hiatus(bot, account.uuid, login_edge=True) is False
    bot.get_channel.return_value.send.assert_not_awaited()
    patched.send.assert_not_awaited()


async def test_a_broken_dm_does_not_take_the_channel_post_down(bot, patched):
    """The staff-facing signal is the one that has to be reliable."""
    _dm_due(patched)
    patched.send.side_effect = RuntimeError("discord is having a day")
    assert await ha.maybe_alert_hiatus(bot, "u", login_edge=True) is True
    bot.get_channel.return_value.send.assert_awaited_once()


async def test_the_live_verify_pass_can_veto_the_dm(bot, patched):
    """The plan is built on stored data. If the second, live pass finds the
    person is in a guild on some *other* account — the one cohort whose
    stored `guild` is known to rot — the send is dropped, and the channel
    post is unaffected."""
    _dm_due(patched)
    patched.verify.return_value = False
    assert await ha.maybe_alert_hiatus(bot, "u", login_edge=True) is True
    bot.get_channel.return_value.send.assert_awaited_once()
    patched.send.assert_not_awaited()


async def test_verify_runs_after_the_shared_guild_crosscheck(bot, patched):
    """Ordering: it re-reads roles that the crosscheck's heal may have just
    changed, so it has to come second."""
    order = []
    patched.refresh.side_effect = lambda mc: (order.append("refresh"), mc)[1]
    patched.verify.side_effect = lambda *a, **k: (order.append("verify"), True)[1]
    _dm_due(patched)
    await ha.maybe_alert_hiatus(bot, "u", login_edge=True)
    assert order == ["refresh", "verify"]

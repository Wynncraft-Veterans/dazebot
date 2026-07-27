"""Tests for the guild gate on hiatus-spotted alerts.

HIATUS means "ex-member, currently guildless" (``.claude/role_state.md``
— *in a guild -> never Hiatus*), so ``maybe_alert_hiatus`` must not post
for a player who has joined a guild. The stored ``MinecraftAccount.guild``
is unreliable for exactly this cohort — nothing in the periodic path
scans a guild that no Returners member is in — so the gate re-reads it
live before posting, and heals the stale HIATUS role so the same player
doesn't stay in the watchers' poll scope.

The ORM and the Wynncraft API are patched out; what's under test is the
ordering and the branch conditions, not persistence.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import lib.mc.hiatus_alerts as ha
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

    return SimpleNamespace(refresh=refresh, fire=fire, cooldown=cooldown_hit)


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

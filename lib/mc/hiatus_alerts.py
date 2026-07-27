"""Cooldown-gated alert helper for hiatus-spotted notifications.

Two cogs (``cogs/activity/server_watcher.py`` and ``cogs/activity/
hiatus_watcher.py``) both feed potential hiatus-online detections
through this module so the dedup logic (24h per-UUID cooldown,
guild sanity check, channel resolution) lives in one place.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord

from orm import HiatusSpottedAlert, MinecraftAccount

logger = logging.getLogger("dazebot.lib.mc.hiatus_alerts")

HIATUS_ALERT_COOLDOWN = timedelta(hours=24)


async def _heal_stale_hiatus(bot, account: MinecraftAccount) -> None:
    """Fire the membership transition that the HIATUS role holder is
    actually owed, now that we know they're in ``account.guild``.

    Nothing in the periodic path observes a *guildless* player joining an
    unscanned guild: ``cogs/activity`` only walks the Returners roster
    plus whatever other guilds current Returners members turn up in, so a
    HIATUS user who joins some third guild is never seen and keeps the
    role forever. Without this, the guild gate below would suppress their
    alert on every detection but they'd stay in the watchers' poll scope
    indefinitely, burning API calls on a role that should be REGISTERED.
    """
    from lib.role_state import Trigger, fire_trigger_for_mc_uuids  # local: import cycle

    trigger = (
        Trigger.JOINED_VETS if account.guild == "Returners" else Trigger.JOINED_OTHER_GUILD
    )
    logger.info(
        "healing stale HIATUS for %s (%s): in guild %r -> %s",
        account.mc_username, account.uuid, account.guild, trigger.value,
    )
    await fire_trigger_for_mc_uuids(
        bot, {account.uuid}, trigger, reason="hiatus-alert: stale HIATUS, player is in a guild"
    )


async def maybe_alert_hiatus(bot, uuid: str, *, server: str | None = None) -> bool:
    """Post a hiatus-spotted alert if the 24h cooldown for ``uuid`` has
    elapsed. Returns True if a message was posted, False otherwise.

    ``server`` is the world string from the live observation that triggered
    this call (e.g. ``hiatus_watcher``'s bulk-endpoint dict value). It
    takes precedence in the alert text; if not provided or falsy, falls
    back to the persisted ``account.last_seen_server``, then to ``"?"``.
    The persisted value is stale or missing for the non-privacy-hidden
    cohort that ``server_watcher`` never polls.

    **Guild gate.** HIATUS means "ex-member, currently guildless" (see
    ``../../.claude/role_state.md`` — *in a guild -> never Hiatus*), so an
    account that is in *any* guild is skipped, not just Returners. The
    role snapshots drift in both directions: briefly during a join
    transition, and permanently for a HIATUS user who joins a guild
    nobody scans (nothing else in the periodic path observes that — see
    ``_heal_stale_hiatus``). The stored ``guild`` column is itself stale
    for exactly that cohort, so once the cheap checks pass we re-read it
    live via ``refresh_mc_guild`` before committing to a post. That costs
    at most one refresh per alert, and the self-heal drops a genuinely
    moved-on player out of the watchers' scope so it doesn't recur.
    """
    from lib.mc.resolve import refresh_mc_guild  # local: import cycle

    if not bot.config.HIATUS_ALERTS_ENABLED:
        return False
    account = await MinecraftAccount.get_or_none(uuid=uuid)
    if account is None:
        return False
    # Stored-guild fast path: no API spend when we already know they're in
    # a guild. The live crosscheck below only runs for accounts we believe
    # to be guildless.
    if account.guild is not None:
        return False

    cutoff = datetime.now(timezone.utc) - HIATUS_ALERT_COOLDOWN
    if await HiatusSpottedAlert.filter(uuid=uuid, created_at__gte=cutoff).exists():
        return False

    # Best-effort: leaves the stored value untouched if the API is down,
    # in which case we fall through and alert on what we have.
    await refresh_mc_guild(account)
    if account.guild is not None:
        logger.info(
            "hiatus alert for %s (%s) suppressed: live guild is %r",
            account.mc_username, uuid, account.guild,
        )
        await _heal_stale_hiatus(bot, account)
        return False

    channel = bot.get_channel(bot.config.HIATUS_SPOTTED_ALERT_CHANNEL)
    if channel is None:
        logger.warning(
            "hiatus alert for %s suppressed: channel %s unresolved",
            uuid, bot.config.HIATUS_SPOTTED_ALERT_CHANNEL,
        )
        return False

    server = server or account.last_seen_server or "?"
    try:
        await channel.send(
            f"Hiatus user `{account.mc_username}` ({uuid}) is currently online on {server}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as e:
        logger.warning("hiatus alert post failed for %s: %s", uuid, e)
        return False

    await HiatusSpottedAlert.create(uuid=uuid)
    logger.info("posted hiatus alert for %s (%s) on %s", account.mc_username, uuid, server)
    return True

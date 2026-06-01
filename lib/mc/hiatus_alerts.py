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


async def maybe_alert_hiatus(bot, uuid: str, *, server: str | None = None) -> bool:
    """Post a hiatus-spotted alert if the 24h cooldown for ``uuid`` has
    elapsed. Returns True if a message was posted, False otherwise.

    ``server`` is the world string from the live observation that triggered
    this call (e.g. ``hiatus_watcher``'s bulk-endpoint dict value). It
    takes precedence in the alert text; if not provided or falsy, falls
    back to the persisted ``account.last_seen_server``, then to ``"?"``.
    The persisted value is stale or missing for the non-privacy-hidden
    cohort that ``server_watcher`` never polls.

    Belt-and-suspenders: an account whose ``guild`` column is currently
    ``"Returners"`` is skipped even if the caller asserts HIATUS, since
    the role/guild snapshots can drift briefly during a join transition.
    """
    if not bot.config.HIATUS_ALERTS_ENABLED:
        return False
    account = await MinecraftAccount.get_or_none(uuid=uuid)
    if account is None:
        return False
    if account.guild == "Returners":
        return False

    cutoff = datetime.now(timezone.utc) - HIATUS_ALERT_COOLDOWN
    if await HiatusSpottedAlert.filter(uuid=uuid, created_at__gte=cutoff).exists():
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

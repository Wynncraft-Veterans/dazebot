"""Cooldown-gated alert helper for hiatus-spotted notifications.

Two cogs (``cogs/activity/server_watcher.py`` and ``cogs/activity/
hiatus_watcher.py``) both feed potential hiatus-online detections
through this module so the dedup logic (24h per-UUID cooldown,
guild sanity check, channel resolution) lives in one place.

A detection now fans out to **two independent sinks**:

* the ``#activity`` channel post for staff (this module), on a 24h
  per-UUID cooldown;
* the "welcome back" DM to the player (``lib/mc/hiatus_return_dm.py``),
  on its own, much stricter set of gates.

They are deliberately not chained. The channel cooldown used to be the
first thing checked and returned early, which would have starved the DM:
the snooze button promises "next time you log in", and a player who logs
back in six hours later would have been swallowed by a 24h cooldown that
has nothing to do with them. What the two sinks *do* share is the live
guild crosscheck below and the single Wynncraft request it costs, which
is why both sinks' cheap gates are evaluated before it — if neither sink
wants this detection we spend nothing, exactly as before.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord

from orm import HiatusSpottedAlert, MinecraftAccount

logger = logging.getLogger("dazebot.lib.mc.hiatus_alerts")

HIATUS_ALERT_COOLDOWN = timedelta(hours=24)


async def heal_stale_hiatus(bot, account: MinecraftAccount) -> None:
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


async def _post_channel_alert(bot, account: MinecraftAccount, server: str) -> bool:
    """The staff-facing half. Returns True if a message was posted."""
    channel = bot.get_channel(bot.config.HIATUS_SPOTTED_ALERT_CHANNEL)
    if channel is None:
        logger.warning(
            "hiatus alert for %s suppressed: channel %s unresolved",
            account.uuid, bot.config.HIATUS_SPOTTED_ALERT_CHANNEL,
        )
        return False
    try:
        await channel.send(
            f"Hiatus user `{account.mc_username}` ({account.uuid}) is currently online on {server}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as e:
        logger.warning("hiatus alert post failed for %s: %s", account.uuid, e)
        return False

    await HiatusSpottedAlert.create(uuid=account.uuid)
    logger.info(
        "posted hiatus alert for %s (%s) on %s", account.mc_username, account.uuid, server
    )
    return True


async def maybe_alert_hiatus(
    bot, uuid: str, *, server: str | None = None, login_edge: bool = False
) -> bool:
    """Run both hiatus sinks for ``uuid``. Returns True if the *channel*
    alert was posted — unchanged from before the DM existed, so the two
    watcher call sites keep their contract.

    ``server`` is the world string from the live observation that triggered
    this call (e.g. ``hiatus_watcher``'s bulk-endpoint dict value). It
    takes precedence in the alert text; if not provided or falsy, falls
    back to the persisted ``account.last_seen_server``, then to ``"?"``.
    The persisted value is stale or missing for the non-privacy-hidden
    cohort that ``server_watcher`` never polls.

    ``login_edge`` says whether this detection is a genuine offline ->
    online transition rather than a fresh observation of a session we were
    already watching. Only the DM sink consults it; the channel post is
    unchanged and still fires off any detection. The distinction matters
    because ``server_watcher``'s stat-delta branch re-enters here on
    essentially every tick of active play, and ``hiatus_watcher``'s
    newly-online diff is empty-initialised on every restart — neither is a
    login, and the DM must not treat them as one.

    **Guild gate.** HIATUS means "ex-member, currently guildless" (see
    ``../../.claude/role_state.md`` — *in a guild -> never Hiatus*), so an
    account that is in *any* guild is skipped, not just Returners. The
    role snapshots drift in both directions: briefly during a join
    transition, and permanently for a HIATUS user who joins a guild
    nobody scans (nothing else in the periodic path observes that — see
    ``heal_stale_hiatus``). The stored ``guild`` column is itself stale
    for exactly that cohort, so once the cheap checks pass we re-read it
    live via ``refresh_mc_guild`` before committing to a post. That costs
    at most one refresh per alert, and the self-heal drops a genuinely
    moved-on player out of the watchers' scope so it doesn't recur.

    One behavioural note on that refresh. It used to sit *below* the 24h
    channel cooldown, so a stale-HIATUS player got at most one heal per
    day. Now a due DM can also pull us past the cooldown and into the
    refresh, so ``heal_stale_hiatus`` can fire on detections the cooldown
    used to swallow. That is a strictly better outcome — the heal is the
    thing that stops us polling someone who shouldn't be in scope — but it
    is a real change in when Discord role writes happen.
    """
    from lib.mc.hiatus_return_dm import (  # local: import cycle
        plan_return_dm,
        send_return_dm,
        verify_return_dm,
    )
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

    # Both sinks' cheap gates first, so that "neither sink wants this" is
    # still a zero-request outcome.
    cutoff = datetime.now(timezone.utc) - HIATUS_ALERT_COOLDOWN
    channel_due = not await HiatusSpottedAlert.filter(uuid=uuid, created_at__gte=cutoff).exists()
    dm_plan = await plan_return_dm(bot, account, login_edge=login_edge)
    if not channel_due and dm_plan is None:
        return False

    # Best-effort: leaves the stored value untouched if the API is down,
    # in which case we fall through and alert on what we have.
    await refresh_mc_guild(account)
    if account.guild is not None:
        logger.info(
            "hiatus alert for %s (%s) suppressed: live guild is %r",
            account.mc_username, uuid, account.guild,
        )
        await heal_stale_hiatus(bot, account)
        return False

    posted = False
    if channel_due:
        posted = await _post_channel_alert(bot, account, server or account.last_seen_server or "?")

    if dm_plan is not None:
        # A failure here must not take the channel alert down with it —
        # the staff-facing signal is the one that has to be reliable.
        try:
            # Second, live pass: the plan was built on stored data, and for
            # this cohort the stored `guild` of the person's *other*
            # accounts is exactly what goes stale. See verify_return_dm.
            if await verify_return_dm(bot, account, dm_plan):
                await send_return_dm(bot, account, dm_plan)
        except Exception:  # noqa: BLE001 — Discord + DB, best-effort by contract
            logger.exception("hiatus-return DM failed for %s (%s)", account.mc_username, uuid)

    return posted

"""Discord-side handling for guild rank-change alerts.

Triggered by ``POST /api/internal/rank-alert`` from temporary-server when a
vetsmod client observes an in-game guild rank change. Two classifications
arrive here:

- ``ban`` — Captain/Strategist/Chief/Owner demoted someone to Recruit.
  Posted with a role @ping.
- ``kick`` — Recruit set back to Recruit (failed-onboarding signal).
  Posted without a ping.

After posting, schedules an async WAPI verification: waits for Wynncraft's
guild API cache to turn over (multi-minute TTL) then fetches the target
player's current rank and edits the alert message with a VERIFIED or
UNVERIFIED tag.

Mote celebrations (``a → a`` where ``a != Recruit``) are handled
*entirely* by temporary-server's bridge sender — they never reach this
module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger("dazebot.rank_alerts")

# Channel that receives both BAN and KICK notices (the alerts that
# request admin action -- urgent BAN with a role ping, deferred KICK
# without one).
RANK_ALERT_CHANNEL_ID: int = 1313786394929528832
RANK_ALERT_PING_ROLE_ID: int = 1313778812361904188

# Thread (under any channel; resolved by ID alone) that collects
# moderation notifications which do NOT request admin action: EJECT
# notices (the eject already happened or was dispatched by the actor)
# and admin caution-point overrides (purely audit-trail entries). Any
# alert that asks an admin to do something belongs in
# ``RANK_ALERT_CHANNEL_ID`` instead.
MODERATION_LOG_THREAD_ID: int = 1502977253297098762

# How long to wait before WAPI verification — Wynncraft's guild API has a
# multi-minute cache TTL, so a quick verify will see stale data. 5 minutes
# strikes a balance between "verification appears reasonably soon" and
# "WAPI has had time to refresh".
WAPI_VERIFY_DELAY_SECONDS: int = 5 * 60


async def post_rank_alert(
    bot: "Bot",
    classification: str,
    actor: str,
    target: str,
    from_rank: str,
    to_rank: str,
) -> None:
    """Post a BAN or KICK alert to the configured Discord channel.

    Returns immediately on misconfiguration / channel-resolve failures; this
    is fire-and-forget from the HTTP endpoint's perspective. Schedules WAPI
    verification on success.
    """
    if classification not in ("ban", "kick"):
        logger.warning("post_rank_alert: ignoring classification %r", classification)
        return

    # Both BAN and KICK notices land in the rank-alert channel: BAN is
    # urgent (pinged, "kick ASAP"); KICK is deferred (no ping, "kick
    # when convenient") but still requires an admin to act, so it
    # belongs alongside BAN rather than in the informational
    # moderation-log thread.
    channel = bot.get_channel(RANK_ALERT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(RANK_ALERT_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            logger.error(
                "post_rank_alert: cannot fetch channel %s: %s",
                RANK_ALERT_CHANNEL_ID, exc,
            )
            return

    if not isinstance(channel, discord.abc.Messageable):
        logger.error(
            "post_rank_alert: channel %s is not messageable", RANK_ALERT_CHANNEL_ID,
        )
        return

    if classification == "ban":
        content = (
            f"BAN NOTICE <@&{RANK_ALERT_PING_ROLE_ID}>\n"
            f"{actor} is requesting {target} be banned!\n"
            "This could be due to the warning cap being exceeded, "
            "or due to an emergency.\n"
            "Either way, get on it, they need to be kicked ASAP!"
        )
        allowed = discord.AllowedMentions(
            everyone=False, users=False,
            roles=[discord.Object(id=RANK_ALERT_PING_ROLE_ID)],
        )
    else:  # kick
        content = (
            "KICK NOTICE (no ping)\n"
            f"{actor} has put in notification that {target} failed their onboarding.\n"
            "This means they need to be kicked if they are still in the guild."
        )
        allowed = discord.AllowedMentions.none()

    try:
        message = await channel.send(content, allowed_mentions=allowed)
    except discord.HTTPException as exc:
        logger.error("post_rank_alert: send failed: %s", exc)
        return

    logger.info(
        "rank-alert posted: %s — %s set %s from %s to %s (msg id=%s)",
        classification.upper(), actor, target, from_rank, to_rank, message.id,
    )

    asyncio.create_task(
        _verify_and_tag(message, target, to_rank),
        name=f"rank-alert-verify-{message.id}",
    )


async def _verify_and_tag(
    message: discord.Message,
    target: str,
    expected_rank: str,
) -> None:
    """Wait for WAPI cache to turn over, then verify the rank and edit the message.

    On success, appends ``[VERIFIED]`` or ``[UNVERIFIED — WAPI shows: <rank>]``
    to the original alert content. On WAPI failure, appends a verification-
    failed note instead.
    """
    await asyncio.sleep(WAPI_VERIFY_DELAY_SECONDS)

    actual_rank: str | None = None
    failure_note: str | None = None

    try:
        # Imported lazily so a WAPI module-load failure doesn't take down
        # the whole rank-alert path at import time.
        from lib.wynn_api.player import get_player_stats

        player = await get_player_stats(target)
        if player.guild is not None:
            actual_rank = player.guild.rank
    except Exception as exc:  # noqa: BLE001 — WAPI is third-party
        logger.warning("rank-alert verify: WAPI lookup failed for %s: %s", target, exc)
        failure_note = "verification failed (WAPI error)"

    if failure_note is not None:
        tag = f"[{failure_note}]"
    elif actual_rank is None:
        tag = "[UNVERIFIED — target not in any Wynncraft guild]"
    elif actual_rank.strip().lower() == expected_rank.strip().lower():
        tag = "[VERIFIED]"
    else:
        tag = f"[UNVERIFIED — WAPI shows rank: {actual_rank}]"

    new_content = f"{message.content}\n\n{tag}"
    try:
        await message.edit(content=new_content, allowed_mentions=discord.AllowedMentions.none())
        logger.info(
            "rank-alert verify: edited msg %s with %s (target=%s, expected=%s, actual=%s)",
            message.id, tag, target, expected_rank, actual_rank,
        )
    except discord.HTTPException as exc:
        logger.warning("rank-alert verify: edit failed for msg %s: %s", message.id, exc)

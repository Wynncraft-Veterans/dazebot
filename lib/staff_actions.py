"""Caution / warning / eject record-keeping for in-game guild staff.

Three action kinds, with escalating point values:

  * ``caution`` (1 pt)  -- informal note. No DM unless the commit just
    crossed the warning threshold.
  * ``warning`` (3 pts) -- formal. DMs the warned user.
  * ``eject``   (6 pts) -- immediate ban-threshold trip. DMs the target,
    adds them to the dazebot blocklist, posts an EJECT NOTICE in the
    rank-alert channel, and tells the issuing client what in-game
    command to dispatch (``/gu kick`` for chiefs, ``/gu rank ... recruit``
    for captains/strategists -- the workaround for the Wynncraft
    "only chiefs can kick" limitation).

Threshold rule on commit: if cumulative points were ``< 3`` before this
row was inserted and are now ``>= 3``, a formal warning is triggered
even on a plain caution. Likewise for ``< 6 -> >= 6`` and ejects.
Sqlite serialises writes, so the race between two concurrent cautioners
is handled implicitly: only one of them will own the threshold-crossing
commit and trigger the DM.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Optional

import discord
from tortoise.expressions import Q

from lib.linking import dm_or_log
from lib.wynn_api.player import get_player_full_stats
from orm import (
    Blocklist,
    DiscordAccount,
    MinecraftAccount,
    StaffActionEntry,
    UNKNOWN_LAST_ONLINE,
)

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger("dazebot.staff_actions")


# Point values per action. Keep these in sync with the server-side wire
# protocol and with vetsmod's local UI hints; the source of truth is
# nevertheless the row written here.
POINTS_BY_KIND: dict[str, int] = {
    "caution": 1,
    "warning": 3,
    "eject": 6,
}

# Cumulative-points thresholds for escalation triggers.
WARNING_THRESHOLD = 3
EJECT_THRESHOLD = 6

# The DM prefix used on every formal warning AND eject. The user spec
# says ejects use the same formal-warning message format; we reuse a
# single prefix so the player sees one consistent shape regardless of
# which command landed on them. vetsmod clients receiving a chat-bus
# ``warning`` frame format this prefix specially in-game; the Discord
# DM lands here verbatim.
WARNING_PREFIX = "⚠ WARNING ⚠"


@dataclass
class ActionResult:
    """Outcome of recording a single staff-action commit."""

    target_uuid: str
    target_username: str
    prior_total: int
    new_total: int
    # "none" | "warning" | "eject" -- which (if any) escalation fired on
    # this commit. ``warning``/``eject`` kinds always produce their own
    # escalation; ``caution`` only escalates when it crossed a threshold.
    triggered: str
    dm_sent: bool
    blocklisted: bool  # True iff this commit *newly* added a blocklist row
    suggested_dispatch: Optional[str]  # "kick" | "demote" | None


# ---------------------------------------------------------------------------
# Resolution: a temp-server WS frame carries a username, but we key entries
# on UUID. resolve() turns the former into the latter, creating a
# MinecraftAccount row when we've never seen this player before.
# ---------------------------------------------------------------------------


async def resolve_target(name_or_uuid: str) -> tuple[str, str, MinecraftAccount]:
    """Return ``(uuid, canonical_username, mc_account)`` for ``name_or_uuid``.

    Looks the value up in the local ``MinecraftAccount`` table first
    (matching uuid / mc_username / wynn_username case-insensitively); on
    miss, falls back to the Wynncraft API (which accepts either a UUID or
    a username) and creates a row so subsequent lookups are cheap. Raises
    :class:`WynnApiError` (or ``Exception`` for non-WAPI failures) if the
    name cannot be resolved.
    """
    existing = await MinecraftAccount.filter(
        Q(uuid=name_or_uuid)
        | Q(mc_username__iexact=name_or_uuid)
        | Q(wynn_username__iexact=name_or_uuid)
    ).first()
    if existing is not None:
        return existing.uuid, existing.mc_username, existing
    fs = await get_player_full_stats(name_or_uuid)
    mc = await MinecraftAccount.create(
        uuid=fs.uuid,
        wynn_username=fs.username,
        mc_username=fs.username,
        guild=fs.guild.name if fs.guild else None,
        last_online=fs.lastJoin or UNKNOWN_LAST_ONLINE,
        last_manual_check=UNKNOWN_LAST_ONLINE,
        first_join=fs.firstJoin,
    )
    return mc.uuid, mc.mc_username, mc


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def total_points_for(target_uuid: str) -> int:
    """Cumulative caution-point total for ``target_uuid``. ``0`` if the
    target has no entries.

    Sums in Python rather than via SQL ``SUM()`` -- per-target row counts
    are tiny (rarely more than a few dozen entries) and the values_list
    query keeps the tortoise-orm idiom simple.
    """
    points = await StaffActionEntry.filter(
        target_uuid=target_uuid
    ).values_list("points", flat=True)
    return sum(points)


async def history_for(
    target_uuid: str, limit: int = 25
) -> list[StaffActionEntry]:
    """Most-recent entries for ``target_uuid`` first."""
    return await (
        StaffActionEntry.filter(target_uuid=target_uuid)
        .order_by("-created_at")
        .limit(limit)
    )


# ---------------------------------------------------------------------------
# Writes (the meat)
# ---------------------------------------------------------------------------


async def record_action(
    bot: "Bot",
    *,
    kind: str,
    target_username: str,
    actor_uuid: str,
    actor_username: str,
    actor_rank: Optional[str],
    message: Optional[str],
) -> ActionResult:
    """Record one caution/warning/eject. Single source of truth for the
    side-effects: insert the audit row, DM if escalating, blocklist on
    eject, post the EJECT NOTICE on eject.

    ``actor_rank`` is the issuing staff member's in-game guild rank
    (``chief``/``strategist``/``captain``/...). It only affects the
    ``suggested_dispatch`` field for ejects -- chiefs get ``"kick"``,
    everyone else gets ``"demote"``.
    """
    if kind not in POINTS_BY_KIND:
        raise ValueError(f"unknown kind {kind!r}")
    points = POINTS_BY_KIND[kind]

    target_uuid, canonical_username, mc = await resolve_target(target_username)
    prior_total = await total_points_for(target_uuid)
    new_total = prior_total + points

    crossed_warning = prior_total < WARNING_THRESHOLD <= new_total
    crossed_eject = prior_total < EJECT_THRESHOLD <= new_total

    # Determine which escalation fires on THIS commit. Eject wins over
    # warning when both would fire (rare: a /caution that takes total
    # 5 -> 6 trips eject without the warning DM).
    if kind == "eject" or crossed_eject:
        triggered = "eject"
    elif kind == "warning" or crossed_warning:
        triggered = "warning"
    else:
        triggered = "none"

    await StaffActionEntry.create(
        target_uuid=target_uuid,
        target_username_at_time=canonical_username,
        actor_uuid=actor_uuid,
        actor_username_at_time=actor_username,
        kind=kind,
        points=points,
        message=message,
    )

    dm_sent = False
    if triggered != "none":
        dm_sent = await _deliver_dm(
            bot,
            target_uuid=target_uuid,
            target_username=canonical_username,
            triggered=triggered,
            actor_username=actor_username,
            message=message,
        )

    blocklisted = False
    if triggered == "eject":
        blocklisted = await _ensure_blocklisted(
            mc=mc,
            actor_uuid=actor_uuid,
            actor_username=actor_username,
            message=message,
        )
        await _post_eject_notice(
            bot,
            actor_username=actor_username,
            actor_rank=actor_rank,
            target_username=canonical_username,
            target_uuid=target_uuid,
            message=message,
        )

    suggested = _suggest_dispatch(actor_rank) if triggered == "eject" else None

    logger.info(
        "staff_action: kind=%s actor=%s(%s,%s) target=%s(%s) "
        "prior=%d new=%d triggered=%s dm=%s blocklist=%s suggest=%s",
        kind, actor_username, actor_uuid, actor_rank,
        canonical_username, target_uuid,
        prior_total, new_total, triggered, dm_sent, blocklisted, suggested,
    )

    return ActionResult(
        target_uuid=target_uuid,
        target_username=canonical_username,
        prior_total=prior_total,
        new_total=new_total,
        triggered=triggered,
        dm_sent=dm_sent,
        blocklisted=blocklisted,
        suggested_dispatch=suggested,
    )


# ---------------------------------------------------------------------------
# Admin override (manual point adjustments)
# ---------------------------------------------------------------------------


async def record_adjustment(
    bot: "Bot",
    *,
    target_username: str,
    actor_uuid: str,
    actor_username: str,
    delta: int,
    reason: Optional[str],
) -> ActionResult:
    """Insert an admin-override "adjustment" row that bumps a target's
    cumulative point total by ``delta`` (positive or negative).

    Unlike :func:`record_action`, this path is **side-effect-light**:
    no DM, no blocklist add, no EJECT notice, no in-game push.
    Adjustments are explicit administrative edits and shouldn't cascade
    through the warning pipeline -- if an admin wants the side-effects,
    they run ``/eject`` or ``~block`` directly.

    A log entry is posted to the moderation-log thread so admin overrides
    remain auditable in Discord without a DB query.

    Returns an :class:`ActionResult` with ``triggered="none"``,
    ``dm_sent=False``, ``blocklisted=False``, ``suggested_dispatch=None``.
    """
    target_uuid, canonical_username, _mc = await resolve_target(target_username)
    prior_total = await total_points_for(target_uuid)
    new_total = prior_total + delta

    await StaffActionEntry.create(
        target_uuid=target_uuid,
        target_username_at_time=canonical_username,
        actor_uuid=actor_uuid,
        actor_username_at_time=actor_username,
        kind="adjustment",
        points=delta,
        message=reason,
    )

    # Audit log entry. Done on a best-effort basis -- a thread post
    # failure must not abort the DB insert (the row is the source of
    # truth; the thread message is for human-readability).
    reason_blurb = f"\nReason: {reason.strip()}" if reason else ""
    sign = "+" if delta >= 0 else ""
    content = (
        f"ADMIN ADJUSTMENT\n"
        f"`{actor_username}` adjusted `{canonical_username}`'s caution "
        f"points by {sign}{delta} ({prior_total} -> {new_total})."
        f"{reason_blurb}"
    )
    await _post_to_moderation_thread(bot, content, label="admin adjustment")

    logger.info(
        "staff_action adjust: actor=%s(%s) target=%s(%s) delta=%+d "
        "prior=%d new=%d reason=%r",
        actor_username, actor_uuid, canonical_username, target_uuid,
        delta, prior_total, new_total, reason,
    )

    return ActionResult(
        target_uuid=target_uuid,
        target_username=canonical_username,
        prior_total=prior_total,
        new_total=new_total,
        triggered="none",
        dm_sent=False,
        blocklisted=False,
        suggested_dispatch=None,
    )


# ---------------------------------------------------------------------------
# Side-effect helpers
# ---------------------------------------------------------------------------


def _suggest_dispatch(actor_rank: Optional[str]) -> Optional[str]:
    """Map an in-game guild rank to the ``/gu ...`` command class that
    actor's vetsmod client should dispatch on a successful eject.

    Per spec: only chiefs can ``/gu kick``; captains and strategists work
    around this by demoting to ``recruit`` (the placeholder rank that
    signals "needs to be kicked" to chiefs).
    """
    if actor_rank is None:
        return None
    r = actor_rank.strip().lower()
    if r in ("chief", "owner"):
        return "kick"
    if r in ("strategist", "captain"):
        return "demote"
    return None


async def _deliver_dm(
    bot: "Bot",
    *,
    target_uuid: str,
    target_username: str,
    triggered: str,
    actor_username: str,
    message: Optional[str],
) -> bool:
    """Best-effort: DM the warned user via their linked Discord account.

    Returns ``False`` (without raising) when the target has no
    DiscordAccount link, when the linked account can't be fetched, or
    when DMs are closed. The caller logs the failure -- DM delivery is
    not a hard requirement for the audit row to be valid.
    """
    mc = await MinecraftAccount.filter(uuid=target_uuid).first()
    if mc is None:
        logger.info(
            "staff_action DM skipped: no MinecraftAccount for %s (%s)",
            target_username, target_uuid,
        )
        return False
    disc = await DiscordAccount.filter(minecraft_account=mc).first()
    if disc is None:
        logger.info(
            "staff_action DM skipped: %s (%s) has no linked Discord account",
            target_username, target_uuid,
        )
        return False
    try:
        user = await bot.fetch_user(int(disc.disc_uuid))
    except (ValueError, discord.HTTPException) as exc:
        logger.warning(
            "staff_action DM: could not fetch user %s for %s: %s",
            disc.disc_uuid, target_uuid, exc,
        )
        return False

    body = (message or "").strip()
    # Eject reuses the WARNING_PREFIX per spec; the additional context
    # (you've been added to the blocklist) is stitched on after.
    if triggered == "eject":
        suffix = (
            "\n\nThis incident also added you to the dazebot blocklist; "
            "you cannot rejoin the in-game guild without staff intervention."
        )
    else:
        suffix = ""
    if body:
        content = f"{WARNING_PREFIX} {body}\n\n_Issued by `{actor_username}`._{suffix}"
    else:
        content = f"{WARNING_PREFIX}\n\n_Issued by `{actor_username}`._{suffix}"
    return await dm_or_log(user, content, fallback_logger=logger)


async def _ensure_blocklisted(
    *,
    mc: MinecraftAccount,
    actor_uuid: str,
    actor_username: str,
    message: Optional[str],
) -> bool:
    """Add ``mc`` to the dazebot blocklist if not already present.
    Returns ``True`` iff this call inserted a new row.
    """
    existing = await Blocklist.filter(minecraft_account=mc).first()
    if existing is not None:
        return False
    reason = (
        f"ejected by {actor_username}" + (f": {message.strip()}" if message else "")
    )
    # Truncate to fit Blocklist.reason's 500-char column.
    reason = reason[:500]
    await Blocklist.create(
        minecraft_account=mc,
        reason=reason,
        # The Blocklist column is a Discord uuid; the actor's uuid is a
        # Minecraft uuid here. Leave null so it doesn't get mistaken for
        # a Discord id by /info or similar.
        blocked_by_disc_uuid=None,
    )
    return True


async def _post_eject_notice(
    bot: "Bot",
    *,
    actor_username: str,
    actor_rank: Optional[str],
    target_username: str,
    target_uuid: str,
    message: Optional[str],
) -> None:
    """Post an EJECT NOTICE to the moderation-log thread.

    Informational, no ping: this is "logging that an eject happened".
    The urgent "hop on and kick this person" alert -- the BAN NOTICE
    pinged in the rank-alert channel -- is fired separately by
    :func:`lib.rank_alerts.post_rank_alert` *only* for the
    captain/strategist case, when vetsmod observes the resulting
    ``/gu rank ... recruit`` rank change. Chief ejects don't generate
    a BAN NOTICE because the chief already kicks immediately; this
    log entry is the only record on the Discord side.
    """
    rank_blurb = f" ({actor_rank})" if actor_rank else ""
    msg_blurb = f"\nReason: {message.strip()}" if message else ""
    if _suggest_dispatch(actor_rank) == "kick":
        action_blurb = (
            f"{actor_username}{rank_blurb} ejected `{target_username}` "
            "and is dispatching `/gu kick` themselves."
        )
    else:
        action_blurb = (
            f"{actor_username}{rank_blurb} ejected `{target_username}` "
            "and is demoting them to recruit. A chief still needs to "
            "actually `/gu kick` them (a separate BAN notice will ping)."
        )
    content = (
        f"EJECT NOTICE\n"
        f"{action_blurb}\n"
        f"`{target_username}` (`{target_uuid}`) has been added to the "
        f"dazebot blocklist."
        f"{msg_blurb}"
    )
    await _post_to_moderation_thread(bot, content, label="eject notice")


async def _post_to_moderation_thread(
    bot: "Bot", content: str, *, label: str,
) -> None:
    """Resolve the moderation-log thread by ID and post ``content``.

    Pings are unconditionally suppressed -- this thread is for
    informational moderation logs, not for urgent attention. ``label``
    is used only in error logs so failures are diagnosable.
    """
    from lib.rank_alerts import MODERATION_LOG_THREAD_ID

    channel = bot.get_channel(MODERATION_LOG_THREAD_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(MODERATION_LOG_THREAD_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            logger.error(
                "%s: cannot fetch moderation thread %s: %s",
                label, MODERATION_LOG_THREAD_ID, exc,
            )
            return
    if not isinstance(channel, discord.abc.Messageable):
        logger.error(
            "%s: moderation thread %s is not messageable",
            label, MODERATION_LOG_THREAD_ID,
        )
        return
    try:
        await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        logger.error("%s: send failed: %s", label, exc)

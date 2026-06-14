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
from datetime import datetime, timedelta, timezone
import logging
from typing import TYPE_CHECKING, Iterable, Optional

import discord

from lib.mc.linking import dm_or_log
from lib.mc.resolve import ensure_mc_account
from orm import (
    Blocklist,
    DiscordAccount,
    MinecraftAccount,
    StaffActionEntry,
)

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger("dazebot.lib.staff.staff_actions")


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


# ---------------------------------------------------------------------------
# Decay
#
# Each caution point ages out independently. Issue an entry and its N
# points get ordinals (M+1, M+2, ..., M+N) where M is the player's live
# (non-expired) point count at the moment of issue; each ordinal x
# expires ``_expiry_days(x)`` days after the entry's ``created_at``.
# Sanity check: y(1)=21d, y(5)=182d, sum(y(1..5)) ~= 407d ("about a
# year" for 5 accumulated points, per spec).
#
# The replay walks an audit-log in chronological order. Negative-delta
# adjustments retire live points oldest-issued-first (most lenient, and
# matches the lowest-ordinal-expires-first natural ordering). The
# audit log is never mutated -- expired rows stay for history.
# ---------------------------------------------------------------------------


def _expiry_days(ordinal: int) -> float:
    """Decay curve for the *x*-th live point. ``y(x) = 21 * (26/3) ** ((x-1)/4)``."""
    return 21.0 * (26.0 / 3.0) ** ((ordinal - 1) / 4.0)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_aware(dt: datetime) -> datetime:
    # SQLite + tortoise's auto_now_add can return tz-naive datetimes; treat
    # them as UTC so timedelta arithmetic is sound.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@dataclass
class _LivePoint:
    issued_at: datetime
    expires_at: datetime
    entry_index: int


@dataclass(frozen=True)
class _ReplayResult:
    live_count: int
    next_expiry: Optional[datetime]
    per_entry_live: tuple[int, ...]


def _replay(entries: "Iterable[StaffActionEntry]", now: datetime) -> _ReplayResult:
    """Walk ``entries`` (must be chronological by ``created_at``) and
    return the live-point snapshot at ``now``.

    ``per_entry_live[i]`` carries how many of ``entries[i]``'s points
    are still alive at ``now`` -- used by the cog to dim fully-expired
    rows in the ``~warnings`` embed. Pure function; no I/O.
    """
    entries_list = list(entries)
    live: list[_LivePoint] = []

    for idx, e in enumerate(entries_list):
        created = _coerce_aware(e.created_at)
        # Drop points that expired before this entry happened.
        live = [lp for lp in live if lp.expires_at > created]

        delta = e.points
        if delta > 0:
            base = len(live)
            for i in range(delta):
                exp = created + timedelta(days=_expiry_days(base + i + 1))
                live.append(_LivePoint(created, exp, idx))
        elif delta < 0:
            # Retire oldest-issued first (user spec). Stable sort so
            # synthetic same-instant entries follow insertion order.
            live.sort(key=lambda lp: lp.issued_at)
            del live[: min(-delta, len(live))]

    live = [lp for lp in live if lp.expires_at > now]

    per_entry = [0] * len(entries_list)
    for lp in live:
        per_entry[lp.entry_index] += 1

    next_expiry = min((lp.expires_at for lp in live), default=None)
    return _ReplayResult(
        live_count=len(live),
        next_expiry=next_expiry,
        per_entry_live=tuple(per_entry),
    )


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

    Thin wrapper over :func:`lib.resolve.ensure_mc_account` -- exists to
    preserve the tuple shape this module's callers expect. Raises
    :class:`WynnApiError` (or other Exceptions for non-WAPI failures) if
    the name cannot be resolved.
    """
    mc = await ensure_mc_account(name_or_uuid)
    return mc.uuid, mc.mc_username, mc


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def total_points_for(target_uuid: str) -> int:
    """Live caution-point total for ``target_uuid`` at the current
    moment. ``0`` if every entry has fully decayed (or the target has
    no entries).

    Replays the player's audit log through ``_replay`` and applies the
    decay curve ``y(x) = 21 * (26/3)^((x-1)/4)`` days per point. Expired
    rows remain in the table for the audit trail but contribute 0.
    """
    rows = await (
        StaffActionEntry.filter(target_uuid=target_uuid).order_by("created_at")
    )
    return _replay(rows, _utc_now()).live_count


async def live_breakdown(
    target_uuid: str, *, limit: int = 25
) -> tuple[int, Optional[datetime], list[tuple[StaffActionEntry, int]]]:
    """One-shot snapshot for the ``~warnings`` embed and the GET
    endpoint: live count, soonest unexpired ``expires_at`` (or ``None``
    if no live points), and a most-recent-first list of
    ``(entry, live_points_of_this_entry)`` pairs truncated to ``limit``.

    A pair's second element being ``0`` means every point that entry
    contributed has already decayed -- callers render those rows dimly
    but keep them in the list (the audit trail is the whole point).
    """
    rows = list(
        await StaffActionEntry.filter(target_uuid=target_uuid).order_by("created_at")
    )
    result = _replay(rows, _utc_now())
    paired = list(zip(rows, result.per_entry_live))
    paired.reverse()
    return result.live_count, result.next_expiry, paired[:limit]


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
    live point total by ``delta`` (positive or negative).

    Negative deltas retire up to ``|delta|`` of the player's currently
    live points (oldest-issued first; see ``_replay``). If the player
    has fewer live points than ``|delta|`` the excess is dropped --
    live count can't go below zero, so the negative-balance "buffer"
    admins could previously rely on no longer exists.

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
    # Live count can't go below zero -- negative deltas clamp to 0 once
    # all live points have been retired. The audit row still records the
    # raw ``delta`` so the history shows what the admin actually typed.
    new_total = max(0, prior_total + delta)

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
    from lib.staff.rank_alerts import MODERATION_LOG_THREAD_ID

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

"""Week 2: thread ↔ role mirror.

A single Discord thread is the source of truth for a single Discord role.
Joining the thread grants the role; leaving the thread strips it. The
gateway events keep the two sets in sync at runtime; a backfill at boot
reconciles any drift from events the bot missed while offline.

``/return 2`` is a status reporter — shows the thread and role sizes and
the current drift in each direction. No mutating UI lives here; if the
sync ever needs a manual kick, the backfill function is exported and can
be invoked directly.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from cogs.events.returns import Tier, register

logger = logging.getLogger("dazebot.cogs.events.returns.week_2")

THREAD_ID = 1511066769421373451
ROLE_ID = 1511049558560608347


async def _resolve_thread(bot: discord.Client) -> Optional[discord.Thread]:
    ch = bot.get_channel(THREAD_ID)
    if isinstance(ch, discord.Thread):
        return ch
    try:
        ch = await bot.fetch_channel(THREAD_ID)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logger.warning("week_2 thread %s not fetchable: %s", THREAD_ID, e)
        return None
    return ch if isinstance(ch, discord.Thread) else None


async def _thread_member_ids(thread: discord.Thread) -> Optional[set[int]]:
    try:
        members = await thread.fetch_members()
    except discord.HTTPException as e:
        logger.warning("week_2 fetch_members on thread %s failed: %s", THREAD_ID, e)
        return None
    return {tm.id for tm in members}


async def _resolve_member(
    guild: discord.Guild, user_id: int
) -> Optional[discord.Member]:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None
    except discord.HTTPException as e:
        logger.warning("week_2 fetch_member(%s) failed: %s", user_id, e)
        return None


async def sync_thread_role(bot: discord.Client) -> None:
    """Idempotent two-way backfill. Adds the role to thread members missing
    it and strips it from holders not in the thread.
    """
    thread = await _resolve_thread(bot)
    if thread is None:
        logger.warning("week_2 sync skipped: thread %s unresolved", THREAD_ID)
        return
    guild = thread.guild
    role = guild.get_role(ROLE_ID)
    if role is None:
        logger.warning(
            "week_2 sync skipped: role %s not in guild %s", ROLE_ID, guild.id
        )
        return

    thread_ids = await _thread_member_ids(thread)
    if thread_ids is None:
        return
    role_holder_ids = {m.id for m in role.members}

    to_add = thread_ids - role_holder_ids
    to_remove = role_holder_ids - thread_ids

    added = removed = failed = missing = 0
    for uid in to_add:
        member = await _resolve_member(guild, uid)
        if member is None:
            missing += 1
            continue
        try:
            await member.add_roles(role, reason="week_2 backfill: in thread")
            added += 1
        except discord.HTTPException as e:
            logger.warning("week_2 backfill add %s failed: %s", uid, e)
            failed += 1

    for uid in to_remove:
        member = await _resolve_member(guild, uid)
        if member is None:
            missing += 1
            continue
        try:
            await member.remove_roles(role, reason="week_2 backfill: not in thread")
            removed += 1
        except discord.HTTPException as e:
            logger.warning("week_2 backfill remove %s failed: %s", uid, e)
            failed += 1

    logger.info(
        "week_2 sync: thread=%d role=%d added=%d removed=%d failed=%d missing=%d",
        len(thread_ids), len(role_holder_ids), added, removed, failed, missing,
    )


# ---------------------------------------------------------------------------
# Gateway events — keep the role in step with thread membership
# ---------------------------------------------------------------------------


async def _on_thread_member_join(tm: discord.ThreadMember) -> None:
    if tm.thread_id != THREAD_ID:
        return
    thread = tm.thread
    if thread is None:
        return
    role = thread.guild.get_role(ROLE_ID)
    if role is None:
        return
    member = await _resolve_member(thread.guild, tm.id)
    if member is None or role in member.roles:
        return
    try:
        await member.add_roles(role, reason="week_2: joined thread")
    except discord.HTTPException as e:
        logger.warning("week_2 add on join %s failed: %s", tm.id, e)


async def _on_thread_member_remove(tm: discord.ThreadMember) -> None:
    if tm.thread_id != THREAD_ID:
        return
    thread = tm.thread
    if thread is None:
        return
    role = thread.guild.get_role(ROLE_ID)
    if role is None:
        return
    member = await _resolve_member(thread.guild, tm.id)
    if member is None or role not in member.roles:
        return
    try:
        await member.remove_roles(role, reason="week_2: left thread")
    except discord.HTTPException as e:
        logger.warning("week_2 remove on leave %s failed: %s", tm.id, e)


def register_listeners(bot: commands.Bot) -> None:
    bot.add_listener(_on_thread_member_join, name="on_thread_member_join")
    bot.add_listener(_on_thread_member_remove, name="on_thread_member_remove")


# ---------------------------------------------------------------------------
# /return 2 — status reporter
# ---------------------------------------------------------------------------


@register(2)
async def handle(ctx: commands.Context) -> None:
    thread = await _resolve_thread(ctx.bot)
    if thread is None:
        await ctx.reply(f"Thread `{THREAD_ID}` is not resolvable.", ephemeral=True)
        return
    role = thread.guild.get_role(ROLE_ID)
    if role is None:
        await ctx.reply(
            f"Role `{ROLE_ID}` is not in guild `{thread.guild.id}`.", ephemeral=True
        )
        return

    thread_ids = await _thread_member_ids(thread)
    if thread_ids is None:
        await ctx.reply(
            "Couldn't fetch thread members right now — try again in a bit.",
            ephemeral=True,
        )
        return
    role_holder_ids = {m.id for m in role.members}
    missing_role = thread_ids - role_holder_ids
    extra_role = role_holder_ids - thread_ids

    lines = [
        "**Return 2 — thread ↔ role mirror**",
        f"Thread: <#{THREAD_ID}> ({len(thread_ids)} members)",
        f"Role: {role.mention} ({len(role_holder_ids)} holders)",
        (
            f"Drift: {len(missing_role)} in thread without the role, "
            f"{len(extra_role)} role holders not in the thread."
        ),
    ]
    await ctx.reply(
        "\n".join(lines),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )

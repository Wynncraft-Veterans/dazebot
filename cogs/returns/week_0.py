"""Week 0: cults.

Three actions, dispatched off ``action`` in the kwargs forwarded by
``cogs/return_cmd.py``:

* ``join <cult>``        REGISTERED — switch the caller's active cult.
* ``add <cult> <owner>`` ADMIN      — create a new cult with an MC figurehead.
                                      ``owner`` may be a Discord mention/id,
                                      an MC username, or an MC UUID.
* ``list <cult>``        REGISTERED — print figurehead, staff, and members.

Cult names are stored lowercased; ``unique=True`` on the column doubles as
the case-insensitive guard. Mutual exclusivity is enforced by the
``unique=True`` on ``CultMembership.discord_account``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands
from tortoise.expressions import Q

from config import CurrConfig
from cogs.returns import register
from lib.auth import (
    _has_admin_perm,
    _has_registered_role,
    _has_staff_role,
    _is_operator,
)
from lib.converters import CaseInsensitiveMember
from lib.wynn_api.errors import WynnApiError
from lib.wynn_api.player import get_player_stats
from orm import (
    Cult,
    CultMembership,
    DiscordAccount,
    MinecraftAccount,
    UNKNOWN_LAST_ONLINE,
)

logger = logging.getLogger("dazebot.cogs.returns.week_0")


# Cult name (lowercased) -> private thread id under channel 1313786225735237654.
# Bot needs Manage Messages (or Manage Threads) on the parent to add/remove
# non-invited members on private threads.
CULT_THREADS: dict[str, int] = {
    "wencult":    1501233308284092546,
    "deercult":   1501233117829140480,
    "nazcult":    1501233190285738115,
    "fishcult":   1501232813943292026,
    "brycult":    1501233371802767420,
    "xandercult": 1501233419135221860,
}


async def _resolve_thread(bot, thread_id: int) -> Optional[discord.Thread]:
    ch = bot.get_channel(thread_id)
    if isinstance(ch, discord.Thread):
        return ch
    try:
        ch = await bot.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logger.warning("cult thread %s not fetchable: %s", thread_id, e)
        return None
    return ch if isinstance(ch, discord.Thread) else None


async def _add_to_cult_thread(bot, cult_name: str, discord_id: int) -> None:
    thread_id = CULT_THREADS.get(cult_name.lower())
    if thread_id is None:
        logger.debug("no thread mapped for cult %r; skipping add", cult_name)
        return
    thread = await _resolve_thread(bot, thread_id)
    if thread is None:
        return
    try:
        await thread.add_user(discord.Object(id=discord_id))
    except discord.HTTPException as e:
        logger.warning("add_user(%s) on thread %s failed: %s", discord_id, thread_id, e)
        return
    try:
        await thread.send(
            f"<@{discord_id}> joined {cult_name}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    except discord.HTTPException as e:
        logger.warning("welcome msg in thread %s failed: %s", thread_id, e)


async def _remove_from_cult_thread(bot, cult_name: str, discord_id: int) -> None:
    thread_id = CULT_THREADS.get(cult_name.lower())
    if thread_id is None:
        return
    thread = await _resolve_thread(bot, thread_id)
    if thread is None:
        return
    try:
        await thread.remove_user(discord.Object(id=discord_id))
    except discord.HTTPException as e:
        # 404 here is normal (user wasn't in the thread); log at debug.
        if isinstance(e, discord.NotFound):
            logger.debug("remove_user(%s) on thread %s: not a member", discord_id, thread_id)
        else:
            logger.warning("remove_user(%s) on thread %s failed: %s", discord_id, thread_id, e)


async def backfill_cult_threads(bot) -> None:
    """Walk every CultMembership row and ensure each member is in their cult's
    thread. Idempotent: re-adding a member is a no-op on Discord's side.

    Figureheads are excluded everywhere: any stale CultMembership row whose
    owner is the figurehead of *any* cult is deleted, and we sweep each
    figurehead out of every cult thread in case they were added before this
    rule existed.
    """
    figurehead_mc_ids = set(await Cult.all().values_list("owner_id", flat=True))

    # Snapshot each thread's current membership once. The thread-members
    # rate-limit bucket is per-channel, so blind PUTs/DELETEs for state
    # that already matches reality were eating 429s. Diff against this set
    # and only write when something actually changes.
    thread_state: dict[str, tuple[Optional[discord.Thread], set[int]]] = {}
    for cn, tid in CULT_THREADS.items():
        thread = await _resolve_thread(bot, tid)
        if thread is None:
            thread_state[cn] = (None, set())
            continue
        try:
            members = await thread.fetch_members()
            thread_state[cn] = (thread, {tm.id for tm in members})
        except discord.HTTPException as e:
            logger.warning(
                "backfill: fetch_members on thread %s (%s) failed: %s", tid, cn, e,
            )
            thread_state[cn] = (thread, set())

    rows = await CultMembership.all().prefetch_related("cult", "discord_account")
    added = failed = skipped = pruned = already = 0
    for m in rows:
        mc_id = m.discord_account.minecraft_account_id
        if mc_id is not None and mc_id in figurehead_mc_ids:
            await m.delete()
            pruned += 1
            continue
        try:
            disc_id = int(m.discord_account.disc_uuid)
        except ValueError:
            skipped += 1
            continue
        cult_name = m.cult.name
        state = thread_state.get(cult_name.lower())
        if state is None:
            skipped += 1
            continue
        thread, members = state
        if thread is None:
            failed += 1
            continue
        if disc_id in members:
            already += 1
            continue
        try:
            await thread.add_user(discord.Object(id=disc_id))
            members.add(disc_id)
            added += 1
        except discord.HTTPException as e:
            logger.warning(
                "backfill: add_user(%s) on thread %s (%s) failed: %s",
                disc_id, thread.id, cult_name, e,
            )
            failed += 1

    swept = 0
    if figurehead_mc_ids:
        figurehead_discs = await DiscordAccount.filter(
            minecraft_account_id__in=list(figurehead_mc_ids)
        )
        figurehead_disc_ids: set[int] = set()
        for disc in figurehead_discs:
            try:
                figurehead_disc_ids.add(int(disc.disc_uuid))
            except ValueError:
                continue
        for cn, (thread, members) in thread_state.items():
            if thread is None:
                continue
            for fdid in figurehead_disc_ids & members:
                try:
                    await thread.remove_user(discord.Object(id=fdid))
                    members.discard(fdid)
                    swept += 1
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    logger.debug(
                        "figurehead sweep remove(%s, %s) failed: %s", fdid, cn, e,
                    )

    logger.info(
        "cult thread backfill: total=%d added=%d already=%d failed=%d skipped=%d "
        "pruned_figureheads=%d swept_figurehead_thread_memberships=%d",
        len(rows), added, already, failed, skipped, pruned, swept,
    )


def _is_admin_or_higher(user: discord.abc.User) -> bool:
    return _is_operator(user) or _has_admin_perm(user)


def _is_registered_or_higher(user: discord.abc.User) -> bool:
    return (
        _is_operator(user)
        or _has_admin_perm(user)
        or _has_staff_role(user)
        or _has_registered_role(user)
    )


async def _resolve_owner_mc(ctx: commands.Context, owner: str) -> Optional[MinecraftAccount]:
    """Resolve ``owner`` to a MinecraftAccount.

    Tries: Discord mention/id/name → linked MC account, then MC username/UUID
    lookup, then a Wynncraft API fetch (creating the row) for unknown
    usernames. Returns ``None`` only if the value can't be resolved at all.
    """
    if ctx.guild is not None:
        try:
            member = await CaseInsensitiveMember().convert(ctx, owner)
        except commands.MemberNotFound:
            member = None
        if member is not None:
            disc = await DiscordAccount.filter(disc_uuid=str(member.id)).first()
            if disc is None or disc.minecraft_account_id is None:
                return None
            return await MinecraftAccount.get(id=disc.minecraft_account_id)

    mc = await MinecraftAccount.filter(
        Q(uuid=owner)
        | Q(mc_username__iexact=owner)
        | Q(wynn_username__iexact=owner)
    ).first()
    if mc is not None:
        return mc

    try:
        fs = await get_player_stats(owner, full=True)
    except WynnApiError as e:
        logger.info("week_0 add: Wynn API lookup failed for %r: %s", owner, e)
        return None
    return await MinecraftAccount.create(
        uuid=fs.uuid,
        wynn_username=fs.username,
        mc_username=fs.username,
        last_online=fs.lastJoin or UNKNOWN_LAST_ONLINE,
        last_manual_check=UNKNOWN_LAST_ONLINE,
        first_join=fs.firstJoin,
    )


async def _do_add(ctx: commands.Context, cult_name: str, owner: str) -> None:
    if not _is_admin_or_higher(ctx.author):
        await ctx.reply("You need Administrator to add a cult.", ephemeral=True)
        return

    name_key = cult_name.lower()
    if await Cult.filter(name=name_key).exists():
        await ctx.reply(f"A cult named `{cult_name}` already exists.", ephemeral=True)
        return

    mc = await _resolve_owner_mc(ctx, owner)
    if mc is None:
        await ctx.reply(
            f"Could not resolve owner `{owner}` to a Minecraft account "
            "(tried Discord mention, MC username, and MC UUID).",
            ephemeral=True,
        )
        return

    await Cult.create(name=name_key, owner=mc)
    await ctx.reply(f"✅ Created cult `{cult_name}` with figurehead `{mc.mc_username}`.")


# Self-switch gate: a user can move themselves between cults at most once
# every SELF_SWITCH_COOLDOWN. Force-moves by staff bypass this entirely.
SELF_SWITCH_COOLDOWN = timedelta(days=180)


async def _switch_membership(
    bot,
    disc: DiscordAccount,
    discord_id: int,
    cult: Cult,
    existing: Optional[CultMembership],
) -> Optional[str]:
    """Apply a cult switch: rewrite the CultMembership row and sync threads.
    Returns the previous cult's name (or ``None`` if first-time join).
    """
    prev_cult_name: Optional[str] = None
    if existing is not None:
        prev_cult = await Cult.get(id=existing.cult_id)
        prev_cult_name = prev_cult.name
        await existing.delete()
    await CultMembership.create(cult=cult, discord_account=disc)

    if prev_cult_name and prev_cult_name != cult.name:
        await _remove_from_cult_thread(bot, prev_cult_name, discord_id)
    await _add_to_cult_thread(bot, cult.name, discord_id)
    return prev_cult_name


async def do_join_by_name(ctx: commands.Context, cult_name: str) -> None:
    """Public entry-point reused by the `/joincult` shortcut."""
    await _do_join(ctx, cult_name)


async def _do_join(ctx: commands.Context, cult_name: str) -> None:
    if not _is_registered_or_higher(ctx.author):
        await ctx.reply("You need to be registered to join a cult.", ephemeral=True)
        return

    cult = await Cult.filter(name=cult_name.lower()).first()
    if cult is None:
        await ctx.reply(f"No cult named `{cult_name}`.", ephemeral=True)
        return

    disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(ctx.author.id))

    if disc.minecraft_account_id is not None:
        owned = await Cult.filter(owner_id=disc.minecraft_account_id).first()
        if owned is not None:
            await ctx.reply(
                f"You are the figurehead of `{owned.name}` — figureheads can't join cult threads.",
                ephemeral=True,
            )
            return

    existing = await CultMembership.filter(discord_account=disc).first()
    if existing is not None:
        if existing.cult_id == cult.id:
            await ctx.reply(f"You are already in `{cult_name}`.", ephemeral=True)
            return
        elapsed = datetime.now(timezone.utc) - existing.joined_at
        if elapsed < SELF_SWITCH_COOLDOWN:
            remaining = SELF_SWITCH_COOLDOWN - elapsed
            days = max(1, remaining.days + (1 if remaining.seconds else 0))
            await ctx.reply(
                f"You can switch cults again in ~{days} day(s). "
                "Ask staff for a force-move if you need it sooner.",
                ephemeral=True,
            )
            return

    await _switch_membership(ctx.bot, disc, ctx.author.id, cult, existing)
    await ctx.reply(f"✅ Joined `{cult_name}`.")


async def do_force_join_by_name(
    ctx: commands.Context,
    target: discord.Member,
    cult_name: str,
) -> None:
    """Staff entry-point: move ``target`` to ``cult_name`` regardless of
    cooldown. Figurehead exclusion is still enforced because that rule is
    permanent, not a cooldown.
    """
    if not (_is_operator(ctx.author) or _has_admin_perm(ctx.author) or _has_staff_role(ctx.author)):
        await ctx.reply("You need staff to force-move someone's cult.", ephemeral=True)
        return

    cult = await Cult.filter(name=cult_name.lower()).first()
    if cult is None:
        await ctx.reply(f"No cult named `{cult_name}`.", ephemeral=True)
        return

    disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(target.id))

    if disc.minecraft_account_id is not None:
        owned = await Cult.filter(owner_id=disc.minecraft_account_id).first()
        if owned is not None:
            await ctx.reply(
                f"{target.mention} is the figurehead of `{owned.name}`; "
                "figureheads can't be placed in any cult's thread.",
                ephemeral=True,
            )
            return

    existing = await CultMembership.filter(discord_account=disc).first()
    if existing is not None and existing.cult_id == cult.id:
        await ctx.reply(f"{target.mention} is already in `{cult_name}`.", ephemeral=True)
        return

    prev = await _switch_membership(ctx.bot, disc, target.id, cult, existing)
    note = f"from `{prev}` " if prev else ""
    await ctx.reply(f"✅ Force-moved {target.mention} {note}to `{cult_name}`.")


async def _do_announce(ctx: commands.Context, message: str) -> None:
    if not _is_admin_or_higher(ctx.author):
        await ctx.reply("You need Administrator to broadcast to cult threads.", ephemeral=True)
        return

    await ctx.defer(ephemeral=True)
    # Slash-modal text fields can't carry actual newlines, so admins can type
    # the literal two characters \n where they want a line break.
    text = message.replace("\\n", "\n")
    sent = failed = missing = 0
    for cult_name, thread_id in CULT_THREADS.items():
        thread = await _resolve_thread(ctx.bot, thread_id)
        if thread is None:
            missing += 1
            continue
        try:
            await thread.send(
                text,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            sent += 1
        except discord.HTTPException as e:
            logger.warning("announce: send to thread %s (%s) failed: %s", thread_id, cult_name, e)
            failed += 1

    await ctx.reply(
        f"✅ Sent to {sent} thread(s). ({failed} failed, {missing} unresolved)",
        ephemeral=True,
    )


async def _username_for_disc(bot, disc: DiscordAccount) -> str:
    """In-game username for a cult member, or a best-effort Discord fallback
    if they're not linked.
    """
    if disc.minecraft_account_id is not None:
        mc = await MinecraftAccount.get(id=disc.minecraft_account_id)
        return mc.mc_username
    try:
        user = await bot.fetch_user(int(disc.disc_uuid))
        return f"@{user.name} (unlinked)"
    except (ValueError, discord.HTTPException):
        return f"<{disc.disc_uuid}> (unlinked)"


async def _do_list(ctx: commands.Context, cult_name: str) -> None:
    if not _is_registered_or_higher(ctx.author):
        await ctx.reply("You need to be registered to list a cult.", ephemeral=True)
        return

    cult = await Cult.filter(name=cult_name.lower()).prefetch_related("owner").first()
    if cult is None:
        await ctx.reply(f"No cult named `{cult_name}`.", ephemeral=True)
        return

    memberships = await CultMembership.filter(cult=cult).prefetch_related("discord_account")

    staff_names: list[str] = []
    member_names: list[str] = []
    guild = ctx.guild
    staff_role_id = CurrConfig.STAFF_ROLE
    for m in memberships:
        disc = m.discord_account
        username = await _username_for_disc(ctx.bot, disc)
        is_staff = False
        if guild is not None:
            try:
                discord_id = int(disc.disc_uuid)
            except ValueError:
                discord_id = None
            if discord_id is not None:
                member = guild.get_member(discord_id)
                if member is not None and any(r.id == staff_role_id for r in member.roles):
                    is_staff = True
        if is_staff:
            staff_names.append(username)
        else:
            member_names.append(username)

    figurehead = cult.owner.mc_username
    title = cult_name.capitalize()

    def fmt(names: list[str]) -> str:
        return ", ".join(f"`{n}`" for n in names) if names else "(none)"

    lines = [
        f"# {title} Members:",
        f"Figurehead: `{figurehead}`",
        f"Staff: {fmt(staff_names)}",
        f"Members: {fmt(member_names)}",
    ]
    await ctx.reply("\n".join(lines))


@register(0)
async def handle(
    ctx: commands.Context,
    *,
    flag: bool = False,
    action: Optional[str] = None,
    cult: Optional[str] = None,
    owner: Optional[str] = None,
    target: Optional[discord.Member] = None,
    message: Optional[str] = None,
    **kwargs,
) -> None:
    if action is None:
        await ctx.reply(
            "Usage: `/return 0 <join|add|list|force|announce> [cult] [owner|target|message]`",
            ephemeral=True,
        )
        return

    action = action.lower()

    if action == "announce":
        if not message:
            await ctx.reply("`announce` requires a `message`.", ephemeral=True)
            return
        await _do_announce(ctx, message)
        return

    if not cult:
        await ctx.reply(f"`{action}` requires a cult name.", ephemeral=True)
        return

    if action == "join":
        await _do_join(ctx, cult)
    elif action == "add":
        if not owner:
            await ctx.reply("`add` requires an owner (Discord mention, MC username, or MC UUID).", ephemeral=True)
            return
        await _do_add(ctx, cult, owner)
    elif action == "list":
        await _do_list(ctx, cult)
    elif action == "force":
        if target is None:
            await ctx.reply("`force` requires a `target` user.", ephemeral=True)
            return
        await do_force_join_by_name(ctx, target, cult)
    else:
        await ctx.reply(
            f"Unknown action `{action}`. Use `join`, `add`, `list`, `force`, or `announce`.",
            ephemeral=True,
        )

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
from lib.wynn_api.player import get_player_full_stats
from orm import (
    Cult,
    CultMembership,
    DiscordAccount,
    MinecraftAccount,
    UNKNOWN_LAST_ONLINE,
)

logger = logging.getLogger("dazebot.cogs.returns.week_0")


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
        fs = await get_player_full_stats(owner)
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

    existing = await CultMembership.filter(discord_account=disc).first()
    if existing is not None:
        if existing.cult_id == cult.id:
            await ctx.reply(f"You are already in `{cult_name}`.", ephemeral=True)
            return
        await existing.delete()
    await CultMembership.create(cult=cult, discord_account=disc)

    await ctx.reply(f"✅ Joined `{cult_name}`.")


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
        member_names.append(username)
        if guild is not None:
            try:
                discord_id = int(disc.disc_uuid)
            except ValueError:
                continue
            member = guild.get_member(discord_id)
            if member is not None and any(r.id == staff_role_id for r in member.roles):
                staff_names.append(username)

    figurehead = cult.owner.mc_username
    title = cult_name.capitalize()
    lines = [
        f"# {title} Members:",
        f"Figurehead: {figurehead}",
        f"Staff: {', '.join(staff_names) if staff_names else '(none)'}",
        f"Members: {', '.join(member_names) if member_names else '(none)'}",
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
    **kwargs,
) -> None:
    if action is None:
        await ctx.reply(
            "Usage: `/return 0 <join|add|list> <cult> [owner]`",
            ephemeral=True,
        )
        return

    action = action.lower()
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
    else:
        await ctx.reply(f"Unknown action `{action}`. Use `join`, `add`, or `list`.", ephemeral=True)

"""Apartment cog: the ``~apartment`` group (info / assign / force / evict /
create / status).

The seed import lives separately in
``../vets-deploy/scripts/one-off/seed-dazebot-apartments.sh``; this cog is
pure CRUD over the resulting ``apartments`` table.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

import discord
from discord.ext import commands

from bot import Bot
from lib.auth import is_admin, is_registered, is_staff
from lib.discord_utils.converters import CaseInsensitiveMember
from lib.mc.resolve import resolve_mc_account_loose
from orm import Apartment, DiscordAccount

logger = logging.getLogger("dazebot.cogs.community.apartment")


async def _resolve_owner_name(ctx: commands.Context, value: str) -> str:
    """Resolve ``value`` into the canonical ``mc_username`` to store as
    ``Apartment.owner_mc_username``.

    Cascade: Discord member (mention/id/case-insensitive name) → their
    linked MinecraftAccount.mc_username → loose MC lookup against the local
    DB → fall back to the raw value.

    Falls back to the raw input on a miss because the seed data includes
    names that aren't (yet) linked to a Discord account or known to the
    Wynncraft API — refusing the command on every unknown name would block
    legitimate staff assignments of brand-new players.
    """
    try:
        member = await CaseInsensitiveMember().convert(ctx, value)
    except commands.MemberNotFound:
        member = None
    if member is not None:
        disc = (
            await DiscordAccount.filter(disc_uuid=str(member.id))
            .select_related("minecraft_account")
            .first()
        )
        if disc and disc.minecraft_account:
            return disc.minecraft_account.mc_username
    mc = await resolve_mc_account_loose(value)
    if mc is not None:
        return mc.mc_username
    return value


def _owned_by(apartment: Apartment, mc_username: str) -> bool:
    """Case-insensitive owner match."""
    if apartment.owner_mc_username is None:
        return False
    return apartment.owner_mc_username.lower() == mc_username.lower()


def _format_row(a: Apartment) -> str:
    owner = a.owner_mc_username if a.owner_mc_username else "VACANT"
    return f"`{a.number}` — {owner}"


class ApartmentCog(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Apartment cog initialized")

    @commands.hybrid_group(name="apartment", description="Returners apartment management.")
    async def apartment_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply(
                "Use `~apartment info <username/number>` (registered), "
                "`~apartment assign <username>` (staff), "
                "`~apartment force <username> <number>` (staff), "
                "`~apartment evict <username/number>` (staff), "
                "`~apartment create <number>` (admin), "
                "or `~apartment status` (staff)."
            )

    @apartment_group.command(
        name="info",
        description="(Registered) Look up an apartment by room number or owner.",
    )
    @is_registered()
    async def apartment_info(self, ctx: commands.Context, *, arg: str):
        by_number = await Apartment.filter(number__iexact=arg).first()
        if by_number is not None:
            await ctx.reply(_format_row(by_number), allowed_mentions=discord.AllowedMentions.none())
            return

        owner_name = await _resolve_owner_name(ctx, arg)
        by_owner = await Apartment.filter(owner_mc_username__iexact=owner_name).first()
        if by_owner is not None:
            await ctx.reply(_format_row(by_owner), allowed_mentions=discord.AllowedMentions.none())
            return

        await ctx.reply(
            f"No apartment matches `{arg}` (tried both room number and owner).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @apartment_group.command(
        name="assign",
        description="(Staff) Assign a random vacant apartment to the user.",
    )
    @is_staff()
    async def apartment_assign(self, ctx: commands.Context, *, username: str):
        owner = await _resolve_owner_name(ctx, username)

        existing = await Apartment.filter(owner_mc_username__iexact=owner).first()
        if existing is not None:
            await ctx.reply(
                f"`{owner}` already owns `{existing.number}`. Use "
                f"`~apartment force {owner} <number>` to move them, or "
                f"`~apartment evict {owner}` first.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        vacant = await Apartment.filter(owner_mc_username__isnull=True).all()
        if not vacant:
            await ctx.reply(
                "No vacant apartments. Use `~apartment create <number>` to add one.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        target = random.choice(vacant)
        target.owner_mc_username = owner
        await target.save(update_fields=["owner_mc_username", "updated_at"])
        logger.info("apartment assign: %s -> %s by %s", owner, target.number, ctx.author)
        await ctx.reply(
            f"✅ Assigned `{owner}` to apartment `{target.number}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @apartment_group.command(
        name="force",
        description="(Staff) Force-assign a user to a specific room (displaces current occupant).",
    )
    @is_staff()
    async def apartment_force(self, ctx: commands.Context, username: str, number: str):
        owner = await _resolve_owner_name(ctx, username)

        target = await Apartment.filter(number__iexact=number).first()
        if target is None:
            await ctx.reply(
                f"No apartment `{number}` exists. Use `~apartment create {number}` first.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # If they already own this exact room, no-op.
        if _owned_by(target, owner):
            await ctx.reply(
                f"`{owner}` already owns `{target.number}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # Vacate any other apartments owned by the target so they end up
        # owning exactly this one. Excludes the destination row in case
        # casing differs.
        prior = await Apartment.filter(owner_mc_username__iexact=owner).exclude(id=target.id).all()
        for p in prior:
            p.owner_mc_username = None
            await p.save(update_fields=["owner_mc_username", "updated_at"])
            logger.info("apartment force: vacated prior %s of %s", p.number, owner)

        displaced = target.owner_mc_username  # may be None
        target.owner_mc_username = owner
        await target.save(update_fields=["owner_mc_username", "updated_at"])
        logger.info(
            "apartment force: %s -> %s by %s (displaced: %s, prior vacated: %s)",
            owner, target.number, ctx.author, displaced,
            [p.number for p in prior],
        )

        bits = [f"✅ Forced `{owner}` into apartment `{target.number}`."]
        if displaced:
            bits.append(f"Displaced previous owner `{displaced}` (now without an apartment).")
        if prior:
            bits.append(f"Vacated their prior apartment(s): {', '.join(f'`{p.number}`' for p in prior)}.")
        await ctx.reply(" ".join(bits), allowed_mentions=discord.AllowedMentions.none())

    @apartment_group.command(
        name="evict",
        description="(Staff) Vacate an apartment by room number or owner.",
    )
    @is_staff()
    async def apartment_evict(self, ctx: commands.Context, *, arg: str):
        by_number = await Apartment.filter(number__iexact=arg).first()
        if by_number is not None:
            prior_owner = by_number.owner_mc_username
            if prior_owner is None:
                await ctx.reply(
                    f"Apartment `{by_number.number}` is already vacant.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            by_number.owner_mc_username = None
            await by_number.save(update_fields=["owner_mc_username", "updated_at"])
            logger.info("apartment evict by number: %s (was %s) by %s", by_number.number, prior_owner, ctx.author)
            await ctx.reply(
                f"✅ Evicted `{prior_owner}` from `{by_number.number}` (now vacant).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        owner = await _resolve_owner_name(ctx, arg)
        by_owner = await Apartment.filter(owner_mc_username__iexact=owner).first()
        if by_owner is None:
            await ctx.reply(
                f"No apartment matches `{arg}` (tried both room number and owner).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        room_number = by_owner.number
        by_owner.owner_mc_username = None
        await by_owner.save(update_fields=["owner_mc_username", "updated_at"])
        logger.info("apartment evict by owner: %s from %s by %s", owner, room_number, ctx.author)
        await ctx.reply(
            f"✅ Evicted `{owner}` from `{room_number}` (now vacant).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @apartment_group.command(
        name="create",
        description="(Admin) Create a new vacant apartment with the given room number.",
    )
    @is_admin()
    async def apartment_create(self, ctx: commands.Context, number: str):
        if await Apartment.filter(number__iexact=number).exists():
            await ctx.reply(
                f"Apartment `{number}` already exists.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await Apartment.create(number=number, owner_mc_username=None)
        logger.info("apartment create: %s by %s", number, ctx.author)
        await ctx.reply(
            f"✅ Created vacant apartment `{number}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @apartment_group.command(
        name="status",
        description="(Staff) Show apartment counts (total + vacant).",
    )
    @is_staff()
    async def apartment_status(self, ctx: commands.Context):
        total = await Apartment.all().count()
        vacant = await Apartment.filter(owner_mc_username__isnull=True).count()
        occupied = total - vacant
        await ctx.reply(
            f"**Apartments**: {total} total — {occupied} occupied, {vacant} vacant.",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: Bot):
    await bot.add_cog(ApartmentCog(bot))
    logger.info("Apartment cog loaded successfully")

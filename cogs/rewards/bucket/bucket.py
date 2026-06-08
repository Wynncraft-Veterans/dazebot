"""The ``~bucket`` cog: staff-awarded prize-claim tokens in six tiers.

A "bucket pull" is one IOU for a future prize, awarded by staff for
placements in returns and similar events. Users see their outstanding
pulls via ``~bucket check``; staff redeem them in person and then mark
the row consumed via ``~bucket redeem``.

This cog is a thin shim like :mod:`cogs.rewards.ctp.ctp`: every command
resolves the target user, dispatches to :mod:`cogs.rewards.bucket.lib.svc`,
and renders the result via :mod:`cogs.rewards.bucket.lib.format`.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from bot import Bot
from cogs.rewards.bucket.lib import format as fmt
from cogs.rewards.bucket.lib import svc
from lib.auth import Tier, current_tier, is_registered, is_staff
from lib.discord_utils.converters import CaseInsensitiveMember
from lib.discord_utils.paginated_embed import Paginator, from_lines
from orm import DiscordAccount

logger = logging.getLogger("dazebot.cogs.rewards.bucket")


HISTORY_LINES_PER_PAGE = 10


async def _resolve_target_disc(
    ctx: commands.Context, value: str,
) -> tuple[Optional[discord.Member], Optional[DiscordAccount]]:
    """Resolve a ``<user>`` command arg to a ``(member, DiscordAccount)``
    pair.

    Mirrors :func:`cogs.rewards.ctp.ctp._resolve_target_disc`: try
    :class:`CaseInsensitiveMember` (handles mention / snowflake / name),
    then fall back to a raw ``disc_uuid`` string lookup so staff can
    target users who have left the guild but still have rows in the DB.
    The :class:`discord.Member` side may be ``None`` even when the DB
    side resolves; both being ``None`` means no match.
    """
    member: Optional[discord.Member] = None
    try:
        member = await CaseInsensitiveMember().convert(ctx, value)
    except commands.MemberNotFound:
        pass

    if member is not None:
        disc = await svc.get_or_create_disc(str(member.id))
        return member, disc

    disc = await DiscordAccount.filter(disc_uuid=value).first()
    if disc is not None:
        return None, disc
    return None, None


def _mention(member: Optional[discord.Member], disc: DiscordAccount) -> str:
    """Best-effort display string for an awarded / redeemed reply. Uses a
    bare ``<@id>`` mention when we only have a DB row — Discord renders
    it as ``@unknown-user`` rather than failing.
    """
    if member is not None:
        return member.mention
    return f"<@{disc.disc_uuid}>"


class BucketCog(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("BucketCog initialized")

    # --- Group root ---------------------------------------------------

    @commands.hybrid_group(
        name="bucket",
        description="Staff-awarded prize-claim tokens in six tiers.",
    )
    async def bucket_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        tier = await current_tier(ctx)
        sections: list[str] = []
        if tier >= Tier.REGISTERED:
            sections.append(
                "**Registered**\n"
                "- `~bucket check` — show your unredeemed bucket pulls"
            )
        if tier >= Tier.STAFF:
            sections.append(
                "**Staff**\n"
                "- `~bucket award <user> <1-6> <reason>` — award a pull\n"
                "- `~bucket redeem <user> <1-6>` — consume the oldest pull of that tier\n"
                "- `~bucket status <1-6>` — count outstanding pulls in a tier\n"
                "- `~bucket info <user>` — full pull history for a user"
            )
        if not sections:
            await ctx.reply("You don't have access to any `~bucket` subcommands.")
            return
        await ctx.reply(
            "\n\n".join(sections),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --- Registered tier ----------------------------------------------

    @bucket_group.command(
        name="check",
        description="(Registered) Show your unredeemed bucket pulls.",
    )
    @is_registered()
    async def bucket_check(self, ctx: commands.Context) -> None:
        disc = await svc.get_or_create_disc(str(ctx.author.id))
        counts = await svc.count_outstanding_by_tier(disc)
        await ctx.reply(
            "\n".join(fmt.format_check_lines(counts)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --- Staff tier ---------------------------------------------------

    @bucket_group.command(
        name="award",
        description="(Staff) Award a bucket pull (1-6) to a user.",
    )
    @is_staff()
    async def bucket_award(
        self,
        ctx: commands.Context,
        user: str,
        tier: int,
        *,
        reason: str,
    ) -> None:
        if not svc.is_valid_tier(tier):
            await ctx.reply(
                f"Bucket tier must be between {svc.MIN_TIER} and {svc.MAX_TIER}.",
            )
            return
        member, disc = await _resolve_target_disc(ctx, user)
        if disc is None:
            await ctx.reply(
                f"Couldn't resolve `{user}` to a Discord user or existing account.",
            )
            return
        await svc.award(
            disc=disc,
            tier=tier,
            reason=reason,
            actor_disc_uuid=str(ctx.author.id),
        )
        await ctx.reply(
            f"Awarded a bucket {tier} pull to {_mention(member, disc)} for: {reason}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bucket_group.command(
        name="redeem",
        description="(Staff) Consume the oldest unredeemed pull of <tier> for <user>.",
    )
    @is_staff()
    async def bucket_redeem(
        self,
        ctx: commands.Context,
        user: str,
        tier: int,
    ) -> None:
        if not svc.is_valid_tier(tier):
            await ctx.reply(
                f"Bucket tier must be between {svc.MIN_TIER} and {svc.MAX_TIER}.",
            )
            return
        member, disc = await _resolve_target_disc(ctx, user)
        if disc is None:
            await ctx.reply(
                f"Couldn't resolve `{user}` to a Discord user or existing account.",
            )
            return
        pull = await svc.redeem_oldest(
            disc=disc,
            tier=tier,
            actor_disc_uuid=str(ctx.author.id),
        )
        mention = _mention(member, disc)
        if pull is None:
            await ctx.reply(
                f"{mention} has no outstanding bucket {tier} pulls.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        awarded_ts = int(pull.created_at.timestamp())
        await ctx.reply(
            f"Redeemed bucket {tier} pull `#{pull.id}` for {mention} "
            f"(awarded <t:{awarded_ts}:R> for: {pull.reason}).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bucket_group.command(
        name="status",
        description="(Staff) Count outstanding pulls in a bucket tier.",
    )
    @is_staff()
    async def bucket_status(self, ctx: commands.Context, tier: int) -> None:
        if not svc.is_valid_tier(tier):
            await ctx.reply(
                f"Bucket tier must be between {svc.MIN_TIER} and {svc.MAX_TIER}.",
            )
            return
        n = await svc.count_outstanding_for_tier(tier)
        suffix = "" if n == 1 else "s"
        await ctx.reply(f"Bucket {tier} has {n} outstanding claim{suffix}!")

    @bucket_group.command(
        name="info",
        description="(Staff) Show full bucket-pull history for a user.",
    )
    @is_staff()
    async def bucket_info(self, ctx: commands.Context, *, user: str) -> None:
        member, disc = await _resolve_target_disc(ctx, user)
        if disc is None:
            await ctx.reply(
                f"Couldn't resolve `{user}` to a Discord user or existing account.",
            )
            return
        rows = await svc.list_history(disc)
        name = member.display_name if member is not None else disc.disc_uuid
        if not rows:
            await ctx.reply(
                f"{_mention(member, disc)} has no bucket history.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        lines = [fmt.format_history_line(r) for r in rows]
        embeds = from_lines(
            f"Bucket history — {name}", lines, HISTORY_LINES_PER_PAGE, logger,
        )
        view = Paginator(embeds) if len(embeds) > 1 else None
        await ctx.reply(
            embed=embeds[0],
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(BucketCog(bot))
    logger.info("BucketCog loaded")

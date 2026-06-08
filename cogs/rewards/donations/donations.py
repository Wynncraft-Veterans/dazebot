"""The ``~donations`` staff-only cog.

Records and surfaces in-game donations to the guild. Most donations are
items (screenshot evidence), not currency — ``value_emeralds`` on
:class:`orm.Donation` is the staff-assigned emerald-equivalent valuation.

All commands restricted to channel :data:`DONATIONS_CHANNEL_ID`. Edits to
existing records are admin-only because they're destructive; view/record
are staff.

Glint integration: when a donation hits the 5%-of-cumulative milestone,
the recipient bubbles to the front of a list whose two most recent
distinct entries fill slots 7-8 of ``GET /api/internal/glinted`` (consumed
by temporary-server's glinted poller). Math lives in
:func:`cogs.rewards.donations.lib.svc.donation_milestone_recipients` —
recomputed live on each endpoint hit so edits and removes propagate
without a separate invalidation step.

Image durability: ``ctx.message.attachments`` URLs (Discord CDN) are
captured at record time. Discord re-signs them when the source message
is re-fetched, so they survive as long as the source message survives.
Re-hosting to ``i.wynnvets.org`` is out of scope for v1. The ``record``
subcommand only captures attachments when invoked as a prefix command;
slash invocations work without images.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from bot import Bot
from cogs.rewards.donations.lib import format as fmt
from cogs.rewards.donations.lib import svc
from cogs.rewards.lib.confirm_view import ConfirmView
from lib.auth import is_admin, is_staff
from lib.discord_utils.paginated_embed import Paginator, from_lines
from lib.emerald import EmeraldParseError, format_emeralds_as_stx, parse_emeralds
from lib.mc.resolve import DonationRecipientError, resolve_donation_recipient

logger = logging.getLogger("dazebot.cogs.rewards.donations")


DONATIONS_CHANNEL_ID = 1336152747644551248
LINES_PER_PAGE = 10
RECENT_FETCH_LIMIT = 100
HIGHEST_FETCH_LIMIT = 200


async def _channel_ok(ctx: commands.Context) -> bool:
    """First-line guard on every command in this group. Replies with a
    pointer to the right channel and returns False when invoked elsewhere.
    """
    if ctx.channel.id != DONATIONS_CHANNEL_ID:
        await ctx.reply(
            f"`~donations` commands are restricted to <#{DONATIONS_CHANNEL_ID}>.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False
    return True


class DonationsCog(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("DonationsCog initialized")

    # -----------------------------------------------------------------
    # Group root + view commands (staff)
    # -----------------------------------------------------------------

    @commands.hybrid_group(
        name="donations",
        description="(Staff) Track in-game donations to the guild.",
    )
    async def donations_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        if not await _channel_ok(ctx):
            return
        await ctx.reply(
            "Use one of: `~donations record`, `~donations recent`, "
            "`~donations leaderboard`, `~donations highest`, "
            "`~donations info <id>`, `~donations edit <id> ...` (admin).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @donations_group.command(
        name="record",
        description="(Staff) Record a donation. Attach images via prefix invocation.",
    )
    @is_staff()
    async def donations_record(
        self,
        ctx: commands.Context,
        recipient: str,
        value: str,
        *,
        comment: Optional[str] = None,
    ) -> None:
        if not await _channel_ok(ctx):
            return

        try:
            mc = await resolve_donation_recipient(ctx, recipient)
        except DonationRecipientError as e:
            await ctx.reply(str(e), allowed_mentions=discord.AllowedMentions.none())
            return

        try:
            value_emeralds = parse_emeralds(value)
        except EmeraldParseError as e:
            await ctx.reply(
                f"Couldn't parse value `{value}`: {e}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if value_emeralds <= 0:
            await ctx.reply(
                "Donation value must be greater than zero.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        image_urls: list[str] = []
        if ctx.message is not None and ctx.message.attachments:
            image_urls = [a.url for a in ctx.message.attachments]

        donation = await svc.record_donation(
            recipient=mc,
            value_emeralds=value_emeralds,
            comment=comment,
            image_urls=image_urls,
            recorder_disc_uuid=str(ctx.author.id),
        )

        attachment_note = (
            f" with {len(image_urls)} image{'s' if len(image_urls) != 1 else ''}"
            if image_urls
            else ""
        )
        await ctx.reply(
            f"Recorded donation **#{donation.id}** of "
            f"**{format_emeralds_as_stx(donation.value_emeralds)}** "
            f"to **{mc.mc_username}**{attachment_note}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @donations_group.command(
        name="recent",
        description="(Staff) Show the most recent donations, paginated.",
    )
    @is_staff()
    async def donations_recent(self, ctx: commands.Context) -> None:
        if not await _channel_ok(ctx):
            return
        donations = await svc.list_recent(RECENT_FETCH_LIMIT)
        if not donations:
            await ctx.reply("No donations recorded yet.")
            return
        lines = [fmt.format_recent_line(d) for d in donations]
        embeds = from_lines("Recent donations", lines, LINES_PER_PAGE, logger)
        view = Paginator(embeds) if len(embeds) > 1 else None
        await ctx.reply(
            embed=embeds[0], view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @donations_group.command(
        name="leaderboard",
        description="(Staff) All-time per-user donation totals, paginated.",
    )
    @is_staff()
    async def donations_leaderboard(self, ctx: commands.Context) -> None:
        if not await _channel_ok(ctx):
            return
        totals = await svc.leaderboard_totals()
        if not totals:
            await ctx.reply("No donations recorded yet.")
            return
        lines = [
            fmt.format_leaderboard_line(rank, mc, total)
            for rank, (mc, total) in enumerate(totals, start=1)
        ]
        embeds = from_lines("Donation leaderboard", lines, LINES_PER_PAGE, logger)
        view = Paginator(embeds) if len(embeds) > 1 else None
        await ctx.reply(
            embed=embeds[0], view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @donations_group.command(
        name="highest",
        description="(Staff) Highest single donations; users may repeat.",
    )
    @is_staff()
    async def donations_highest(self, ctx: commands.Context) -> None:
        if not await _channel_ok(ctx):
            return
        donations = await svc.highest_donations(HIGHEST_FETCH_LIMIT)
        if not donations:
            await ctx.reply("No donations recorded yet.")
            return
        lines = [
            fmt.format_highest_line(rank, d)
            for rank, d in enumerate(donations, start=1)
        ]
        embeds = from_lines("Highest single donations", lines, LINES_PER_PAGE, logger)
        view = Paginator(embeds) if len(embeds) > 1 else None
        await ctx.reply(
            embed=embeds[0], view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @donations_group.command(
        name="info",
        description="(Staff) Show full info for one donation.",
    )
    @is_staff()
    async def donations_info(self, ctx: commands.Context, donation_id: int) -> None:
        if not await _channel_ok(ctx):
            return
        donation = await svc.get_donation(donation_id)
        if donation is None:
            await ctx.reply(f"No donation found with id `{donation_id}`.")
            return
        embeds = fmt.build_info_embed(donation, svc.parse_image_urls(donation))
        await ctx.reply(
            embeds=embeds,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # -----------------------------------------------------------------
    # Edit subgroup (admin)
    # -----------------------------------------------------------------

    @donations_group.group(
        name="edit",
        description="(Admin) Edit or remove a donation record.",
    )
    @is_admin()
    async def donations_edit_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        if not await _channel_ok(ctx):
            return
        await ctx.reply(
            "Use `~donations edit <id> value <new>`, "
            "`~donations edit <id> comment <new>`, "
            "or `~donations edit <id> remove`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @donations_edit_group.command(
        name="value",
        description="(Admin) Change a donation's value.",
    )
    @is_admin()
    async def donations_edit_value(
        self, ctx: commands.Context, donation_id: int, *, value: str,
    ) -> None:
        if not await _channel_ok(ctx):
            return
        donation = await svc.get_donation(donation_id)
        if donation is None:
            await ctx.reply(f"No donation found with id `{donation_id}`.")
            return
        try:
            new_value = parse_emeralds(value)
        except EmeraldParseError as e:
            await ctx.reply(f"Couldn't parse value `{value}`: {e}")
            return
        if new_value <= 0:
            await ctx.reply("Donation value must be greater than zero.")
            return
        old_value = donation.value_emeralds
        await svc.edit_donation_value(donation, new_value)
        await ctx.reply(
            f"Donation **#{donation.id}** value updated: "
            f"{format_emeralds_as_stx(old_value)} → "
            f"**{format_emeralds_as_stx(new_value)}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @donations_edit_group.command(
        name="comment",
        description="(Admin) Change/clear a donation's comment. Use 'clear' or 'none' to clear.",
    )
    @is_admin()
    async def donations_edit_comment(
        self, ctx: commands.Context, donation_id: int, *, comment: str,
    ) -> None:
        if not await _channel_ok(ctx):
            return
        donation = await svc.get_donation(donation_id)
        if donation is None:
            await ctx.reply(f"No donation found with id `{donation_id}`.")
            return
        normalised = comment.strip()
        new_comment: Optional[str] = (
            None if normalised.lower() in {"", "clear", "none"} else normalised
        )
        await svc.edit_donation_comment(donation, new_comment)
        if new_comment is None:
            await ctx.reply(
                f"Donation **#{donation.id}** comment cleared.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.reply(
                f"Donation **#{donation.id}** comment updated.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @donations_edit_group.command(
        name="remove",
        description="(Admin) Delete a donation record (irreversible).",
    )
    @is_admin()
    async def donations_edit_remove(
        self, ctx: commands.Context, donation_id: int,
    ) -> None:
        if not await _channel_ok(ctx):
            return
        donation = await svc.get_donation(donation_id)
        if donation is None:
            await ctx.reply(f"No donation found with id `{donation_id}`.")
            return
        view = ConfirmView(author_id=ctx.author.id)
        prompt = await ctx.reply(
            f"Permanently delete donation **#{donation.id}** "
            f"({format_emeralds_as_stx(donation.value_emeralds)} to "
            f"**{donation.recipient_mc.mc_username}**)?",
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await view.wait()
        if not view.confirmed:
            await prompt.edit(content="Cancelled.", view=None)
            return
        await svc.remove_donation(donation)
        await prompt.edit(
            content=f"Donation **#{donation_id}** deleted.",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(DonationsCog(bot))
    logger.info("DonationsCog loaded")

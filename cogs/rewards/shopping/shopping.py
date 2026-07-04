"""The ``~shopping`` cog: staff-tracked "we want to buy X" wishlist plus
staff-recorded fulfilment donations.

Sits alongside :mod:`cogs.rewards.donations` and shares the same channel
gate (``DONATIONS_CHANNEL_ID``), the same emerald parser
(:mod:`lib.emerald`), and the same recipient resolver
(:func:`lib.mc.resolve.resolve_donation_recipient`). Deviations from the
donations shape are called out where they exist:

* :class:`orm.ShoppingRequestAdjustment` is an append-only qty log; the
  request row itself carries a denormalised ``qty_remaining`` for
  read-hot ``~shopping list`` filtering.
* Shopping donations are soft-voided rather than hard-deleted — the
  audit trail is the whole point of the append-only design.
* ``~shopping list`` is deliberately the ONE open, non-channel-gated
  command in the group. Members need to know what to hunt for without
  needing staff-channel access; every other command stays staff/admin-
  gated and channel-gated.

Glint integration: :func:`cogs.rewards.shopping.lib.svc.top_monthly_shopping_donor_online`
is called by ``/api/internal/glinted`` slot 5 (rolling 30-day window,
online-only, no fallthrough). See the endpoint docstring for the full
slot layout.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from bot import Bot
from cogs.rewards.donations.donations import DONATIONS_CHANNEL_ID
from cogs.rewards.shopping.lib import format as fmt
from cogs.rewards.shopping.lib import items as items_lookup
from cogs.rewards.shopping.lib import svc
from lib.auth import is_admin, is_staff
from lib.discord_utils.paginated_embed import Paginator, from_lines
from lib.emerald import EmeraldParseError, format_emeralds_as_stx, parse_emeralds
from lib.mc.resolve import DonationRecipientError, resolve_donation_recipient

logger = logging.getLogger("dazebot.cogs.rewards.shopping")


LINES_PER_PAGE = 10


async def _channel_ok(ctx: commands.Context) -> bool:
    """First-line channel guard, shared with the donations cog.

    NOT applied to ``~shopping list`` — that command is intentionally
    open in any channel so members can check the wishlist.
    """
    if ctx.channel.id != DONATIONS_CHANNEL_ID:
        await ctx.reply(
            f"`~shopping` commands are restricted to <#{DONATIONS_CHANNEL_ID}> "
            f"(except `~shopping list`, which works anywhere).",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False
    return True


class ShoppingCog(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("ShoppingCog initialized")

    # -----------------------------------------------------------------
    # Group root
    # -----------------------------------------------------------------

    @commands.hybrid_group(
        name="shopping",
        description="Track guild shopping requests and donations.",
    )
    async def shopping_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        await ctx.reply(
            "Use one of: `~shopping request` / `record` / `list` / "
            "`leaderboard` / `info <id>` / `adjust <id> <±n>` / "
            "`close <id>` (admin) / `void <donation_id>` (admin) / "
            "`edit <id> item|value|comment <new>` (admin).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # -----------------------------------------------------------------
    # Write commands (staff)
    # -----------------------------------------------------------------

    @shopping_group.command(
        name="request",
        description="(Staff) Post a new shopping request.",
    )
    @is_staff()
    async def shopping_request(
        self,
        ctx: commands.Context,
        item: str,
        unit_value: str,
        qty: int,
        *,
        comment: Optional[str] = None,
    ) -> None:
        if not await _channel_ok(ctx):
            return

        if qty < 0:
            await ctx.reply(
                "Quantity must be zero or greater.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            unit_value_emeralds = parse_emeralds(unit_value)
        except EmeraldParseError as e:
            await ctx.reply(
                f"Couldn't parse unit value `{unit_value}`: {e}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if unit_value_emeralds <= 0:
            await ctx.reply(
                "Unit value must be greater than zero.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            canonical = await items_lookup.resolve_item_name(item)
        except items_lookup.ItemLookupError as e:
            await ctx.reply(str(e), allowed_mentions=discord.AllowedMentions.none())
            return

        request = await svc.create_request(
            item_name=canonical,
            item_name_lower=canonical.lower(),
            unit_value_emeralds=unit_value_emeralds,
            qty=qty,
            requester_disc_uuid=str(ctx.author.id),
            comment=comment,
        )

        unit_value_stx = format_emeralds_as_stx(unit_value_emeralds)
        if qty == 0:
            await ctx.reply(
                f"Recorded shopping wishlist entry **#{request.id}**: "
                f"**{canonical}** @ **{unit_value_stx}**/ea. "
                f"Hidden from `~shopping list` (no outstanding demand); "
                f"staff can still `~shopping record` overflow donations "
                f"against it at the over-request rate.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            total = format_emeralds_as_stx(unit_value_emeralds * qty)
            await ctx.reply(
                f"Recorded shopping request **#{request.id}**: "
                f"**{qty} × {canonical}** @ **{unit_value_stx}**/ea "
                f"(total budget {total}).",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @shopping_group.command(
        name="record",
        description="(Staff) Record a fulfilment donation against an open request.",
    )
    @is_staff()
    async def shopping_record(
        self,
        ctx: commands.Context,
        recipient: str,
        item: str,
        qty: int,
        *,
        comment: Optional[str] = None,
    ) -> None:
        if not await _channel_ok(ctx):
            return

        if qty <= 0:
            await ctx.reply(
                "Quantity must be greater than zero.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            mc = await resolve_donation_recipient(ctx, recipient)
        except DonationRecipientError as e:
            await ctx.reply(str(e), allowed_mentions=discord.AllowedMentions.none())
            return

        request = await svc.find_open_request_for_item(item.strip().lower())
        if request is None:
            await ctx.reply(
                f"No open shopping request matches `{item}`. Use "
                f"`~shopping request` to open one first, or check "
                f"`~shopping list`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        donation = await svc.record_donation(
            request=request,
            recipient=mc,
            qty=qty,
            comment=comment,
            recorder_disc_uuid=str(ctx.author.id),
        )

        qty_over = qty - donation.qty_at_full_value
        split_note = ""
        if qty_over > 0:
            full_credit = donation.qty_at_full_value * donation.unit_value_emeralds_at_time
            over_credit = donation.value_emeralds - full_credit
            over_pct = round(100 * over_credit / (qty_over * donation.unit_value_emeralds_at_time))
            split_note = (
                f" ({donation.qty_at_full_value} at 100%, "
                f"{qty_over} at {over_pct}%)"
            )
        closed_note = ""
        if request.closed_at is not None:
            closed_note = " Request now closed."
        await ctx.reply(
            f"Recorded shopping donation **#{donation.id}**: "
            f"**{qty} × {request.item_name}** to **{mc.mc_username}** — "
            f"credit **{format_emeralds_as_stx(donation.value_emeralds)}**{split_note}. "
            f"Request **#{request.id}** now at "
            f"{request.qty_remaining}/{request.qty_initial}.{closed_note}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shopping_group.command(
        name="adjust",
        description="(Staff) Adjust an open request's qty. Delta is signed (e.g. +3 or -2).",
    )
    @is_staff()
    async def shopping_adjust(
        self,
        ctx: commands.Context,
        request_id: int,
        delta: str,
        *,
        reason: Optional[str] = None,
    ) -> None:
        if not await _channel_ok(ctx):
            return

        try:
            delta_int = int(delta.strip())
        except (AttributeError, ValueError):
            await ctx.reply(
                f"Couldn't parse delta `{delta}`. Use a signed integer "
                f"like `+3` or `-2`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if delta_int == 0:
            await ctx.reply(
                "Delta of 0 has no effect.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        request = await svc.get_request(request_id)
        if request is None:
            await ctx.reply(
                f"No shopping request with id `{request_id}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            await svc.append_adjustment(
                request=request,
                delta_qty=delta_int,
                reason=reason,
                actor_disc_uuid=str(ctx.author.id),
            )
        except svc.ShoppingError as e:
            await ctx.reply(str(e), allowed_mentions=discord.AllowedMentions.none())
            return

        sign = "+" if delta_int > 0 else ""
        reopened_note = ""
        if delta_int > 0 and request.closed_at is None and request.qty_remaining == delta_int:
            # closed_at was cleared by append_adjustment if we just reopened
            reopened_note = " Request reopened."
        closed_note = (
            " Request now closed." if request.closed_at is not None else ""
        )
        await ctx.reply(
            f"Adjusted request **#{request.id}** by **{sign}{delta_int}**. "
            f"Now at {request.qty_remaining}/{request.qty_initial}."
            f"{reopened_note}{closed_note}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # -----------------------------------------------------------------
    # View commands
    # -----------------------------------------------------------------

    @shopping_group.command(
        name="list",
        description="Open shopping requests. Works in any channel.",
    )
    async def shopping_list(self, ctx: commands.Context) -> None:
        # Deliberately no channel gate and no staff gate — see cog
        # docstring. This is the only command in the group that is
        # readable by everyone, in any channel.
        requests = await svc.list_open_requests()
        if not requests:
            await ctx.reply(
                "No open shopping requests.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        lines = [fmt.format_open_request_line(r) for r in requests]
        embeds = from_lines("Open shopping requests", lines, LINES_PER_PAGE, logger)
        view = Paginator(embeds) if len(embeds) > 1 else None
        await ctx.reply(
            embed=embeds[0], view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shopping_group.command(
        name="leaderboard",
        description="(Staff) All-time cumulative shopping donation totals per recipient.",
    )
    @is_staff()
    async def shopping_leaderboard(self, ctx: commands.Context) -> None:
        if not await _channel_ok(ctx):
            return
        totals = await svc.leaderboard_totals()
        if not totals:
            await ctx.reply(
                "No shopping donations recorded yet.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        lines = [
            fmt.format_leaderboard_line(rank, mc, total)
            for rank, (mc, total) in enumerate(totals, start=1)
        ]
        embeds = from_lines("Shopping leaderboard", lines, LINES_PER_PAGE, logger)
        view = Paginator(embeds) if len(embeds) > 1 else None
        await ctx.reply(
            embed=embeds[0], view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shopping_group.command(
        name="info",
        description="(Staff) Show the full state + history of one request.",
    )
    @is_staff()
    async def shopping_info(
        self, ctx: commands.Context, request_id: int,
    ) -> None:
        if not await _channel_ok(ctx):
            return
        request = await svc.get_request(request_id)
        if request is None:
            await ctx.reply(
                f"No shopping request with id `{request_id}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        adjustments, donations = await svc.request_history(request_id)
        embed = fmt.build_info_embed(request, adjustments, donations)
        await ctx.reply(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # -----------------------------------------------------------------
    # Admin commands
    # -----------------------------------------------------------------

    @shopping_group.command(
        name="close",
        description="(Admin) Force-close a shopping request (logs a zero-out adjustment).",
    )
    @is_admin()
    async def shopping_close(
        self,
        ctx: commands.Context,
        request_id: int,
        *,
        reason: Optional[str] = None,
    ) -> None:
        if not await _channel_ok(ctx):
            return
        request = await svc.get_request(request_id)
        if request is None:
            await ctx.reply(
                f"No shopping request with id `{request_id}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if request.closed_at is not None:
            await ctx.reply(
                f"Request **#{request.id}** is already closed.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        # append_adjustment auto-closes when qty_remaining hits 0.
        try:
            await svc.append_adjustment(
                request=request,
                delta_qty=-request.qty_remaining,
                reason=reason or "manual close",
                actor_disc_uuid=str(ctx.author.id),
            )
        except svc.ShoppingError as e:
            await ctx.reply(str(e), allowed_mentions=discord.AllowedMentions.none())
            return
        await ctx.reply(
            f"Shopping request **#{request.id}** closed.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shopping_group.command(
        name="void",
        description="(Admin) Soft-void a shopping donation and restore the fulfilled qty.",
    )
    @is_admin()
    async def shopping_void(
        self,
        ctx: commands.Context,
        donation_id: int,
        *,
        reason: Optional[str] = None,
    ) -> None:
        if not await _channel_ok(ctx):
            return
        donation = await svc.get_donation(donation_id)
        if donation is None:
            await ctx.reply(
                f"No shopping donation with id `{donation_id}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if donation.voided_at is not None:
            await ctx.reply(
                f"Donation **#{donation.id}** is already voided.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await svc.void_donation(
            donation=donation,
            voided_by_disc_uuid=str(ctx.author.id),
            reason=reason,
        )
        restored = donation.qty_at_full_value
        restore_note = ""
        if restored > 0 and donation.request_id is not None:
            restore_note = (
                f" Restored **{restored}** to request "
                f"**#{donation.request_id}**."
            )
        await ctx.reply(
            f"Shopping donation **#{donation.id}** voided.{restore_note}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # -----------------------------------------------------------------
    # Edit subgroup (admin) — mutations on ShoppingRequest fields
    # -----------------------------------------------------------------

    @shopping_group.group(
        name="edit",
        description="(Admin) Edit a shopping request's fields.",
    )
    @is_admin()
    async def shopping_edit_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        if not await _channel_ok(ctx):
            return
        await ctx.reply(
            "Use `~shopping edit item <id> <new name>`, "
            "`~shopping edit value <id> <new value>`, or "
            "`~shopping edit comment <id> <new comment>`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shopping_edit_group.command(
        name="item",
        description="(Admin) Rename an existing request's item (revalidates against Wynncraft).",
    )
    @is_admin()
    async def shopping_edit_item(
        self, ctx: commands.Context, request_id: int, *, new_item: str,
    ) -> None:
        if not await _channel_ok(ctx):
            return
        request = await svc.get_request(request_id)
        if request is None:
            await ctx.reply(
                f"No shopping request with id `{request_id}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            canonical = await items_lookup.resolve_item_name(new_item)
        except items_lookup.ItemLookupError as e:
            await ctx.reply(str(e), allowed_mentions=discord.AllowedMentions.none())
            return
        old_name = request.item_name
        await svc.edit_request_item(request, canonical, canonical.lower())
        await ctx.reply(
            f"Request **#{request.id}** item renamed: "
            f"`{old_name}` → **{canonical}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shopping_edit_group.command(
        name="value",
        description="(Admin) Change a request's unit value. Doesn't affect historical donations.",
    )
    @is_admin()
    async def shopping_edit_value(
        self, ctx: commands.Context, request_id: int, *, new_value: str,
    ) -> None:
        if not await _channel_ok(ctx):
            return
        request = await svc.get_request(request_id)
        if request is None:
            await ctx.reply(
                f"No shopping request with id `{request_id}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            new_v = parse_emeralds(new_value)
        except EmeraldParseError as e:
            await ctx.reply(f"Couldn't parse value `{new_value}`: {e}")
            return
        if new_v <= 0:
            await ctx.reply("Unit value must be greater than zero.")
            return
        old_v = request.unit_value_emeralds
        await svc.edit_request_value(request, new_v)
        await ctx.reply(
            f"Request **#{request.id}** unit value updated: "
            f"{format_emeralds_as_stx(old_v)} → "
            f"**{format_emeralds_as_stx(new_v)}**/ea (future donations only).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @shopping_edit_group.command(
        name="comment",
        description="(Admin) Change/clear a request's comment. Use 'clear' or 'none' to clear.",
    )
    @is_admin()
    async def shopping_edit_comment(
        self, ctx: commands.Context, request_id: int, *, new_comment: str,
    ) -> None:
        if not await _channel_ok(ctx):
            return
        request = await svc.get_request(request_id)
        if request is None:
            await ctx.reply(
                f"No shopping request with id `{request_id}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        normalised = new_comment.strip()
        cleared = normalised.lower() in {"", "clear", "none"}
        await svc.edit_request_comment(request, None if cleared else normalised)
        if cleared:
            await ctx.reply(
                f"Request **#{request.id}** comment cleared.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.reply(
                f"Request **#{request.id}** comment updated.",
                allowed_mentions=discord.AllowedMentions.none(),
            )


async def setup(bot: Bot) -> None:
    await bot.add_cog(ShoppingCog(bot))
    logger.info("ShoppingCog loaded")

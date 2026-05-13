"""``~warnings <username>`` -- staff-only readout of a player's caution
history.

Writes (cautions, warnings, ejects) come in via the
``POST /api/internal/staff-action`` HTTP endpoint, forwarded by
temporary-server when a staff member runs ``/caution`` / ``/warn`` /
``/eject`` in the mod. This cog only exposes a Discord-side **read** of
the same state -- in-game writes from Discord were intentionally not
specified, since the staff member needs to be in-game to see context
anyway.

The same data is reachable from in-game via vetsmod's ``/wv check``
(also a read-only command), so staff get a consistent view either way.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Optional

import discord
from discord.ext import commands

from bot import Bot
from lib.auth import is_admin, is_staff
from lib.mc.wynn_api.errors import WynnApiError
from lib.staff import staff_actions

logger = logging.getLogger("dazebot.cogs.staff_actions")


def _kind_label(kind: str) -> str:
    """Bold text label per kind. Keeps the embed readable without
    relying on a fixed-width emoji column."""
    return {
        "caution": "**caution**",
        "warning": "**WARNING**",
        "eject": "**EJECT**",
    }.get(kind, f"**{kind}**")


class StaffActions(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("StaffActions cog initialized")

    @commands.hybrid_command(
        name="warnings",
        description="(Staff) Show a player's caution / warning / eject history.",
    )
    @is_staff()
    async def warnings(self, ctx: commands.Context, username: str):
        """Resolve ``username`` (mc username or uuid), then print a
        paginated history with the running point total. Identical
        underlying read as vetsmod's ``/wv check``.
        """
        await ctx.defer()
        try:
            target_uuid, canonical, _mc = await staff_actions.resolve_target(username)
        except WynnApiError as exc:
            await ctx.reply(f"Couldn't resolve `{username}`: {exc.message}")
            return
        except Exception:  # noqa: BLE001 - WAPI is third-party
            logger.exception("warnings: resolve failed for %r", username)
            await ctx.reply(f"Failed to resolve `{username}` (Wynncraft API error).")
            return

        total = await staff_actions.total_points_for(target_uuid)
        rows = await staff_actions.history_for(target_uuid)

        embed = discord.Embed(
            title=f"Caution history -- {canonical}",
            color=_color_for_total(total),
        )
        embed.add_field(
            name="Total points",
            value=_format_total(total),
            inline=False,
        )
        if not rows:
            embed.add_field(
                name="History",
                value="_(no entries)_",
                inline=False,
            )
            await ctx.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return

        # Most-recent first; Discord embed field caps at 25, history_for
        # already limits to 25.
        lines = []
        for r in rows:
            ts = int(_to_aware(r.created_at).timestamp())
            head = f"{_kind_label(r.kind)} (+{r.points}) by `{r.actor_username_at_time}` <t:{ts}:R>"
            if r.message:
                # Trim to keep embed under Discord's 6000-char total.
                trimmed = r.message.strip()
                if len(trimmed) > 200:
                    trimmed = trimmed[:200] + "..."
                lines.append(f"{head}\n> {trimmed}")
            else:
                lines.append(head)

        # Discord field value max 1024 chars; chunk if we'd overflow.
        chunk = ""
        idx = 1
        for line in lines:
            if len(chunk) + len(line) + 2 > 1000:
                embed.add_field(
                    name=f"History (page {idx})",
                    value=chunk or "_(empty)_",
                    inline=False,
                )
                chunk = line
                idx += 1
            else:
                chunk = f"{chunk}\n{line}" if chunk else line
        if chunk:
            embed.add_field(
                name=f"History (page {idx})" if idx > 1 else "History",
                value=chunk,
                inline=False,
            )

        await ctx.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # =================================================================
    # Admin overrides: manual caution-point adjustments
    # =================================================================
    #
    # Both commands gate on @is_admin (ADMIN tier in lib.auth) -- this
    # is an escape hatch above the normal /caution /warn /eject flow,
    # not a regular staff tool. An adjustment row is inserted (kind=
    # "adjustment", points=<delta>) so the running total moves; no DM
    # is sent, no blocklist row is created, no kick/demote is
    # dispatched. Admins who want any of those side-effects should run
    # the appropriate command directly instead of hand-editing the
    # total.
    #
    # The override is logged to the moderation-log thread for audit.

    @commands.hybrid_command(
        name="warnings-set",
        description="(Admin) Set a player's cumulative caution-point total to an absolute value.",
    )
    @is_admin()
    async def warnings_set(
        self,
        ctx: commands.Context,
        username: str,
        new_total: int,
        *,
        reason: Optional[str] = None,
    ):
        """``/warnings-set <username> <new_total> [reason]`` -- compute
        the delta against the current total and insert one adjustment
        row. Use ``new_total=0`` to clear.
        """
        if new_total < 0:
            await ctx.reply("`new_total` must be >= 0.")
            return
        await ctx.defer()
        try:
            target_uuid, canonical, _mc = await staff_actions.resolve_target(username)
        except WynnApiError as exc:
            await ctx.reply(f"Couldn't resolve `{username}`: {exc.message}")
            return
        except Exception:  # noqa: BLE001 -- WAPI is third-party
            logger.exception("warnings-set: resolve failed for %r", username)
            await ctx.reply(f"Failed to resolve `{username}` (Wynncraft API error).")
            return

        prior = await staff_actions.total_points_for(target_uuid)
        delta = new_total - prior
        if delta == 0:
            await ctx.reply(
                f"`{canonical}` already has `{prior}` caution point"
                f"{'' if prior == 1 else 's'}; no change.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        result = await staff_actions.record_adjustment(
            self.bot,
            target_username=canonical,
            actor_uuid=str(ctx.author.id),
            actor_username=str(ctx.author),
            delta=delta,
            reason=reason,
        )
        sign = "+" if delta > 0 else ""
        await ctx.reply(
            f"Set `{result.target_username}` to `{result.new_total}` caution point"
            f"{'' if result.new_total == 1 else 's'} "
            f"({result.prior_total} {sign}{delta}).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(
        name="warnings-adjust",
        description="(Admin) Add or subtract caution points from a player's running total.",
    )
    @is_admin()
    async def warnings_adjust(
        self,
        ctx: commands.Context,
        username: str,
        delta: int,
        *,
        reason: Optional[str] = None,
    ):
        """``/warnings-adjust <username> <delta> [reason]`` -- positive
        ``delta`` adds points, negative ``delta`` removes them. The
        running total is allowed to go below zero (admin's call); the
        warning/eject thresholds still apply to *commits*, not to
        adjustments, so a negative balance just absorbs future
        cautions silently.
        """
        if delta == 0:
            await ctx.reply("`delta` must be non-zero.")
            return
        await ctx.defer()
        try:
            result = await staff_actions.record_adjustment(
                self.bot,
                target_username=username,
                actor_uuid=str(ctx.author.id),
                actor_username=str(ctx.author),
                delta=delta,
                reason=reason,
            )
        except WynnApiError as exc:
            await ctx.reply(f"Couldn't resolve `{username}`: {exc.message}")
            return
        except Exception:  # noqa: BLE001 -- WAPI is third-party
            logger.exception("warnings-adjust: failed for %r", username)
            await ctx.reply(f"Failed to adjust `{username}` (see logs).")
            return

        sign = "+" if delta > 0 else ""
        await ctx.reply(
            f"Adjusted `{result.target_username}` by `{sign}{delta}` "
            f"({result.prior_total} -> {result.new_total}).",
            allowed_mentions=discord.AllowedMentions.none(),
        )


def _color_for_total(total: int) -> discord.Color:
    if total >= staff_actions.EJECT_THRESHOLD:
        return discord.Color.red()
    if total >= staff_actions.WARNING_THRESHOLD:
        return discord.Color.orange()
    if total > 0:
        return discord.Color.gold()
    return discord.Color.green()


def _format_total(total: int) -> str:
    suffix = ""
    if total >= staff_actions.EJECT_THRESHOLD:
        suffix = " -- **EJECT THRESHOLD CROSSED**"
    elif total >= staff_actions.WARNING_THRESHOLD:
        suffix = " -- warning threshold crossed"
    return f"`{total}`{suffix}"


def _to_aware(dt: datetime) -> datetime:
    """``auto_now_add`` rows from sqlite may come back tz-naive; coerce
    to UTC so ``.timestamp()`` produces the right Discord timestamp.
    """
    if dt.tzinfo is None:
        from datetime import timezone

        return dt.replace(tzinfo=timezone.utc)
    return dt


async def setup(bot: Bot):
    await bot.add_cog(StaffActions(bot))
    logger.info("StaffActions cog loaded")

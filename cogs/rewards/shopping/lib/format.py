"""Line and embed builders for the ``~shopping`` cog responses.

Pure sync functions that take ORM rows and return strings or
:class:`discord.Embed` objects. Mirrors the shape of
:mod:`cogs.rewards.donations.lib.format`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import discord

from lib.emerald import format_emeralds_as_stx
from orm import (
    MinecraftAccount,
    ShoppingDonation,
    ShoppingRequest,
    ShoppingRequestAdjustment,
)

COMMENT_TRUNC = 60


def truncate_comment(comment: Optional[str], limit: int = COMMENT_TRUNC) -> str:
    if not comment:
        return ""
    text = comment.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _discord_relative_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:R>"


def format_open_request_line(r: ShoppingRequest) -> str:
    """One line per open request for ``~shopping list``.

    Shape: ``#{id} {qty_remaining}/{qty_initial} × **{item_name}** @ {value}/ea — <@requester> {relative_ts}``
    """
    return (
        f"#{r.id} {r.qty_remaining}/{r.qty_initial} × **{r.item_name}** "
        f"@ {format_emeralds_as_stx(r.unit_value_emeralds)}/ea — "
        f"<@{r.requester_disc_uuid}> {_discord_relative_ts(r.created_at)}"
    )


def format_leaderboard_line(rank: int, mc: MinecraftAccount, total: int) -> str:
    """One line per recipient for ``~shopping leaderboard``.

    Shape: ``{rank}. **{mc_username}** — {total as stx}``
    """
    return f"{rank}. **{mc.mc_username}** — {format_emeralds_as_stx(total)}"


def format_adjustment_line(a: ShoppingRequestAdjustment) -> str:
    """One history line per :class:`ShoppingRequestAdjustment`.

    Signed delta with the actor and relative timestamp.
    """
    sign = "+" if a.delta_qty >= 0 else ""
    line = (
        f"{sign}{a.delta_qty} by <@{a.actor_disc_uuid}> "
        f"{_discord_relative_ts(a.created_at)}"
    )
    snippet = truncate_comment(a.reason)
    if snippet:
        line += f" — {snippet}"
    return line


def format_donation_history_line(d: ShoppingDonation) -> str:
    """One history line per :class:`ShoppingDonation` on ``~shopping info``.

    Voided donations are struck through so the audit trail stays visible
    without confusing the "current credit" total.
    """
    base = (
        f"#{d.id} {d.qty}× to **{d.recipient_mc.mc_username}** "
        f"({d.qty_at_full_value} at 100%, {d.qty - d.qty_at_full_value} at 20%) — "
        f"credit {format_emeralds_as_stx(d.value_emeralds)} — "
        f"<@{d.recorder_disc_uuid}> {_discord_relative_ts(d.created_at)}"
    )
    snippet = truncate_comment(d.comment)
    if snippet:
        base += f" — {snippet}"
    if d.voided_at is not None:
        reason = f" (reason: {d.voided_reason})" if d.voided_reason else ""
        base = f"~~{base}~~ — **voided** by <@{d.voided_by_disc_uuid}>{reason}"
    return base


def build_info_embed(
    request: ShoppingRequest,
    adjustments: list[ShoppingRequestAdjustment],
    donations: list[ShoppingDonation],
) -> discord.Embed:
    """Compose the ``~shopping info <id>`` embed.

    Includes the request's current state, the append-only adjustment log,
    and the donation history (voided rows dimmed).
    """
    status = "**CLOSED**" if request.closed_at is not None else "open"
    embed = discord.Embed(
        title=f"Shopping request #{request.id}: {request.item_name}",
        description=request.comment or None,
    )
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(
        name="Unit value",
        value=format_emeralds_as_stx(request.unit_value_emeralds),
        inline=True,
    )
    embed.add_field(
        name="Qty",
        value=f"{request.qty_remaining} remaining / {request.qty_initial} initial",
        inline=True,
    )
    embed.add_field(
        name="Requested by",
        value=f"<@{request.requester_disc_uuid}> {_discord_relative_ts(request.created_at)}",
        inline=False,
    )

    if adjustments:
        history = "\n".join(format_adjustment_line(a) for a in adjustments)
        if len(history) > 1024:
            history = history[:1020] + "\n…"
        embed.add_field(name="Adjustments", value=history, inline=False)

    if donations:
        history = "\n".join(format_donation_history_line(d) for d in donations)
        if len(history) > 1024:
            history = history[:1020] + "\n…"
        embed.add_field(name="Donations", value=history, inline=False)

    return embed

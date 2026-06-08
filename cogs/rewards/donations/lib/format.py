"""Embed and line builders for the ``~donations`` cog responses.

Pure sync functions that take ORM rows and return strings or
:class:`discord.Embed` objects — no DB access. The cog imports these and
hands the results to ``ctx.reply`` / the ``Paginator`` view.

PUA characters (spec line 12: backtick blocks containing Plane 15
codepoints like ``󰀀󰄀…``) round-trip correctly through Python ``str``
slicing — each is a single codepoint. The truncator below uses plain
``len`` / slice semantics, so backtick blocks may be cut mid-block on
display lines; the full text is preserved on the ``info`` view.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import discord

from lib.emerald import format_emeralds_as_stx
from orm import Donation, MinecraftAccount

# Truncate comments on paginated list views; full text on ``~donations info``.
COMMENT_TRUNC = 60


def truncate_comment(comment: Optional[str], limit: int = COMMENT_TRUNC) -> str:
    """Trim a comment for display in paginated list views.

    Returns the empty string when ``comment`` is None or blank. Adds an
    ellipsis (``…``) when truncation happens.
    """
    if not comment:
        return ""
    text = comment.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _discord_relative_ts(dt: datetime) -> str:
    """Render a Python datetime as a Discord ``<t:..:R>`` relative timestamp."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:R>"


def format_recent_line(d: Donation) -> str:
    """One line per donation for ``~donations recent``.

    Shape: ``#{id} **{mc_username}** — {value as stx} — {truncated comment}``
    """
    base = (
        f"#{d.id} **{d.recipient_mc.mc_username}** — "
        f"{format_emeralds_as_stx(d.value_emeralds)}"
    )
    snippet = truncate_comment(d.comment)
    if snippet:
        base += f" — {snippet}"
    return base


def format_leaderboard_line(rank: int, mc: MinecraftAccount, total: int) -> str:
    """One line per recipient for ``~donations leaderboard``.

    Shape: ``{rank}. **{mc_username}** — {total as stx}``
    """
    return f"{rank}. **{mc.mc_username}** — {format_emeralds_as_stx(total)}"


def format_highest_line(rank: int, d: Donation) -> str:
    """One line per donation for ``~donations highest``.

    Shape: ``{rank}. #{id} **{mc_username}** — {value as stx} — {comment snippet}``
    """
    base = (
        f"{rank}. #{d.id} **{d.recipient_mc.mc_username}** — "
        f"{format_emeralds_as_stx(d.value_emeralds)}"
    )
    snippet = truncate_comment(d.comment)
    if snippet:
        base += f" — {snippet}"
    return base


def build_info_embed(d: Donation, image_urls: list[str]) -> list[discord.Embed]:
    """Build the embed list for ``~donations info <id>``.

    Returns a primary embed with full metadata + comment, plus one image
    embed per attached URL (Discord only renders one image per embed; by
    sharing the same ``url`` across embeds, Discord groups them visually
    as a gallery).
    """
    primary = discord.Embed(
        title=f"Donation #{d.id}",
        description=d.comment or None,
    )
    primary.add_field(
        name="Recipient",
        value=f"**{d.recipient_mc.mc_username}** (`{d.recipient_mc.uuid}`)",
        inline=False,
    )
    primary.add_field(
        name="Value",
        value=format_emeralds_as_stx(d.value_emeralds),
        inline=True,
    )
    primary.add_field(
        name="Recorded by",
        value=f"<@{d.recorder_disc_uuid}>",
        inline=True,
    )
    primary.add_field(
        name="When",
        value=_discord_relative_ts(d.created_at),
        inline=True,
    )

    # First image goes on the primary embed; additional images become
    # sibling embeds sharing the same .url so Discord groups them.
    gallery_url = f"https://discord.com/donation/{d.id}"  # sentinel; not navigated
    embeds: list[discord.Embed] = [primary]
    if image_urls:
        primary.url = gallery_url
        primary.set_image(url=image_urls[0])
        for url in image_urls[1:]:
            extra = discord.Embed(url=gallery_url)
            extra.set_image(url=url)
            embeds.append(extra)
    return embeds

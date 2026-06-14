"""Embed / line builders for the ``~ctp`` cog responses.

Pure functions that take ORM rows and return strings or
``discord.Embed`` objects — no DB access. The cog imports these and
hands the results to ``ctx.reply`` / the ``Paginator`` view.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

import discord

from cogs.rewards.ctp.lib import balance as balance_svc
from cogs.rewards.ctp.lib import boards as boards_svc
from cogs.rewards.ctp.lib import link_bonus
from orm import CTPBoard, CTPLedger, CTPPrize


def _ts(dt: datetime) -> str:
    """Render a UTC datetime as a Discord relative timestamp (``<t:..:R>``)."""
    return f"<t:{int(dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp())}:R>"


def _abs_ts(dt: datetime) -> str:
    """Render a UTC datetime as a Discord short-date timestamp (``<t:..:f>``)."""
    return f"<t:{int(dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp())}:f>"


def _board_tag(board: Optional[CTPBoard], snapshot_category: Optional[str]) -> str:
    """The bracketed tag at the start of a history line.

    Prefers the live ``board.enum`` (so a renamed board shows the new
    name), falls back to the category snapshot for non-board sources
    (``PAY`` for redeems, ``GIFT`` for gifts, ``GLINT`` for invests,
    ``ADMIN`` for set), or ``N/A`` for board-less rewards.
    """
    if board is not None:
        return board.enum
    return snapshot_category or "N/A"


def format_history_lines(entries: Iterable[CTPLedger]) -> list[str]:
    """Render each ledger row to one (or two) strings for the
    ``~ctp history`` paginator. Mirrors the spec format at chore_torn_palace.md
    lines 19–27.
    """
    out: list[str] = []
    for e in entries:
        src = e.source
        if src == balance_svc.SOURCE_REWARD:
            tag = _board_tag(e.board, None)
            task = f"task #{e.task_number}" if e.task_number else "task"
            out.append(f"`[{tag}]` Completed {task}: received {e.amount_delta} points.")
            if e.comment:
                out.append(f"  - {e.comment}")
            continue
        if src == balance_svc.SOURCE_REDEEM:
            cat = e.prize_category_at_time or "PAY"
            display = e.prize_display_at_time or "(deleted prize)"
            out.append(f"`[PAY]` Redeemed {-e.amount_delta} points on [{cat}] {display}.")
            if e.expires_at is not None:
                out.append(f"  - This perk lasted until {_abs_ts(e.expires_at)}.")
            continue
        if src == balance_svc.SOURCE_GIFT_SENT:
            cp = f"<@{e.counterparty_disc_uuid}>" if e.counterparty_disc_uuid else "someone"
            out.append(f"`[GIFT]` Gave {-e.amount_delta} points to {cp}.")
            continue
        if src == balance_svc.SOURCE_GIFT_RECEIVED:
            cp = f"<@{e.counterparty_disc_uuid}>" if e.counterparty_disc_uuid else "someone"
            out.append(f"`[GIFT]` Received {e.amount_delta} points from {cp}.")
            continue
        if src == balance_svc.SOURCE_GLINT_INVEST:
            out.append(f"`[GLINT]` Invested {-e.amount_delta} points.")
            continue
        if src == balance_svc.SOURCE_ADMIN_SET:
            sign = "+" if e.amount_delta >= 0 else ""
            actor = f"<@{e.actor_disc_uuid}>" if e.actor_disc_uuid else "an admin"
            out.append(f"`[ADMIN]` Balance adjusted by {sign}{e.amount_delta} by {actor}.")
            if e.comment:
                out.append(f"  - {e.comment}")
            continue
        if src == balance_svc.SOURCE_LINK_BONUS:
            label = link_bonus.BONUS_LABELS.get(e.comment or "", "Account-link bonus")
            plural = "" if e.amount_delta == 1 else "s"
            out.append(f"`[BONUS]` {label}: received {e.amount_delta} point{plural}.")
            continue
        out.append(f"`[?]` ({src}) delta={e.amount_delta}")
    return out


def format_info_history_lines(entries: Iterable[CTPLedger]) -> list[str]:
    """Compact one-line-per-entry format for ``~ctp info <user>``.

    Mirrors spec line 113: ``[Date/time] +n points [[ART](url) [83](url)]``
    or ``[Date/time] -n points [GIFT:Naz]``. Uses Discord absolute
    timestamps so the renderer respects each viewer's locale.
    """
    out: list[str] = []
    for e in entries:
        ts = _abs_ts(e.created_at)
        sign = "+" if e.amount_delta >= 0 else ""
        tail = ""
        if e.source == balance_svc.SOURCE_REWARD and e.board is not None:
            board_link = f"[{e.board.enum}]({boards_svc.format_board_url(e.board)})"
            if e.task_number:
                task_link = f"[{e.task_number}]({boards_svc.format_task_url(e.task_number)})"
                tail = f" [{board_link} {task_link}]"
            else:
                tail = f" [{board_link}]"
        elif e.source == balance_svc.SOURCE_REWARD:
            tail = " [N/A]"
        elif e.source == balance_svc.SOURCE_REDEEM:
            display = e.prize_display_at_time or "(deleted)"
            cat = e.prize_category_at_time or "PAY"
            tail = f" [{cat}:{display}]"
        elif e.source == balance_svc.SOURCE_GIFT_SENT:
            cp = f"<@{e.counterparty_disc_uuid}>" if e.counterparty_disc_uuid else "?"
            tail = f" [GIFT:{cp}]"
        elif e.source == balance_svc.SOURCE_GIFT_RECEIVED:
            cp = f"<@{e.counterparty_disc_uuid}>" if e.counterparty_disc_uuid else "?"
            tail = f" [GIFT←{cp}]"
        elif e.source == balance_svc.SOURCE_GLINT_INVEST:
            tail = " [GLINT]"
        elif e.source == balance_svc.SOURCE_ADMIN_SET:
            tail = " [ADMIN]"
        elif e.source == balance_svc.SOURCE_LINK_BONUS:
            kind = e.comment or "?"
            tail = f" [BONUS:{kind}]"
        out.append(f"{ts} {sign}{e.amount_delta} points{tail}")
    return out


def format_glint_leaderboard_lines(
    glinted: list[tuple[discord.Member, int]],
    standby: list[tuple[discord.Member, int]],
) -> list[str]:
    """Build the ``~ctp glints bids`` body. Two sections mirroring the
    spec example at chore_torn_palace.md lines 75–86.
    """
    lines: list[str] = []
    if glinted:
        lines.append("**Currently Glinted:**")
        for i, (m, total) in enumerate(glinted, start=1):
            lines.append(f"{i}. {m.mention}: {total} points")
    else:
        lines.append("*No glinted users yet.*")
    if standby:
        lines.append("")
        lines.append("**Standby:**")
        offset = len(glinted)
        for i, (m, total) in enumerate(standby, start=offset + 1):
            lines.append(f"{i}. {m.mention}: {total} points")
    return lines


def format_duration(seconds: Optional[int]) -> str:
    """Render a prize duration as the spec phrases it: ``"one-time"``,
    ``"20 mins"``, ``"4 hours"``, ``"24h"``, ``"3 weeks"``, ``"1 month"``.
    Approximations match the seed payload (1 month = 30 days).
    """
    if seconds is None or seconds == 0:
        return "one-time"
    if seconds == 2592000:
        return "1 month"
    if seconds == 1814400:
        return "3 weeks"
    if seconds == 86400:
        return "24h"
    if seconds == 14400:
        return "4 hours"
    if seconds % 3600 == 0 and seconds < 86400:
        return f"{seconds // 3600} hours"
    if seconds % 60 == 0 and seconds < 3600:
        return f"{seconds // 60} mins"
    return f"{seconds}s"


def build_prize_info_embeds(
    prizes: list[CTPPrize],
    artwork_url: Optional[str],
) -> list[discord.Embed]:
    """Build one embed per category (Access / Change / Service / Gift)
    followed by a dedicated Glints page, each with a row-per-prize field.
    Attaches ``artwork_url`` to the first embed only so paginating to a
    later page doesn't re-render the image. Disclaimers, when present,
    are appended after the row.

    Glints aren't a catalog row — they're a top-N leaderboard payout
    (see ``../glints.py``) — so they don't go through ``~ctp prize add``
    and are rendered from a hard-coded block here.
    """
    by_category: dict[str, list[CTPPrize]] = {}
    for p in prizes:
        by_category.setdefault(p.category, []).append(p)

    # Categories are entirely admin-defined; render pages in
    # alphabetical order, then append the Glints page (the only
    # system-side rendering hook).
    populated = sorted(by_category)
    pages = len(populated) + 1  # +1 for the always-present Glints page

    embeds: list[discord.Embed] = []
    for page_idx, category in enumerate(populated, start=1):
        embed = discord.Embed(title=f"CTP Prizes — {category}")
        for p in by_category[category]:
            # Disabled rows are only surfaced when build_prize_info_embeds was
            # fed the admin view (~ctp prize info as admin). Mark them so
            # admins can tell at a glance which rows the user-facing list omits.
            if p.disabled:
                value = f"~~`{p.cost}` points · {format_duration(p.duration_seconds)}~~"
            else:
                value = f"`{p.cost}` points · {format_duration(p.duration_seconds)}"
            if p.disclaimer:
                value += f"\n*{p.disclaimer}*"
            name = f"{p.display}  (`{p.enum_name}`)"
            if p.disabled:
                name = f"{name} — (disabled)"
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text=f"Page {page_idx}/{pages}")
        if page_idx == 1 and artwork_url:
            embed.set_image(url=artwork_url)
        embeds.append(embed)

    glints_page = pages
    glints_embed = discord.Embed(title="CTP Prizes — Glints")
    glints_embed.add_field(
        name="Glints  (`GLINTS`)",
        value=(
            "Awarded to the **top 8** users by cumulative glint investment.\n"
            "Eligibility: current vets members, waitlist, or honourary.\n"
            "\n"
            "Use `~ctp glints bid <points>` to invest (cumulative and irreversible).\n"
            "Use `~ctp glints bids` to view current standings."
        ),
        inline=False,
    )
    glints_embed.set_footer(text=f"Page {glints_page}/{pages}")
    if glints_page == 1 and artwork_url:
        glints_embed.set_image(url=artwork_url)
    embeds.append(glints_embed)
    return embeds


def format_status_lines(
    *,
    balance: int,
    glint_rank: Optional[int],
    is_glinted: bool,
    glint_total: int,
    board_enums: list[str],
) -> list[str]:
    """Build the ``~ctp status`` body. Mirrors chore_torn_palace.md
    lines 8–15.
    """
    lines = [f"You have **{balance}** points in the chore-torn palace!"]
    if glint_rank is None:
        if glint_total > 0:
            lines.append(
                f"You have invested {glint_total} points in glints but are not currently eligible for the leaderboard."
            )
        else:
            lines.append("You have not invested any points in glints yet.")
    else:
        verb = "are" if is_glinted else "are not"
        lines.append(
            f"You are number **{glint_rank}** on the glint board and **{verb}** glinted!"
        )
    if board_enums:
        lines.append("You have joined the following CTP boards:")
        for e in board_enums:
            lines.append(f"- {e}")
    return lines


def format_active_redemptions(
    rows: list[CTPLedger],
    disc_uuid_to_member: dict[str, discord.Member],
) -> list[str]:
    """Group active redemptions by prize and render a per-prize "who's
    inside" block. ``disc_uuid_to_member`` is supplied by the caller so
    this helper stays sync / pure.
    """
    by_prize: dict[str, list[CTPLedger]] = {}
    for r in rows:
        key = r.prize_display_at_time or "(deleted prize)"
        by_prize.setdefault(key, []).append(r)

    lines: list[str] = []
    for prize_name, entries in by_prize.items():
        lines.append(f"**{prize_name}**")
        for e in entries:
            uuid = e.discord_account.disc_uuid
            who = disc_uuid_to_member.get(uuid)
            mention = who.mention if who is not None else f"<@{uuid}>"
            ends = _ts(e.expires_at) if e.expires_at else "(no expiry)"
            lines.append(f"- {mention}'s access ends {ends}")
        lines.append("")
    return lines

"""Generic helpers shared across every Return.

Truly cross-event concerns only:

* :func:`tier_allows` / :func:`manage_tier_allows` — resolve a
  :class:`Tier` against the predicates in :mod:`lib.auth`. "Higher tier
  always satisfies lower" is enforced (matches the docstring of ``lib.auth``).
* :func:`send_feedback` and :func:`send_help` — output delivery for
  ``~manage_return``. The text command has no native "ephemeral", so
  feedback is either kept in-channel (in the public-log channel or a DM,
  where persistence is desired) or DM'd to the invoker (everywhere else),
  with a non-spoiling fallback if DMs are closed.

Per-event domain helpers (cults, story mechanics, future events) live
under :mod:`cogs.events.returns.lib` so this file doesn't grow into a
catch-all as the Returns count climbs past 73.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from cogs.events.returns import (
    MANAGE_REGISTRY,
    ManageSubcommand,
    Tier,
    UserCustomCheck,
)
from lib.auth import (
    _has_admin_perm,
    _has_guild_role,
    _has_registered_role,
    _has_staff_role,
    _is_operator,
    _resolve_member,
)

logger = logging.getLogger("dazebot.cogs.events.returns._common")


def tier_allows(
    user: discord.abc.User,
    tier: Tier,
    custom_check: Optional[UserCustomCheck] = None,
    *,
    client: Optional[discord.Client] = None,
) -> bool:
    """True if ``user`` meets ``tier`` (with higher tiers satisfying lower).

    Resolves the user's effective tier from highest to lowest. A user who
    qualifies at level X also passes any required ``tier <= X``.

    If ``custom_check`` is provided it acts as an OR-override: a user who
    would fail the tier but satisfies the custom predicate still passes.
    Used by Return 73 to admit ``STORY_ROLE_IDS`` holders.

    ``client`` lets us look up the user's :class:`discord.Member` in
    ``CurrConfig.GUILD`` when ``user`` is a bare :class:`discord.User` (DM
    invocation). Without it, a non-Member user only passes the operator
    (id-based) check — same as before.
    """
    member = _resolve_member(user, client)
    if custom_check is not None and custom_check(
        member if member is not None else user, client
    ):
        return True
    if _is_operator(user):
        return True  # OPERATOR satisfies every tier
    if member is None:
        return False  # DM without guild membership: nothing role-based passes
    if _has_admin_perm(member):
        return tier <= Tier.ADMIN
    if _has_staff_role(member):
        return tier <= Tier.STAFF
    if _has_guild_role(member):
        return tier <= Tier.GUILD
    if _has_registered_role(member):
        # Reached only when the user holds ROLE_REGISTERED but no guild role
        # (guild-role users took the earlier branch).
        return tier <= Tier.REGISTERED
    return False


async def manage_tier_allows(ctx: commands.Context, sub: ManageSubcommand) -> bool:
    """Manage-subcommand variant: tier check plus async custom check."""
    if not tier_allows(ctx.author, sub.tier, client=ctx.bot):
        return False
    if sub.custom_check is not None:
        try:
            if not await sub.custom_check(ctx):
                return False
        except Exception:
            logger.exception("manage_return custom_check raised; denying")
            return False
    return True


MESSAGE_LIMIT = 2000
"""Discord's per-message content cap. Exceeding it raises 50035."""


def _split_for_discord(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into <=``limit`` chunks on line boundaries.

    A single line longer than ``limit`` is hard-sliced rather than dropped —
    correctness beats prettiness for staff dumps.
    """
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            for i in range(0, len(line), limit):
                piece = line[i : i + limit]
                if len(piece) == limit:
                    out.append(piece)
                else:
                    cur = piece
            continue
        add = line if not cur else "\n" + line
        if cur and len(cur) + len(add) > limit:
            out.append(cur)
            cur = line
        else:
            cur += add
    if cur:
        out.append(cur)
    return out


async def send_feedback(
    ctx: commands.Context,
    text: str,
    *,
    persist: bool,
) -> None:
    """Deliver ``text`` to the invoker of a ``~manage_return`` command.

    * ``persist=True`` (public-log channel or DM with the bot): reply in
      the channel so the result stays visible alongside the original
      command.
    * ``persist=False`` (anywhere else): DM the invoker. If DMs are closed,
      fall back to a non-revealing nudge in the channel.

    Content longer than :data:`MESSAGE_LIMIT` is split on line boundaries
    and sent as consecutive messages (the first still ``reply``-anchored
    to the invoker's command, the rest as plain follow-ups).
    """
    chunks = _split_for_discord(text)
    if persist:
        try:
            await ctx.reply(chunks[0], mention_author=False)
            for chunk in chunks[1:]:
                await ctx.channel.send(chunk)
        except discord.HTTPException:
            logger.exception("send_feedback: in-channel reply failed")
        return

    try:
        for chunk in chunks:
            await ctx.author.send(chunk)
        return
    except discord.Forbidden:
        pass
    except discord.HTTPException:
        logger.exception("send_feedback: DM failed")
        return

    try:
        await ctx.reply(
            "I couldn't DM you. Open your DMs to receive results from "
            "`~manage_return`.",
            mention_author=False,
            delete_after=15,
        )
    except discord.HTTPException:
        pass


async def send_help(
    ctx: commands.Context,
    week: int,
    *,
    persist: bool,
) -> None:
    """Auto-generate a help listing for ``~manage_return <week> help``.

    Lists every sub-command the caller's tier (plus any custom check)
    allows. Sub-command order is the registration order (insertion into
    :data:`MANAGE_REGISTRY`).
    """
    subs = MANAGE_REGISTRY.get(week, {})
    if not subs:
        await send_feedback(
            ctx,
            f"No `~manage_return` subcommands are registered for week {week}.",
            persist=persist,
        )
        return

    visible: list[tuple[str, ManageSubcommand]] = []
    for name, sub in subs.items():
        if await manage_tier_allows(ctx, sub):
            visible.append((name, sub))

    if not visible:
        await send_feedback(
            ctx,
            f"You don't have access to any `~manage_return {week}` subcommands.",
            persist=persist,
        )
        return

    lines = [f"**`~manage_return {week}` — subcommands available to you:**"]
    for name, sub in visible:
        usage = f" {sub.usage}" if sub.usage else ""
        help_line = f" — {sub.help}" if sub.help else ""
        lines.append(f"- `{name}{usage}`{help_line}")
    await send_feedback(ctx, "\n".join(lines), persist=persist)


PERSIST_CHANNEL_ID = 1313786561145606216
"""Channel where ``~manage_return`` commands and responses persist (not auto-deleted)."""


def is_persist_context(ctx: commands.Context) -> bool:
    """True if ``ctx`` is in the public-log channel or a DM with the bot."""
    if isinstance(ctx.channel, discord.DMChannel):
        return True
    return getattr(ctx.channel, "id", None) == PERSIST_CHANNEL_ID

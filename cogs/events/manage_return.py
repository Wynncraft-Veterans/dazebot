"""``~manage_return <week> <subcommand> [args...]`` text command.

Per-week management surfaces (cult creation, broadcasts, force-moves, the
collaborative-story dump, etc.) live behind their week's
``@register_manage`` decorators in ``cogs/events/returns/week_N.py``. This
cog routes the bare ``~manage_return`` text invocation to the right one,
applies the per-subcommand tier and custom checks, and handles the
"invoker message + feedback should be ephemeral *except* in the public-log
channel or a DM" delivery rules from TASK.md.

Text-only by design (not a hybrid command): the slash form would force
typed parameters and lose the multi-word ``<message>`` flexibility staff
need. The root gate is STAFF; individual subcommands raise the bar
further.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from bot import Bot
from cogs.events.returns import MANAGE_REGISTRY
from cogs.events.returns._common import (
    is_persist_context,
    manage_tier_allows,
    send_feedback,
    send_help,
)
from lib.auth import is_staff

logger = logging.getLogger("dazebot.cogs.events.manage_return")


class ManageReturn(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Initialized ManageReturn")

    @commands.command(name="manage_return")
    @is_staff()
    async def manage_return(
        self,
        ctx: commands.Context,
        week: int,
        subcommand: str = "help",
        *args: str,
    ) -> None:
        """Dispatch to a per-week manage subcommand.

        ``~manage_return <week>`` with no subcommand prints the auto-help
        for that week (filtered to the caller's tier).
        """
        persist = is_persist_context(ctx)
        try:
            await self._dispatch(ctx, week, subcommand, args, persist)
        finally:
            if not persist:
                try:
                    await ctx.message.delete()
                except (discord.Forbidden, discord.NotFound, AttributeError):
                    pass

    async def _dispatch(
        self,
        ctx: commands.Context,
        week: int,
        subcommand: str,
        args: tuple[str, ...],
        persist: bool,
    ) -> None:
        subs = MANAGE_REGISTRY.get(week, {})
        if not subs:
            await send_feedback(
                ctx,
                f"No `~manage_return` subcommands are registered for week {week}.",
                persist=persist,
            )
            return

        if subcommand == "help":
            await send_help(ctx, week, persist=persist)
            return

        sub = subs.get(subcommand)
        if sub is None:
            await send_feedback(
                ctx,
                f"Unknown subcommand `{subcommand}` for week {week}. "
                f"Try `~manage_return {week} help`.",
                persist=persist,
            )
            return

        if not await manage_tier_allows(ctx, sub):
            await send_feedback(
                ctx,
                f"You don't have access to `~manage_return {week} {subcommand}`.",
                persist=persist,
            )
            return

        try:
            await sub.callback(ctx, list(args))
        except Exception:
            logger.exception(
                "manage_return week=%s subcommand=%s raised", week, subcommand
            )
            await send_feedback(
                ctx,
                f"`{subcommand}` raised an exception. Check the logs.",
                persist=persist,
            )

    @manage_return.error
    async def manage_return_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        # The root @is_staff() check failing is the common case — keep the
        # response quiet (the invoker isn't supposed to know the command
        # exists if they can't run it).
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                "Usage: `~manage_return <week> [subcommand] [args...]`",
                mention_author=False,
                delete_after=15,
            )
            return
        logger.exception("manage_return: unhandled error", exc_info=error)


async def setup(bot: Bot):
    await bot.add_cog(ManageReturn(bot))
    logger.info("Loaded ManageReturn")

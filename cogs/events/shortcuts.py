"""Slash-command shortcuts that delegate into Returns.

Lives alongside ``return_cmd.py`` so it's obvious where shortcuts plug in
when a new Return needs one. Future entries register more
``@commands.hybrid_command``s here.

``/joincult`` is the canonical example: it has no parameters of its own —
it just opens Return 0's button UI, which already handles "you're already
in a cult", figurehead exclusion, and cult-picker rendering.
"""

import logging

from discord.ext import commands

from bot import Bot
from cogs.events.returns import REGISTRY
from cogs.events.returns._common import tier_allows

logger = logging.getLogger("dazebot.cogs.events.shortcuts")


class Shortcuts(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="joincult",
        description="Open the cults UI (alias for /return 0).",
    )
    async def joincult(self, ctx: commands.Context) -> None:
        handler = REGISTRY.get(0)
        if handler is None:
            await ctx.reply(
                "Return 0 isn't loaded. Try again in a moment.", ephemeral=True
            )
            return
        if not tier_allows(ctx.author, handler.tier, handler.custom_check):
            await ctx.reply(
                "You need to be registered to join a cult.", ephemeral=True
            )
            return
        await handler.callback(ctx)


async def setup(bot: Bot):
    await bot.add_cog(Shortcuts(bot))
    logger.info("Loaded Shortcuts")

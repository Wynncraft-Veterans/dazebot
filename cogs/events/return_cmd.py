"""``/return <int>`` dispatcher cog.

The slash command's only parameter is the integer week id. The per-week
handler (registered via :func:`cogs.events.returns.register`) is responsible
for the full UI — typically an ephemeral message with event-specific
buttons. Per-week admin actions live behind ``~manage_return`` instead.

This cog also bootstraps two startup tasks that need the bot to be ready:

* :func:`cogs.events.returns.week_0.backfill_thread_ids_from_dict` — seed
  the new ``Cult.thread_id`` column for the six grandfathered cults from
  the legacy ``cogs.events.returns.lib.cult_threads.CULT_THREADS`` map.
* :func:`cogs.events.returns.week_0.backfill_cult_threads` — sync
  CultMembership rows against Discord's view of each cult thread.
"""

import logging

from discord.ext import commands

from bot import Bot
from cogs.events.returns import REGISTRY
from cogs.events.returns._common import tier_allows
from cogs.events.returns.week_0 import (
    backfill_cult_threads,
    backfill_thread_ids_from_dict,
)

logger = logging.getLogger("dazebot.cogs.events.return_cmd")


class Returns(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Initialized Returns")

    async def cog_load(self) -> None:
        # Threads need the gateway-cached parent channel; wait for ready.
        self.bot.loop.create_task(self._run_startup_backfill())

    async def _run_startup_backfill(self) -> None:
        await self.bot.wait_until_ready()
        try:
            n = await backfill_thread_ids_from_dict()
            if n:
                logger.info("backfilled Cult.thread_id for %d row(s) from CULT_THREADS", n)
        except Exception:
            logger.exception("thread_id backfill failed")
        try:
            await backfill_cult_threads(self.bot)
        except Exception:
            logger.exception("cult thread backfill failed")

    @commands.hybrid_command(name="return")
    async def return_cmd(
        self,
        ctx: commands.Context,
        id: commands.Range[int, 0, 32767],
    ):
        """Open the week-``id`` Return."""
        handler = REGISTRY.get(id)
        if handler is None:
            await ctx.reply(f"No Return registered for week {id}.", ephemeral=True)
            return
        if not tier_allows(ctx.author, handler.tier, handler.custom_check):
            await ctx.reply(
                "You don't have access to this Return.", ephemeral=True
            )
            return
        await handler.callback(ctx)


async def setup(bot: Bot):
    await bot.add_cog(Returns(bot))
    logger.info("Loaded Returns")

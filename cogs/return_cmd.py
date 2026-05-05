import logging
from typing import Optional

from discord.ext import commands

from bot import Bot
from cogs import returns

logger = logging.getLogger("dazebot.cogs.return_cmd")


class Returns(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Initialized Returns")

    @commands.hybrid_command(name="return")
    async def return_cmd(
        self,
        ctx: commands.Context,
        id: commands.Range[int, 0, 32767],
        action: Optional[str] = None,
        cult: Optional[str] = None,
        owner: Optional[str] = None,
        flag: bool = False,
        # Future stubs — append optional params with defaults so existing
        # invocations keep working. Forwarded to per-week handlers as kwargs.
        # count: commands.Range[int, 0] = 0,
        # note: str = "",
    ):
        """Dispatch to the week-`id` `/return` handler."""
        await returns.dispatch(ctx, id, flag=flag, action=action, cult=cult, owner=owner)


async def setup(bot: Bot):
    await bot.add_cog(Returns(bot))
    logger.info("Loaded Returns")

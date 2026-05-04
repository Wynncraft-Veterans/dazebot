"""(Empty) API cog.

The former ``/joindate`` slash command has been merged into ``/info`` (see
``cogs/management.py``). This file remains as an empty cog so the bot's
extension loader doesn't need to be updated.
"""

import logging

from discord.ext import commands

from bot import Bot

logger = logging.getLogger("dazebot.cogs.api")


class API(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("API cog initialized (no commands; /joindate merged into /info)")


async def setup(bot: Bot):
    await bot.add_cog(API(bot))
    logger.info("API cog loaded successfully")

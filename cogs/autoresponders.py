import logging
import re

import discord
from discord.ext import commands

from bot import Bot

logger = logging.getLogger('discord.cogs.autoresponders')

BUILD_FORUM_CHANNEL_ID = 1359223973208002931
WYNNBUILDER_PATTERN = re.compile(r'(?:https?://)?\S*wynnbuilder\.github\.io\S*', re.IGNORECASE)


class Autoresponders(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Started Autoresponders")

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        if thread.parent_id != BUILD_FORUM_CHANNEL_ID:
            return

        # Fetch the starter (first) message of the forum post
        starter = thread.starter_message
        if starter is None:
            try:
                starter = await thread.fetch_message(thread.id)
            except discord.NotFound:
                return

        if WYNNBUILDER_PATTERN.search(starter.content):
            return

        await thread.send(
            "**Are you sure this is the right place?**\n"
            "The build forum is intended for people looking for feedback on specific builds "
            "or to share specific builds.\n\n"
            "If you are looking for general advice on builds, please instead post in the "
            "[build discussions thread]"
            "(https://discord.com/channels/1313769181321236490/1359228120623878394)!"
        )


async def setup(bot: Bot):
    await bot.add_cog(Autoresponders(bot))
    logger.info("Loaded Autoresponders")

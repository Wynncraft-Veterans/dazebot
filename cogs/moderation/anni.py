"""Anniversary listener: schedules and posts year-anniversary messages
for tracked Discord members."""

import logging
import discord
from discord.ext import commands
from bot import Bot


logger = logging.getLogger("dazebot.cogs.moderation.anni")

from config import CurrConfig


class Anni(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Initialized Anni Cog")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (
            message.guild is None
            or message.channel.id != CurrConfig.AnniConfig.CHANNEL_ID
            or message.webhook_id != CurrConfig.AnniConfig.WEBHOOK_ID
        ):
            return

        if CurrConfig.AnniConfig.TRIGGER in message.content:
            try:
                await message.channel.send(
                    f"<@&{CurrConfig.AnniConfig.ROLE_ID}>", allowed_mentions=discord.AllowedMentions(roles=True)
                )
            except Exception:
                logger.exception("Something unusual happened.")


async def setup(bot: Bot):
    await bot.add_cog(Anni(bot))
    logger.info("Loaded Anni Cog")

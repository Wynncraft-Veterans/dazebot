import logging
import discord
from discord.ext import commands
from bot import Bot

logger = logging.getLogger('discord.cogs.anni')

ROLE_ID = 1457366058951249970
CHANNEL_ID = 1339393368672702567
WEBHOOK_ID = 1396669909077070007
TRIGGER = '@Prelude to Annihilation'


class Anni(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info('Initialized Anni Cog')

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore stuff
        if message.guild is None:
            return

        if message.channel.id != CHANNEL_ID:
            return

        # Only respond to messages from the anni server
        if message.webhook_id != WEBHOOK_ID:
            return

        # Look for string
        if TRIGGER in (message.content or ''):
            try:
                await message.channel.send(
                    f'<@&{ROLE_ID}>',
                    allowed_mentions=discord.AllowedMentions(roles=True)
                )
            except Exception:
                logger.exception('Something unusual happened.')


async def setup(bot: Bot):
    await bot.add_cog(Anni(bot))
    logger.info('Loaded Anni Cog')

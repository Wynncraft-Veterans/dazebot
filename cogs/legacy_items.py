import logging

import discord
from discord.ext import commands

from bot import Bot
from orm import LegacyWarning

# TODO: THIS ENTIRE IMPLEMENTATION IS AWFUL!
# THIS WHOLE COG SHOULD BE REWRITTEN LATER!

logger = logging.getLogger('discord.cogs.legacy_items')

# Channel where legacy items are posted
CHANNEL_ID = 1316957148332298260

# The warning text (kept verbatim from the request)
WARNING_TEXT = '''
# Welcome to the #legacy-trade-market!
-# Several potential buyers subscribe to this channel!
```diff
- BEFORE PUBLISHING ANYTHING PLEASE NOTE: 
```

## Published messages are sent without usernames!
-# You need to provide the buyer some way to contact you.
-# The best way to do so is to @mention yourself in the post! 

## Be brief and only post what you have!
-# Item name, details, etc. are useful; conversations are not.
-# If a collector from another discord server is interested, they will contact you.

## Include everything in one message!
-# There is a substantial rate limit on publishing wares.
-# Please only publish your message when you are sure it is complete!

> *If you broadcasted your previous announcement as a cross-server announcement before reading this, feel free to edit your previous message instead of sending a new one!*'''


class LegacyItems(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info('Initialized LegacyItems Cog')

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return
        if message.channel.id != CHANNEL_ID:
            return
        if message.author.bot:
            return

        user_id = str(message.author.id)

        already_warned = await LegacyWarning.exists(disc_id=user_id)
        if not already_warned:
            try:
                await message.author.send(WARNING_TEXT)
            except discord.Forbidden:
                logger.warning('Could not DM user %s (DMs disabled)', user_id)
            except Exception:
                logger.exception('Failed to DM legacy items warning')
            await LegacyWarning.create(disc_id=user_id)


async def setup(bot: Bot):
    await bot.add_cog(LegacyItems(bot))
    logger.info('Loaded LegacyItems Cog')

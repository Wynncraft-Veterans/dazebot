import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from bot import Bot

# TODO: THIS ENTIRE IMPLEMENTATION IS AWFUL!
# THIS WHOLE COG SHOULD BE REWRITTEN LATER!

logger = logging.getLogger('discord.cogs.legacy_items')

# Channel where legacy items are posted
CHANNEL_ID = 1316957148332298260
# Don't warn again for the same user within this delta
WARN_DELTA = timedelta(days=60)

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
        if not hasattr(self.bot.nosql, 'LEGACY_LAST_POSTS'):
            self.bot.nosql.LEGACY_LAST_POSTS = {}
        logger.info('Initialized LegacyItems Cog')

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return
        if message.channel.id != CHANNEL_ID:
            return
        if message.author.bot:
            return

        user_id = message.author.id
        now = datetime.now(timezone.utc)

        last = self.bot.nosql.LEGACY_LAST_POSTS.get(user_id)
        if last is None or (now - last) >= WARN_DELTA:
            try:
                await message.reply(WARNING_TEXT, mention_author=False)
            except Exception:
                logger.exception('Failed to send legacy items warning')
            self.bot.nosql.LEGACY_LAST_POSTS[user_id] = now


async def setup(bot: Bot):
    await bot.add_cog(LegacyItems(bot))
    logger.info('Loaded LegacyItems Cog')

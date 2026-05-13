"""Miscellaneous low-stakes commands."""

import logging

import aiohttp
import discord
from discord.ext import commands

from bot import Bot

logger = logging.getLogger("dazebot.cogs.meta.pointless")


class Pointless(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        # Lazy: created on first use so cog reloads don't leak the previous
        # session. Closed in cog_unload.
        self._session: aiohttp.ClientSession | None = None
        logger.info("Pointless cog initialized")

    async def cog_unload(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    @commands.hybrid_command(name="randomfact")
    async def randomfact(self, ctx: commands.Context):
        """Fetch a random useless fact."""
        url = "https://uselessfacts.jsph.pl/random.json?language=en"
        try:
            session = await self._get_session()
            async with session.get(url) as response:
                if response.status != 200:
                    await ctx.send("Failed to fetch a random fact.")
                    return
                data = await response.json()
        except Exception as e:
            logger.exception(f"Random fact is borked: {e}")
            await ctx.send("Random fact broke.")
            return

        fact_text = data.get("text")
        if not fact_text:
            logger.error(f"No fact text found in the response {data}")
            await ctx.send("Something went wrong.")
            return

        embed = discord.Embed(description=fact_text, color=0xD75BF4)
        await ctx.send(embed=embed)


async def setup(bot: Bot):
    await bot.add_cog(Pointless(bot))
    logger.info("Pointless cog loaded successfully")

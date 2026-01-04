import logging
import discord
from discord.ext import commands
from bot import Bot
from lib.wynn_api.requestor import Requestor

logger = logging.getLogger('discord.cogs.pointless')
requestor = Requestor()


class Pointless(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Started Fun")

    @commands.hybrid_command(name='randomfact')
    async def randomfact(self, ctx: commands.Context):
        """Fetch a random useless fact."""
        url = "https://uselessfacts.jsph.pl/random.json?language=en"
        try:
            response = await requestor.get(url)
            if response.status != 200:
                await ctx.send("Failed to fetch a random fact.")
                return

            data = await response.json()
            fact_text = data.get("text")
            if not fact_text:
                await ctx.send("How odd...")
                return

            embed = discord.Embed(description=fact_text, color=0xD75BF4)
            await ctx.send(embed=embed)
        except Exception as e:
            logger.exception("Random fact is borked.")
            await ctx.send("Random fact broke.")


async def setup(bot: Bot):
    await bot.add_cog(Pointless(bot))
    logger.info("Loaded Fun")

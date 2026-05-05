import logging
import discord
from discord.ext import commands
import requests
from bot import Bot

logger = logging.getLogger("dazebot.cogs.pointless")


class Pointless(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Started Fun")

    @commands.hybrid_command(name="randomfact")
    async def randomfact(self, ctx: commands.Context):
        """Fetch a random useless fact."""
        url = "https://uselessfacts.jsph.pl/random.json?language=en"
        try:
            response = requests.get(url)
            if response.status_code != 200:
                await ctx.send("Failed to fetch a random fact.")
                return

            data = response.json()
            fact_text = data.get("text")
            if not fact_text:
                logging.error(f"No fact text found in the response {data}")
                await ctx.send("Something went wrong.")
                return

            embed = discord.Embed(description=fact_text, color=0xD75BF4)
            await ctx.send(embed=embed)
        except Exception as e:
            logger.exception(f"Random fact is borked: {e}")
            await ctx.send("Random fact broke.")

async def setup(bot: Bot):
    await bot.add_cog(Pointless(bot))
    logger.info("Loaded Fun")

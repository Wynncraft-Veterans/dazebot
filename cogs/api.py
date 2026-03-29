import logging
from discord.ext import commands
from lib.wynn_api.player import get_player_main_stats

logger = logging.getLogger("dazebot.cogs.api")
from bot import Bot


class API(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("API cog initialized")

    @commands.hybrid_command(name="joindate", description="Sync slash commands")
    async def joindate(self, ctx: commands.Context, username_or_uuid: str):
        try:
            player = await get_player_main_stats(username_or_uuid)
            ts = int(player.firstJoin.timestamp())
            await ctx.send(f"Joindate for {player.username} is <t:{ts}:F>, which was <t:{ts}:R>")
        except Exception as e:
            logger.error(f"[/joindate] {e}")
            await ctx.send("That user probably does not exist")
            raise e


async def setup(bot: Bot):
    await bot.add_cog(API(bot))
    logger.info("API cog loaded successfully")

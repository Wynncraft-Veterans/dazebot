import logging
import discord
from discord.ext import commands
from bot import Bot

logger = logging.getLogger('discord.cogs.utility')


class ProferSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(
                label="armouring", description="Helmets and/or chestplates.", emoji="🪖"
            ),
            discord.SelectOption(
                label="tailoring", description="Leggings and/or boots.", emoji="👞"
            ),
            discord.SelectOption(
                label="jeweling", description="Bracelets, rings, and/or necklaces.", emoji="💍"
            ),
            discord.SelectOption(
                label="weaponsmithing", description="Spears an/or daggers.", emoji="🗡️"
            ),
            discord.SelectOption(
                label="woodworking", description="Wands, bows, and/or reliks.", emoji="🏹"
            ),
            discord.SelectOption(
                label="alchemism", description="Potions.", emoji="⚗️"
            ),
            discord.SelectOption(
                label="scribing", description="Scrolls.", emoji="📜"
            ),
            discord.SelectOption(
                label="cooking", description="Food.", emoji="🍗"
            ),
        ]
        super().__init__(
            placeholder="What do you need your profer to make?",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        # TODO: Findprofer based on cached api responses. Should include everyone able to craft dernic items separated into online and offline. Should also include everyone able to craft sky items, online and offline.
        embed = discord.Embed(
            title = "Profer list",
            description = 'Not yet reimplemented!',
            color = discord.Color.yellow()
        )
#            if onlineVoidProfers or onlineDernicProfers:
#                resultMessage += "```Online members:```\n"
#                if onlineDernicProfers:
#                    resultMessage += "**Able to do dernic " + profession + " crafts:**\n"
#                    for username in onlineDernicProfers:
#                        resultMessage += "- `" + username + "`\n"
#                if onlineVoidProfers:
#                    resultMessage += "\n**Able to do non-dernic " + profession + " crafts:**\n"
#                    for username in onlineVoidProfers:
#                        resultMessage += "- `" + username + "`\n"
#
#            if offlineVoidProfers or offlineDernicProfers:
#                resultMessage += "```Offline members:```\n"
#                if offlineDernicProfers:
#                    resultMessage += "**Able to do dernic " + profession + " crafts:**\n"
#                    for username in offlineDernicProfers:
#                        resultMessage += "- `" + username + "`\n"
#                if offlineVoidProfers:
#                    resultMessage += "\n**Able to do non-dernic " + profession + " crafts:**\n"
#                    for username in offlineVoidProfers:
#                        resultMessage += "- `" + username + "`\n"
                        
class ProferView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180.0)
        self.add_item(ProferSelect())


class Utility(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Utility cog initialized")

    @commands.hybrid_command(name='findprofer')
    async def findprofer(self, ctx: commands.Context):
        """Bring up a select to choose which profession you need a profer for."""
        view = ProferView()
        await ctx.send("Select the profession you need:", view=view)


async def setup(bot: Bot):
    await bot.add_cog(Utility(bot))
    logger.info("Utility cog loaded successfully")

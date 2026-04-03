import logging
import discord
from discord.ext import commands

from bot import Bot
from orm import DiscordAccount, MinecraftAccount, Waitlist
from orm import generate_token

logger = logging.getLogger("dazebot.cogs.auth")


async def _is_eligible(mc: MinecraftAccount) -> bool:
    if mc.guild == "Returners":
        return True
    if mc.is_honourary:
        return True
    on_waitlist = await Waitlist.filter(minecraft_account=mc).exists()
    return on_waitlist


class Auth(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Auth cog initialized")

    @discord.app_commands.command(name="auth", description="Get your bridge token.")
    async def auth(self, interaction: discord.Interaction):
        disc = (
            await DiscordAccount.filter(disc_uuid=str(interaction.user.id)).select_related("minecraft_account").first()
        )

        if disc is None or disc.minecraft_account is None:
            await interaction.response.send_message(
                "You are not linked to a Minecraft account. Use `/request_link` or `/link_code` first.",
                ephemeral=True,
            )
            return

        mc = disc.minecraft_account

        if not await _is_eligible(mc):
            await interaction.response.send_message(
                "You are not eligible for bridge access. You must be in the Returners guild, on the waitlist, or honourary.",
                ephemeral=True,
            )
            return

        if mc.token is None:
            mc.token = generate_token()
            await mc.save(update_fields=["token"])
            logger.info(f"Generated token for {mc.mc_username}")

        await interaction.response.send_message(
            f"Your bridge token is: `{mc.token}`\nUse `/unlock {mc.token}` in Minecraft.",
            ephemeral=True,
        )


async def setup(bot: Bot):
    await bot.add_cog(Auth(bot))
    logger.info("Auth cog loaded successfully")

"""Blocking cog: ``/block`` and ``/unblock``."""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from bot import Bot
from config import CurrConfig
from lib.auth import is_staff
from lib.resolve import resolve_target
from lib.role_state import force_to_registered_only
from orm import Blocklist

logger = logging.getLogger("dazebot.cogs.blocking")


class Blocking(commands.Cog):
    """Staff commands to manage the VETS blocklist."""

    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Blocking cog initialized")

    @commands.hybrid_command(name="block", description="(Staff) Add a user to the VETS blocklist.")
    @is_staff()
    async def block(self, ctx: commands.Context, target: str, *, reason: Optional[str] = None):
        member, mc = await resolve_target(ctx, target)
        if mc is None:
            await ctx.reply(
                f"No Minecraft account found for `{target}`. "
                "Use `/link set` to register them first if needed."
            )
            return

        existing = await Blocklist.filter(minecraft_account=mc).first()
        if existing is not None:
            await ctx.reply(f"`{mc.mc_username}` is already on the blocklist.")
            return

        await Blocklist.create(
            minecraft_account=mc,
            reason=reason,
            blocked_by_disc_uuid=str(ctx.author.id),
        )

        # Force role state to Registered if member is in the guild.
        if member is not None:
            try:
                await force_to_registered_only(member, reason="blocked")
            except discord.HTTPException as e:
                logger.warning(f"/block: role enforcement failed for {member}: {e}")

        # If the MC account is currently in the in-game guild, alert staff.
        in_game_alert_sent = False
        if mc.guild == "Returners":
            channel = self.bot.get_channel(CurrConfig.BLOCKLIST_ALERT_CHANNEL)
            if isinstance(channel, discord.TextChannel):
                await channel.send(
                    f"⚠️ Blocked user `{mc.mc_username}` (`{mc.uuid}`) is currently in the in-game "
                    f"guild Returners. They should be kicked.\n_(blocked by {ctx.author.mention})_",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                in_game_alert_sent = True

        msg = f"\U0001f6ab Added `{mc.mc_username}` to the blocklist."
        if in_game_alert_sent:
            msg += " (Posted alert to in-game-guild channel.)"
        await ctx.reply(msg)

    @commands.hybrid_command(name="unblock", description="(Staff) Remove a user from the blocklist.")
    @is_staff()
    async def unblock(self, ctx: commands.Context, target: str):
        _, mc = await resolve_target(ctx, target)
        if mc is None:
            await ctx.reply(f"No Minecraft account found for `{target}`.")
            return
        deleted = await Blocklist.filter(minecraft_account=mc).delete()
        if not deleted:
            await ctx.reply(f"`{mc.mc_username}` was not on the blocklist.")
            return
        await ctx.reply(f"✅ Removed `{mc.mc_username}` from the blocklist.")


async def setup(bot: Bot):
    await bot.add_cog(Blocking(bot))
    logger.info("Blocking cog loaded successfully")

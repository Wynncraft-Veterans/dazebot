from __future__ import annotations

import logging
import discord
from discord.ext import commands
from tortoise.signals import Signals

from bot import Bot
from config import CurrConfig
from lib.vanity_roles import get_vanity_role_id
from orm import MinecraftAccount, DiscordAccount

logger = logging.getLogger("dazebot.cogs.vanity_roles")

VANITY_ROLE_IDS = set(int(role_id) for _, role_id in CurrConfig.VanityRolesConfig.CUTOFFS)

_cog_instance: VanityRoles | None = None


async def _minecraft_account_post_save(_sender, instance: MinecraftAccount, _created: bool, _using_db, _update_fields):
    if _cog_instance is None or instance.person_id is None:
        return
    disc = await DiscordAccount.filter(person_id=instance.person_id).first()
    if disc is not None:
        await _cog_instance._handle_member_uuid(int(disc.disc_uuid))


async def _discord_account_post_save(_sender, instance: DiscordAccount, _created: bool, _using_db, _update_fields):
    if _cog_instance is not None:
        await _cog_instance._handle_member_uuid(int(instance.disc_uuid))


MinecraftAccount.register_listener(Signals.post_save, _minecraft_account_post_save)
DiscordAccount.register_listener(Signals.post_save, _discord_account_post_save)


class VanityRoles(commands.Cog):
    def __init__(self, bot: Bot):
        global _cog_instance
        self.bot = bot
        _cog_instance = self
        logger.info("Started VanityRoles")

    def cog_unload(self):
        global _cog_instance
        _cog_instance = None
        logger.info("Unloaded VanityRoles, detached listeners")

    async def _handle_member_uuid(self, uuid: int):
        guild = self.bot.get_guild(self.bot.config.GUILD)
        if guild is None:
            logger.error("Guild not found")
            return
        member = guild.get_member(uuid)
        if member is None:
            logger.warning(f"Member {uuid} not found in guild, probably left")
            return
        await self.handle_member(member)

    async def handle_member(self, member: discord.Member):
        account = (
            await MinecraftAccount.filter(
                person__discord_accounts__disc_uuid=member.id,
            )
            .exclude(first_join=None)
            .order_by("first_join")
            .first()
        )

        if account is None:
            logger.info(f"{member} ({member.id}) has no linked minecraft account, skipping")
            return

        assert account.first_join is not None  # unreachable

        role_id_str = get_vanity_role_id(account.first_join, CurrConfig)
        if role_id_str is None:
            logger.info(f"{member} ({member.id}) has no vanity role for first_join={account.first_join}, skipping")
            return

        correct_role_id = int(role_id_str)
        current_vanity_roles = [r for r in member.roles if r.id in VANITY_ROLE_IDS]

        if len(current_vanity_roles) == 1 and current_vanity_roles[0].id == correct_role_id:
            logger.info(f"{member} ({member.id}) already has the correct vanity role, skipping")
            return

        if current_vanity_roles:
            await member.remove_roles(*current_vanity_roles)
            logger.info(f"Removed vanity role(s) {[r.name for r in current_vanity_roles]} from {member} ({member.id})")

        role = member.guild.get_role(correct_role_id)
        if role is None:
            logger.warning(f"{correct_role_id=} does not exist in guild, cannot assign to {member} ({member.id})")
            return

        await member.add_roles(role)
        logger.info(f"Assigned role {role.name} ({role.id}) to {member} ({member.id})")

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("Running startup check for existing members")

        guild = self.bot.get_guild(self.bot.config.GUILD)
        if guild is None:
            logger.error("Guild not found")
            return

        await guild.chunk()
        logger.info(f"Guild chunked, {guild.member_count} members in cache")

        checked = 0
        skipped = 0
        async for account in DiscordAccount.filter(person_id__not_isnull=True):
            member = guild.get_member(int(account.disc_uuid))
            if member is None:
                skipped += 1
                continue
            await self.handle_member(member)
            checked += 1

        logger.info(f"Startup check complete — checked {checked}, skipped {skipped} (not in guild)")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """In case someone rejoins the server but already has an existing Person"""
        logger.info(f"{member} ({member.id}) joined, checking vanity role")
        await self.handle_member(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Useful if a moderator accidentally removes someone's role"""
        if before.roles != after.roles:
            logger.info(f"{after} ({after.id}) roles changed, re-checking vanity role")
            await self.handle_member(after)


async def setup(bot: Bot):
    await bot.add_cog(VanityRoles(bot))
    logger.info("Loaded VanityRoles")

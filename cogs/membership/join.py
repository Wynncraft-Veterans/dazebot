"""Background-task cog for the linking/waitlist subsystem.

User-facing linking commands live in ``cogs/moderation/admin.py`` under
the ``/link`` group. Self-waitlist commands live in
``cogs/membership/waitlist.py`` under the ``/waitlist`` group. This cog
keeps only the periodic janitor loops:

* ``clear_old_requests`` — drop ``LinkRequest`` rows whose MC or Discord side
  has since been linked through some other channel.
* ``waitlist_cleanup`` — refresh stale waitlist entries against the Wynncraft
  API and drop entries that are now in a guild or have been inactive.
"""

from datetime import datetime, timedelta, timezone
import logging

from discord.ext import commands, tasks
from tortoise.expressions import Q

from bot import Bot
from lib.mc.wynn import check_player_full
from orm import LinkRequest, MinecraftAccount, Waitlist

logger = logging.getLogger("dazebot.cogs.membership.join")


class Join(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        self.clear_old_requests.start()
        self.waitlist_cleanup.start()
        logger.info("Join cog initialized")

    @tasks.loop(minutes=5)
    async def clear_old_requests(self):
        to_delete = await LinkRequest.filter(
            Q(minecraft_account__discord_account__isnull=False)
            | Q(discord_account__minecraft_account_id__isnull=False)
        ).values_list("id", flat=True)
        await LinkRequest.filter(id__in=to_delete).delete()

    @tasks.loop(minutes=1)
    async def waitlist_cleanup(self):
        now = datetime.now(tz=timezone.utc)

        mc_accounts = await MinecraftAccount.filter(
            Q(waitlist__isnull=False) & Q(last_manual_check__lt=now - timedelta(days=1))
        ).all()

        # TODO[005]: redundant? activity.py also resets MinecraftAccount.guild
        # just from `Returner` guild checks i think
        for mc in mc_accounts:
            await check_player_full(mc.uuid)

        to_delete = await Waitlist.filter(
            Q(minecraft_account__guild__isnull=False)
            | Q(minecraft_account__last_online__lt=now - timedelta(days=9))
        ).values_list("id", flat=True)
        await Waitlist.filter(id__in=to_delete).delete()

    def cog_unload(self):
        self.clear_old_requests.cancel()
        self.waitlist_cleanup.cancel()


async def setup(bot: Bot):
    await bot.add_cog(Join(bot))
    logger.info("Join cog loaded successfully")

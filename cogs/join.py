"""Background-task cog for the linking/waitlist subsystem.

User-facing linking commands moved to ``cogs/admin.py`` under the ``/link``
group. Self-waitlist commands moved to ``cogs/management.py`` under the
``/waitlist`` group. This cog keeps only the periodic janitor loops:

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
from lib.linking import _enforce_linked_baseline_for
from lib.wynn import check_player_full
from orm import DiscordAccount, LinkRequest, MinecraftAccount, Waitlist

logger = logging.getLogger("dazebot.cogs.join")


class Join(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        self.clear_old_requests.start()
        self.waitlist_cleanup.start()
        self.enforce_linked_baselines.start()
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

    @tasks.loop(minutes=5)
    async def enforce_linked_baselines(self):
        """Belt-and-braces sweep of the linked-account role invariant.

        For every ``DiscordAccount`` with a non-null ``minecraft_account_id``
        we re-run ``ensure_linked_baseline`` (via the linking helper). The
        helper is idempotent and a no-op when the member already has the
        correct primary role, so this loop is cheap.

        This closes the only remaining window where a linked user could end
        up with neither MEMBER nor REGISTERED:
          * race between ``try_consume_code`` and discord's member cache,
          * staff using ``/link set`` while the bot is briefly disconnected,
          * any future code path that creates a link without going through
            the central linking helper.
        """
        await self.bot.wait_until_ready()
        accounts = await DiscordAccount.filter(
            minecraft_account_id__isnull=False
        ).select_related("minecraft_account").all()
        if not accounts:
            return
        retried = 0
        for disc in accounts:
            mc = disc.minecraft_account
            if mc is None:
                continue
            try:
                ok = await _enforce_linked_baseline_for(self.bot, disc.disc_uuid, mc)
                if not ok:
                    retried += 1
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception(
                    "enforce_linked_baselines: unexpected failure for disc=%s mc=%s",
                    disc.disc_uuid, mc.uuid,
                )
        if retried:
            logger.info(
                "enforce_linked_baselines: %d/%d linked accounts could not be "
                "confirmed this pass (will retry next tick)",
                retried, len(accounts),
            )

    @enforce_linked_baselines.before_loop
    async def _before_enforce_linked_baselines(self):
        await self.bot.wait_until_ready()

    def cog_unload(self):
        self.clear_old_requests.cancel()
        self.waitlist_cleanup.cancel()
        self.enforce_linked_baselines.cancel()


async def setup(bot: Bot):
    await bot.add_cog(Join(bot))
    logger.info("Join cog loaded successfully")

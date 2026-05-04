import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import TYPE_CHECKING
import discord
from discord.ext import commands, tasks
from tortoise.expressions import Q

from config import CurrConfig
from lib.discord_paginated_embed import Paginator, from_lines
from lib.linking import dm_or_log, get_or_issue_code
from lib.auth import is_guild
from lib.wynn import check_player_full
from lib.wynn_api.player import get_player_full_stats
from lib.wynn_api.requestor import Requestor
from orm import DiscordAccount, LinkRequest, MinecraftAccount, UNKNOWN_LAST_ONLINE, Waitlist

logger = logging.getLogger("dazebot.cogs.join")
from bot import Bot

requestor = Requestor()


class Join(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        self.clear_old_requests.start()
        self.waitlist_cleanup.start()
        logger.info("Join cog initialized")

    # maybe use a command group?
    # make sure they know mc name is case sensitive.
    # undo a self link
    # check in activity.py to make sure non-guild people arent checked every 11 minutes

    @commands.hybrid_command(
        name="request_link",
        description="Request to link your discord account to a minecraft account (case-sensitive!).",
    )
    async def request_link(self, ctx: commands.Context, username_or_uuid: str):
        existing_disc = (
            await LinkRequest.filter(discord_account_id=ctx.author.id).prefetch_related("minecraft_account").first()
        )
        if existing_disc is not None:
            await ctx.reply(
                f"You ({ctx.author.mention}) already have a pending link request to `{existing_disc.minecraft_account.mc_username}` (`{existing_disc.minecraft_account.uuid}`)",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        existing_mc = (
            await LinkRequest.filter(
                Q(minecraft_account__uuid=username_or_uuid) | Q(minecraft_account__mc_username__iexact=username_or_uuid)
            )
            .prefetch_related("minecraft_account", "discord_account")
            .first()
        )
        if existing_mc is not None:
            await ctx.reply(
                f"You ({ctx.author.mention}) already have a pending link request to `{existing_mc.minecraft_account.mc_username}` (`{existing_mc.minecraft_account.uuid}`)",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        linked_mc = await MinecraftAccount.filter(discord_account__disc_uuid=str(ctx.author.id)).first()
        if linked_mc is not None:
            await ctx.reply(
                f"You ({ctx.author.mention}) are already linked to `{linked_mc.mc_username}` (`{linked_mc.uuid}`)",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        linked_disc = (
            await DiscordAccount.filter(
                Q(minecraft_account__uuid=username_or_uuid) | Q(minecraft_account__mc_username__iexact=username_or_uuid)
            )
            .prefetch_related("minecraft_account")
            .first()
        )
        if linked_disc is not None:
            if TYPE_CHECKING:
                assert linked_disc.minecraft_account is not None
            await ctx.reply(
                f"{ctx.author.mention} is already linked to `{linked_disc.minecraft_account.mc_username}` (`{linked_disc.minecraft_account.uuid}`)",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # TODO[003]: maybe make a function in `lib` that 1. checks if minecraft account exists (returns) 2. get stats 3. create row (returns)
        mc = await MinecraftAccount.filter(Q(uuid=username_or_uuid) | Q(mc_username__iexact=username_or_uuid)).first()

        if mc is None:
            fs = await get_player_full_stats(username_or_uuid)
            mc = await MinecraftAccount.create(
                uuid=fs.uuid,
                wynn_username=fs.username,
                mc_username=fs.username,
                # fs.lastJoin is None when the player has hidden it via Wynncraft
                # privacy. UNKNOWN_LAST_ONLINE is the in-band sentinel for that.
                last_online=fs.lastJoin or UNKNOWN_LAST_ONLINE,
                last_manual_check=UNKNOWN_LAST_ONLINE,
                first_join=fs.firstJoin,  # may be None per Wynncraft privacy opt-out
            )

        disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(ctx.author.id))

        await LinkRequest.create(
            minecraft_account=mc,
            discord_account=disc,
        )

        await ctx.reply(
            f"Link request created for {ctx.author.mention} → `{mc.mc_username}` (`{mc.uuid}`). A staff member will approve it.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="link_requests", description="")
    async def link_requests(self, ctx: commands.Context):
        lrs = await LinkRequest.all().prefetch_related("minecraft_account", "discord_account")
        lrs.sort(key=lambda e: e.created_at)
        lines = [
            f"{lr.id} - <t:{int(lr.created_at.timestamp())}:R> - <@{lr.discord_account.disc_uuid}> - `{lr.minecraft_account.mc_username}` (`{lr.minecraft_account.uuid}`)"
            for lr in lrs
        ]

        if not lines:
            await ctx.reply("No pending link requests.")
            return

        embeds = from_lines(
            title="Current link requests",
            lines=lines,
            lines_per_page=10,
            logger=logger,
        )

        await ctx.send(
            embed=embeds[0],
            view=Paginator(embeds),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="link_approve", description="")
    async def link_approve(self, ctx: commands.Context, id: str):
        lr = await LinkRequest.filter(id=id).prefetch_related("minecraft_account", "discord_account").first()
        if lr is None:
            await ctx.reply(f"That (`{id}`) id doesn't exist.")
            return

        disc = lr.discord_account
        mc = lr.minecraft_account

        disc.minecraft_account = mc
        await disc.save()
        await lr.delete()

        user = await self.bot.fetch_user(int(disc.disc_uuid))
        await ctx.reply(
            f"Linked {user.mention} to `{mc.mc_username}` (`{mc.uuid}`).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="link_code", description="Link your minecraft account.")
    async def link_code(self, ctx: commands.Context, username: str):
        # Reject if username already linked to anyone (including this user).
        existing_link = await MinecraftAccount.filter(mc_username__iexact=username).first()
        if existing_link is not None:
            owner = await DiscordAccount.filter(minecraft_account=existing_link).first()
            if owner is not None:
                if owner.disc_uuid == str(ctx.author.id):
                    await ctx.reply(
                        f"You are already linked to `{existing_link.mc_username}`.",
                        ephemeral=True,
                    )
                else:
                    await ctx.reply(
                        f"`{existing_link.mc_username}` is already linked to another Discord account.",
                        ephemeral=True,
                    )
                return

        row, is_new = await get_or_issue_code(str(ctx.author.id), username)

        body = (
            f"Your link code for **{username}**:\n\n"
            f"```\n{row.code}\n```\n"
            f"1. Connect to `{CurrConfig.MC_PUBLIC_HOST}` in Minecraft.\n"
            f"2. Type the code above into chat.\n\n"
            f"The code is persistent — it does not expire and you can re-run "
            f"`/link_code {username}` any time to see it again."
        )

        dmed = await dm_or_log(ctx.author, body, fallback_logger=logger)
        if dmed:
            verb = "issued" if is_new else "re-sent existing"
            await ctx.reply(
                f"Code {verb} via DM. Check your messages.",
                ephemeral=True,
            )
        else:
            await ctx.reply(body, ephemeral=True)

    @commands.hybrid_command(name="linking", description="Extra info on how to link")
    async def linking(self, ctx: commands.Context):
        # request_link is manual, has to be approved by staff manually, takes longer
        # link code is fully automated, uses the minimalistic rust server
        # this command should give a pretty embed explaining everything, including the server ip and how to link
        ...

    @tasks.loop(minutes=5)
    async def clear_old_requests(self):
        to_delete = await LinkRequest.filter(
            Q(minecraft_account__discord_account__isnull=False) | Q(discord_account__minecraft_account_id__isnull=False)
        ).values_list("id", flat=True)
        await LinkRequest.filter(id__in=to_delete).delete()

    @commands.hybrid_command(name="join_guild")
    @is_guild()
    async def join_guild(self, ctx: commands.Context):
        disc_uuid = ctx.author.id

        mc = await MinecraftAccount.filter(discord_account__disc_uuid=disc_uuid).first()
        if mc is None:
            await ctx.reply(
                "You have not linked your minecraft account to your discord account yet"
            )  # TODO[004]: more info on how to join
            return

        if mc.guild == "Returners":
            await ctx.reply(f"You (`{mc.mc_username}`) are already in the guild")
            return

        if mc.guild is not None:
            await ctx.reply(f"You (`{mc.mc_username}`) are already in a different guild called `{mc.guild}`")
            return

        lrq = await Waitlist.filter(minecraft_account__discord_account__disc_uuid=disc_uuid).first()
        if lrq:
            await ctx.reply(f"You (`{mc.mc_username}`) are already made a request to join, you are on the waitlist")
            return

        await Waitlist.create(minecraft_account=mc)

        await ctx.reply(f"You (`{mc.mc_username}`) are now on the waitlist!")

    @commands.hybrid_command(name="leave_waitlist")
    async def leave_waitlist(self, ctx: commands.Context):
        deleted = await Waitlist.filter(minecraft_account__discord_account__disc_uuid=str(ctx.author.id)).delete()

        if not deleted:
            await ctx.reply("You are not on the waitlist.")
            return

        await ctx.reply("You have been removed from the waitlist.")

    # NOTE: staff-side waitlist management (add / view / remove) lives in
    # cogs/management.py under the `/waitlist` hybrid group. Self-removal
    # (above) stays here because it's a regular-user command, not staff.

    @tasks.loop(minutes=1)
    async def waitlist_cleanup(self):
        now = datetime.now(tz=timezone.utc)

        mc_accounts = await MinecraftAccount.filter(
            Q(waitlist__isnull=False) & Q(last_manual_check__lt=now - timedelta(days=1))
        ).all()

        # TODO[005]: redundant? activity.py also resets MinecraftAccount.guild just from `Returner` guild checks i think
        for mc in mc_accounts:
            await check_player_full(mc.uuid)

        to_delete = await Waitlist.filter(
            Q(minecraft_account__guild__isnull=False) | Q(minecraft_account__last_online__lt=now - timedelta(days=9))
        ).values_list("id", flat=True)
        await Waitlist.filter(id__in=to_delete).delete()

    def cog_unload(self):
        self.clear_old_requests.cancel()
        self.waitlist_cleanup.cancel()


async def setup(bot: Bot):
    await bot.add_cog(Join(bot))
    logger.info("Join cog loaded successfully")

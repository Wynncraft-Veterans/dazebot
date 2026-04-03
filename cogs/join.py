import asyncio
from datetime import datetime, timezone
import logging
from random import randint
from typing import TYPE_CHECKING
import discord
from discord.ext import commands, tasks
from tortoise.expressions import Q

from lib.discord_paginated_embed import Paginator, from_lines
from lib.wynn_api.player import get_player_full_stats
from lib.wynn_api.requestor import Requestor
from lib.link_listener import PendingLink, link_listeners
from orm import DiscordAccount, LinkRequest, MinecraftAccount

logger = logging.getLogger("dazebot.cogs.join")
from bot import Bot

requestor = Requestor()


class Join(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        self.clear_old_requests.start()
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
                last_online=fs.lastJoin or datetime.fromtimestamp(0, tz=timezone.utc),
                last_manual_check=datetime.fromtimestamp(0, tz=timezone.utc),
                first_join=fs.firstJoin,
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
        key = username.lower()

        if key in link_listeners:
            await ctx.reply("There's already a pending link for that username. Wait for it to finish or time out.")
            return

        code = randint(100_000, 999_999)
        future: asyncio.Future[tuple[str, str, str]] = self.bot.loop.create_future()
        link_listeners[key] = PendingLink(future=future)

        await ctx.reply(f"1. Join `IP_HERE`\n2. Type in chat: `{code}`\n3. You have 1 minute")

        try:
            uuid, real_username, message = await asyncio.wait_for(future, timeout=60)
        except asyncio.TimeoutError:
            await ctx.reply("Link timed out. Run the command again to retry.")
            return
        finally:
            link_listeners.pop(key, None)

        existing = await DiscordAccount.filter(minecraft_account__uuid=uuid).first()
        if existing is not None and existing.disc_uuid != str(ctx.author.id):
            other = await self.bot.fetch_user(int(existing.disc_uuid))
            await ctx.reply(
                f"`{real_username}` is already linked to {other.mention}. Unlink them first.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(ctx.author.id))

        if disc.minecraft_account_id:
            other_mc = await MinecraftAccount.get(id=disc.minecraft_account_id)
            await ctx.reply(
                f"{ctx.author.mention} is already linked to `{other_mc.mc_username}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if str(code) in message:
            mc = await MinecraftAccount.filter(uuid=uuid).first()
            if mc is None:
                fs = await get_player_full_stats(uuid)
                mc = await MinecraftAccount.create(
                    uuid=uuid,
                    wynn_username=fs.username,
                    mc_username=real_username,
                    last_online=fs.lastJoin or datetime.fromtimestamp(0, tz=timezone.utc),
                    last_manual_check=datetime.fromtimestamp(0, tz=timezone.utc),
                    first_join=fs.firstJoin,
                )

            disc.minecraft_account = mc
            await disc.save()

            await ctx.reply(
                f"Linked {ctx.author.mention} to Minecraft account `{real_username}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.reply("Wrong code. Run the command again to retry.")

    @commands.hybrid_command(name="linking", description="Extra info on how to link")
    async def linking(self, ctx: commands.Context):
        # request_link is manual, has to be approved by staff manually, takes longer
        # link code is fully automated, uses the minimalistic rust server
        # this command should give a pretty embed explaining everything, including the server ip and how to link
        ...

    @tasks.loop(minutes=5)
    async def clear_old_requests(self):
        await LinkRequest.filter(
            Q(minecraft_account__discord_account_id__isnull=False)
            | Q(discord_account__minecraft_account_id__isnull=False)
        ).delete()

    # some fruma stuff:
    # - self add
    # - self remove
    # - staff add
    # - staff remove
    # - staff reorder (difficult)
    # - task: autoremove when:
    #   - unseen for 9+ days (check daily)
    #   - joined another guild (check daily)
    #   - already joined Vets (check every 15 minutes, just check local DB)

    # self add
    @commands.hybrid_command(name="join_guild")
    async def join_guild(self, ctx: commands.Context):
        # disc_uuid = ctx.author.id

        # # if already joined: "You are already in the guild"
        # mc = await MinecraftAccount.filter(Q(discord_account__disc_uuid=disc_uuid) & Q(guild="Returners")).first()

        # if mc:
        #     await ctx.reply(f"You (`{mc.mc_username}`) are already in the guild")

        # # if already attempting to join: "You already requested to join, you are on the waitlist"
        # lrq = Waitlist.filter(minecraft_account__)

        # # join waitlist
        # # if minecraft account already linked: print minecraft name
        # # if no minecraft account linked, suggest them to (complete) linking

        # mc_accounts = MinecraftAccount.filter(person__discord_accounts__disc_uuid=disc_uuid)
        ...

    # @commands.hybrid_group(name="fruma")

    def cog_unload(self):
        self.clear_old_requests.cancel()


async def setup(bot: Bot):
    await bot.add_cog(Join(bot))
    logger.info("Join cog loaded successfully")

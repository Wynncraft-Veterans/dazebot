import asyncio
from datetime import datetime, timedelta, timezone
import logging
import discord
from discord.ext import commands, tasks
from lib.discord_paginated_embed import Paginator, from_lines
from lib.wynn_api.guild import get_guild
from lib.wynn_api.player import get_player_main_stats
from orm import DiscordAccount, MinecraftAccount, Shout

logger = logging.getLogger("discord.cogs.activity")
from bot import Bot
from tortoise.expressions import Q

# TODO: Put into config rather than define here
ROLES_ALLOWED_TO_SHOUT = [1402295013169172500, 1436108975132119221, 1436109140195020892]


class Activity(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        self.check_guild.start()
        logger.info("Activity cog initialized")

    @tasks.loop(minutes=11)
    async def check_guild(self):
        logger.info("Doing check_guild task")
        guild = await get_guild("Returners")
        members = [*guild.members.all_members()]
        online = [m for m in members if m.online]
        offline = [m for m in members if not m.online]
        logger.info("checking online members")
        for member in online:
            await MinecraftAccount.update_or_create(
                uuid=member.uuid,
                defaults={
                    "guild": guild.name,
                    "username": member.username,
                    "last_online": datetime.now(),
                },
            )
        for member in offline:
            account, created = await MinecraftAccount.get_or_create(
                uuid=member.uuid,
                defaults={
                    "guild": guild.name,
                    "username": member.username,
                    "last_online": datetime.fromtimestamp(0),
                },
            )

            if not created:
                account.guild = guild.name
                account.username = member.username
                await account.save(update_fields=["guild", "username"])
        logger.info("members to check")
        members_to_check = await MinecraftAccount.filter(
            Q(
                last_online__lt=datetime.now() - timedelta(weeks=2),
                uuid__in=[m.uuid for m in offline],
            )
            | Q(uuid__not_in=[m.uuid for m in members])
        )
        logger.info(f"checking members {len(members_to_check)=}")
        for member in members_to_check:
            logger.info(f"{member.username=} {member.uuid=}")
            player = await get_player_main_stats(member.uuid)
            guild_name = player.guild.name if player.guild else None
            await MinecraftAccount.update_or_create(
                uuid=player.uuid,
                defaults={
                    "guild": guild_name,
                    "last_online": player.lastJoin,
                    "username": player.username,
                },
            )

        alerts = []
        now = datetime.now(timezone.utc)
        if (
            len(online) <= self.bot.config.GUILD_DEAD_WHEN
            and now - self.bot.nosql.LAST_DEAD_ALERT >= self.bot.config.GUILD_DEAD_ALERT_DELTA
        ):
            self.bot.nosql.LAST_DEAD_ALERT = now
            alerts.append(self._low_count_alert())

        if (
            guild.members.total >= self.bot.config.GUILD_FULL_WHEN
            and now - self.bot.nosql.LAST_CAP_ALERT >= self.bot.config.GUILD_FULL_ALERT_DELTA
        ):
            self.bot.nosql.LAST_CAP_ALERT = now
            alerts.append(self._guild_full_alert())

        logger.info(f"{len(online)=}")

        await asyncio.gather(*alerts)

    async def _low_count_alert(self):
        logger.warning("Low player count!")
        channel = self.bot.get_channel(self.bot.config.GUILD_DEAD_ALERT_CHANNEL)
        assert isinstance(channel, discord.TextChannel)
        embed = discord.Embed(
            title="Activity Alert",
            description="__**The guild is dead!**__\nWe are within the allowable shout period!\n\n> Who wants to claim this shout? :D\n> (Wen will pay, plus there are prizes!)",
            color=discord.Color.red(),
        )
        await channel.send(
            content=f"<@&{self.bot.config.GUILD_DEAD_ALERT_ROLE}>",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

    async def _guild_full_alert(self):
        logger.warning("Guild is full!")
        channel = self.bot.get_channel(self.bot.config.GUILD_FULL_ALERT_CHANNEL)
        assert isinstance(channel, discord.TextChannel)
        embed = discord.Embed(
            title="Capacity Alert",
            description="__**The guild is full!**__\nA chief needs to kick some people!",
            color=discord.Color.blurple(),
        )
        await channel.send(embed=embed)

    @commands.hybrid_command(name="force_check")
    @commands.has_permissions(administrator=True)
    async def force_check(self, _ctx: commands.Context):
        await self.check_guild()

    @commands.hybrid_command(name="purgelist")
    async def purgelist(self, ctx: commands.Context, days: int = 14):
        cutoff = datetime.now() - timedelta(days=14)
        absent_guild_members = await MinecraftAccount.filter(guild="Returners", last_online__lt=cutoff).order_by(
            "-last_online"
        )

        if not absent_guild_members:
            await ctx.send(f"No members have been away for more than {days} days.")
            return

        logger.info([m.last_online.tzinfo for m in absent_guild_members])
        logger.info(datetime.now().tzinfo)

        lines = [
            f"- `{m.username}` has been away for {(datetime.now(timezone.utc) - m.last_online).days} days."
            for m in reversed(absent_guild_members)
        ]

        embeds = from_lines("Purgelist", lines, 10, logger)
        await ctx.send(embed=embeds[0], view=Paginator(embeds))

    @commands.hybrid_command(name="shout")
    @commands.check_any(
        commands.has_permissions(manage_messages=True),
        commands.has_any_role(ROLES_ALLOWED_TO_SHOUT),  # type: ignore[arg-type]
    )
    async def shout(self, ctx: commands.Context):
        discord_acc, _ = await DiscordAccount.get_or_create(disc_uuid=str(ctx.author.id))
        await Shout.create(shouter=discord_acc)
        discord_acc.shout_count += 1
        await discord_acc.save(update_fields=["shout_count"])
        await ctx.send(f"Thank you for helping the guild, {ctx.author.mention}!\nYour shout has been recorded.")

    @commands.hybrid_command(name="last_shout")
    async def last_shout(self, ctx: commands.Context):
        shouts = await Shout.all().order_by("-created_at").limit(3).prefetch_related("shouter")
        lines = []
        for shout in shouts:
            delta = datetime.now(timezone.utc) - shout.created_at
            user = await self.bot.fetch_user(int(shout.shouter.disc_uuid))
            lines.append(f"{user.mention} - {delta.seconds // 3600} hours and {(delta.seconds // 60) % 60} minutes ago")

        if lines:
            await ctx.send("\n".join(lines), silent=True)
        else:
            await ctx.send("There have been no recorded shouts.")

    @commands.hybrid_command(name="shouterboard")
    async def shouterboard(self, ctx: commands.Context):
        from tortoise.functions import Count

        # Get counts with discord account info
        shout_counter = (
            await Shout.annotate(shout_count=Count("id")).group_by("shouter_id").values("shouter_id", "shout_count")
        )

        sorted_counts = sorted(shout_counter, key=lambda x: x["shout_count"], reverse=True)

        shouter_ids = [item["shouter_id"] for item in sorted_counts]
        shouters = await DiscordAccount.filter(id__in=shouter_ids)
        shouter_dict = {s.id: s for s in shouters}

        lines = []
        for item in sorted_counts:
            shouter = shouter_dict[item["shouter_id"]]
            user = await self.bot.fetch_user(int(shouter.disc_uuid))
            lines.append(f"{user.mention}: {item['shout_count']} shouts")

        if lines:
            embeds = from_lines("Shouterboard", lines, 10, logger)
            await ctx.send(embed=embeds[0], view=Paginator(embeds))
        else:
            await ctx.send("No shouts recorded yet.")

    @commands.hybrid_command(name="last_online")
    async def last_online(self, ctx: commands.Context, username_or_uuid: str):
        try:
            player = await MinecraftAccount.get(Q(uuid=username_or_uuid) | Q(username=username_or_uuid))
            ts = int(player.last_online.timestamp())
            await ctx.send(f"{player.username} was last online on <t:{ts}:F>, which was <t:{ts}:R>")
        except Exception as e:
            logger.error(f"[/last_online] {e}")
            await ctx.send("That user probably does not exist, is too new or is not in the guild.")
            raise e


async def setup(bot: Bot):
    await bot.add_cog(Activity(bot))
    logger.info("Activity cog loaded successfully")

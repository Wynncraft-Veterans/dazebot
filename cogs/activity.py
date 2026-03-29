import asyncio
from datetime import datetime, timedelta, timezone
import logging
import aiohttp
import discord
from discord.ext import commands, tasks
from lib.discord_paginated_embed import Paginator, from_lines
from lib.lib import ProfCategory
from lib.wynn_api.guild import get_guild
from lib.wynn_api.guild_models import BaseMember, Guild
from lib.wynn_api.player import get_player_full_stats
from lib.wynn_api.player_models import WynncraftPlayer
from lib.wynn_api.requestor import Requestor
from orm import DiscordAccount, MinecraftAccount, ProfessionCategories, Shout

logger = logging.getLogger("dazebot.cogs.activity")
from bot import Bot
from tortoise.expressions import Q

# TODO: Put into config rather than define here
ROLES_ALLOWED_TO_SHOUT = [1402295013169172500, 1436108975132119221, 1436109140195020892]


_session: aiohttp.ClientSession | None = None


async def get_session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def get_mc_username(uuid: str) -> str:
    session = await get_session()
    async with session.get(f"https://api.ashcon.app/mojang/v2/user/{uuid}") as res:
        data = await res.json()
        if "username" not in data:
            logger.error(f"For some reason `username` was not in data: {data=}")
            await asyncio.sleep(1)
            return await get_mc_username((uuid))
        return data["username"]


# honestly, such a small impl it doesnt need to be in wynn_api folder
# also because it doesnt have any pydantic classes wrapped around the result
async def get_server_players(server: str):
    requestor = Requestor()
    res = await requestor.get(f"https://api.wynncraft.com/v3/player?identifier=uuid&server={server}")
    data = await res.json()
    return [*data["players"].keys()]


class Activity(commands.Cog):
    bot: Bot
    queue: set[str]

    def __init__(self, bot: Bot):
        self.bot = bot
        self.check_guild.start()
        self.queue = set()
        logger.info("Activity cog initialized")

    async def cog_unload(self):
        global _session
        if _session and not _session.closed:
            await _session.close()

    async def _check_player_full(self, uuid: str) -> tuple[str, WynncraftPlayer, MinecraftAccount]:
        mc_username = await get_mc_username(uuid)
        player = await get_player_full_stats(uuid)
        account = await MinecraftAccount.get(uuid=player.uuid)
        # if player.guild is None:
        #     account.guild = None
        if account.last_online < player.lastJoin:
            account.last_online = player.lastJoin
        if player.firstJoin:
            account.first_join = player.firstJoin
        account.wynn_username = player.username
        account.mc_username = mc_username
        account.last_manual_check = datetime.now(timezone.utc)
        await account.save()
        # if guild_name is not None:
        #     await self._check_guild(guild_name)

        profs = {}
        if player.characters:
            for char in player.characters.values():
                for prof_type, prof in char.professions.items():
                    profs[prof_type] = max(profs.setdefault(prof_type, 0), prof.level)

        for prof_type, level in profs.items():
            _prof, _created = await ProfessionCategories.update_or_create(
                minecraft_account=account,
                prof_type=prof_type,
                defaults={
                    "category": ProfCategory(["pleb", "void", "dernic"][(level >= 100) + (level >= 103)]),
                },
            )

        return mc_username, player, account

    @tasks.loop(minutes=1)
    async def eat_queue(self): ...

    async def _apply_guild(self, guild: Guild, member: BaseMember):
        mc_username = await get_mc_username(member.uuid)
        account, created = await MinecraftAccount.get_or_create(
            uuid=member.uuid,
            defaults={
                "guild": guild.name,
                "wynn_username": member.username,
                "mc_username": mc_username,
                "last_online": datetime.now(timezone.utc)
                if member.online
                else datetime.fromtimestamp(0, tz=timezone.utc),
                "last_manual_check": datetime.fromtimestamp(0, tz=timezone.utc),
            },
        )

        if not created:
            account.guild = guild.name
            account.wynn_username = member.username
            account.mc_username = mc_username
            now = datetime.now(timezone.utc)
            if member.online and account.last_online <= now:
                account.last_online = now
            await account.save()

    async def _check_guild(self, guild_name_full: str) -> Guild:
        guild = await get_guild(guild_name_full)
        members = [*guild.members.all_members()]
        logger.info(f"checking online members {[m.username for m in members if m.online]}")

        tasks = []

        for member in members:
            tasks.append(self._apply_guild(guild, member))

        accounts = await MinecraftAccount.filter(Q(guild=guild_name_full) & ~Q(uuid__in=[m.uuid for m in members]))
        for account in accounts:
            account.guild = None
            await account.save()

        await asyncio.gather(*tasks)

        return guild

    @tasks.loop(minutes=11)
    async def check_guild(self):
        logger.info("Doing check_guild task")
        guild = await self._check_guild("Returners")
        online = [m for m in guild.members.all_members() if m.online]

        logger.info(f"members to check right now {[m.username for m in online]}")
        members_to_check = await MinecraftAccount.filter(
            Q(guild="Returners")
            & (
                Q(
                    last_online__lt=datetime.now(timezone.utc) - timedelta(days=9),  # TODO make configurable
                )
                | Q(last_manual_check__lt=datetime.now(timezone.utc) - timedelta(weeks=1))  # TODO make configurable
            )
        )

        guilds_to_check = set()

        logger.debug(f"checking members {len(members_to_check)=}")

        async def _check_member_helper(member: MinecraftAccount):
            logger.debug(f"STARTED _check_member_helper {member.wynn_username=} {member.uuid=}")
            _, player, _ = await self._check_player_full(member.uuid)

            if player.server and (
                player.lastJoin <= datetime.now(timezone.utc) - timedelta(days=9)
            ):  # TODO configurable
                logger.debug(f"checking {player.server}")
                if member.uuid in await get_server_players(player.server):
                    account = await MinecraftAccount.get(uuid=player.uuid)
                    account.last_online = datetime.now(timezone.utc)
                    await account.save()

            guild_name = player.guild.name if player.guild else None
            guilds_to_check.add(guild_name)
            logger.debug(f"FINSHED _check_member_helper {member.wynn_username=} {member.uuid=}")

        check_members_task = []
        for member in members_to_check:
            check_members_task.append(_check_member_helper(member))
            # await _check_member_helper(member)

        await asyncio.gather(*check_members_task)

        guilds_to_check.discard("Returners")

        tasks = []

        for guild in guilds_to_check:
            tasks.append(self._check_guild(guild))

        await asyncio.gather(*tasks)

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
    async def purgelist(self, ctx: commands.Context, days: int = 9):
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        absent_guild_members = await MinecraftAccount.filter(guild="Returners", last_online__lt=cutoff).order_by(
            "-last_online"
        )

        if not absent_guild_members:
            await ctx.send(f"No members have been away for more than {days} days.")
            return

        logger.info([m.last_online.tzinfo for m in absent_guild_members])
        logger.info(datetime.now().tzinfo)

        lines = [
            f"- `{m.wynn_username if m.wynn_username == m.mc_username else m.wynn_username + '|' + m.mc_username}` has been away for {(datetime.now(timezone.utc) - m.last_online).days} days."
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
        shouters = await DiscordAccount.filter(shout_count__gt=0).order_by("-shout_count")

        lines = []
        for shouter in shouters:
            user = await self.bot.fetch_user(int(shouter.disc_uuid))
            lines.append(f"{user.mention}: {shouter.shout_count} shouts")

        if lines:
            embeds = from_lines("Shouterboard", lines, 10, logger)
            await ctx.send(embed=embeds[0], view=Paginator(embeds))
        else:
            await ctx.send("No shouts recorded yet.")

    @commands.hybrid_command(name="last_online")
    async def last_online(self, ctx: commands.Context, username_or_uuid: str):
        try:
            player = await MinecraftAccount.get(Q(uuid=username_or_uuid) | Q(wynn_username=username_or_uuid))
            ts = int(player.last_online.timestamp())
            await ctx.send(f"{player.wynn_username} was last online on <t:{ts}:F>, which was <t:{ts}:R>")
        except Exception as e:
            logger.error(f"[/last_online] {e}")
            await ctx.send("That user probably does not exist, is too new or is not in the guild.")
            raise e


async def setup(bot: Bot):
    await bot.add_cog(Activity(bot))
    logger.info("Activity cog loaded successfully")

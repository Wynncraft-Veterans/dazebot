import asyncio
from datetime import datetime, timedelta, timezone
import logging
import discord
from discord.ext import commands, tasks
from lib import mc
from lib.discord_paginated_embed import Paginator, from_lines
from lib.wynn import check_player_full
from lib.wynn_api.guild import get_guild
from lib.wynn_api.guild_models import BaseMember, Guild
from lib.wynn_api.requestor import Requestor
from orm import DiscordAccount, MinecraftAccount, Shout

logger = logging.getLogger("dazebot.cogs.activity")
from bot import Bot
from tortoise.expressions import Q

# TODO: Put into config rather than define here
ROLES_ALLOWED_TO_SHOUT = [1402295013169172500, 1436108975132119221, 1436109140195020892]


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
        await mc.unload()

    @tasks.loop(minutes=1)
    async def eat_queue(self): ...

    async def _apply_guild(self, guild: Guild, member: BaseMember):
        # Resolve the canonical Minecraft username. Prefer the API's legacyName
        # when it differs from the live username (renamed account), otherwise
        # fall back to the cached Mojang lookup. (instructions1.md §4)
        try:
            mc_username = await mc.get_mc_username(member.uuid)
        except RuntimeError:
            # All upstream Mojang providers down + no cache: fall back to
            # whatever the Wynncraft API reports so we still record the row.
            mc_username = member.username
        # online may be null per Wynncraft privacy opt-out.
        is_online = bool(member.online)
        account, created = await MinecraftAccount.get_or_create(
            uuid=member.uuid,
            defaults={
                "guild": guild.name,
                "wynn_username": member.username,
                "mc_username": mc_username,
                "last_online": datetime.now(timezone.utc)
                if is_online
                else datetime.fromtimestamp(0, tz=timezone.utc),
                "last_manual_check": datetime.fromtimestamp(0, tz=timezone.utc),
            },
        )

        if not created:
            account.guild = guild.name
            account.wynn_username = member.username
            account.mc_username = mc_username
            now = datetime.now(timezone.utc)
            if is_online and account.last_online <= now:
                account.last_online = now
            await account.save()

    async def _check_guild(self, guild_name_full: str) -> Guild:
        guild = await get_guild(guild_name_full)
        members = [*guild.members.all_members()]
        logger.info(f"checking online members {[m.username for m in members if m.online]}")

        tasks = []

        # Detect newly-joined and newly-left members of *Returners* and fire the
        # role-state machine accordingly. (instructions1.md \u00a76)
        if guild_name_full == "Returners":
            api_uuids = {m.uuid for m in members}
            previously_in: list[MinecraftAccount] = await MinecraftAccount.filter(guild=guild_name_full).all()
            previously_in_uuids = {a.uuid for a in previously_in}
            joined_uuids = api_uuids - previously_in_uuids
            await self._fire_role_transitions_for_uuids(joined_uuids, _trigger="joined")

        for member in members:
            tasks.append(self._apply_guild(guild, member))

        accounts = await MinecraftAccount.filter(Q(guild=guild_name_full) & ~Q(uuid__in=[m.uuid for m in members]))
        left_uuids = [a.uuid for a in accounts]
        for account in accounts:
            account.guild = None
            await account.save()

        await asyncio.gather(*tasks)

        if guild_name_full == "Returners" and left_uuids:
            await self._fire_role_transitions_for_uuids(set(left_uuids), _trigger="became_guildless")
        elif guild_name_full != "Returners":
            # When we re-check a non-Returners guild, fire JOINED_OTHER_GUILD
            # for every uuid we already had in our DB. The state machine
            # ignores no-op transitions, so this is safe even if they've been
            # in this guild for a while.
            api_uuids = {m.uuid for m in members}
            known = await MinecraftAccount.filter(uuid__in=list(api_uuids)).values_list("uuid", flat=True)
            await self._fire_role_transitions_for_uuids(set(known), _trigger="joined_other_guild")

        return guild

    async def _fire_role_transitions_for_uuids(self, uuids: set[str], _trigger: str):
        """Resolve each uuid -> linked discord member -> apply the appropriate
        ``Trigger`` from ``lib.role_state``.
        """
        if not uuids:
            return
        from lib.role_state import Trigger, apply_transition  # local import: cog may load before lib

        guild = self.bot.get_guild(self.bot.config.GUILD)
        if guild is None:
            return
        # Gather discord links for these MC accounts.
        discs = await DiscordAccount.filter(
            minecraft_account__uuid__in=list(uuids)
        ).select_related("minecraft_account")
        # And alts.
        from orm import MinecraftAlt

        alt_links = await MinecraftAlt.filter(
            minecraft_account__uuid__in=list(uuids)
        ).select_related("discord_account", "minecraft_account")
        all_disc_uuids: set[str] = {d.disc_uuid for d in discs}
        all_disc_uuids.update(a.discord_account.disc_uuid for a in alt_links)

        trig_map = {
            "joined": Trigger.JOINED_VETS,
            "became_guildless": Trigger.BECAME_GUILDLESS,
            "joined_other_guild": Trigger.JOINED_OTHER_GUILD,
        }
        trig = trig_map[_trigger]

        for disc_uuid in all_disc_uuids:
            member = guild.get_member(int(disc_uuid))
            if member is None:
                continue
            try:
                await apply_transition(member, trig, reason=f"automation:{_trigger}")
            except discord.HTTPException as e:
                logger.warning(f"automation: failed transition {_trigger} for {member}: {e}")

    @tasks.loop(minutes=11)
    async def check_guild(self):
        logger.info("Doing check_guild task")
        guild = await self._check_guild("Returners")
        # online may be None per privacy opt-out; treat unknown as not-online for alerts.
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
            # logger.debug(f"STARTED _check_member_helper {member.mc_username=} {member.uuid=}")
            _, player, _ = await check_player_full(member.uuid)

            # Wynncraft privacy: lastJoin may be None. Treat "None" as "unknown";
            # do NOT trigger inactivity actions for opted-out players.
            if player.server and player.lastJoin is not None and (
                player.lastJoin <= datetime.now(timezone.utc) - timedelta(days=9)
            ):  # TODO configurable
                logger.debug(f"checking {player.server}")
                if member.uuid in await get_server_players(player.server):
                    account = await MinecraftAccount.get(uuid=player.uuid)
                    account.last_online = datetime.now(timezone.utc)
                    await account.save()

            guild_name = player.guild.name if player.guild else None
            guilds_to_check.add(guild_name)
            # logger.debug(f"FINSHED _check_member_helper {member.mc_username=} {member.uuid=}")

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
            player = await MinecraftAccount.get(
                Q(uuid=username_or_uuid) | Q(mc_username=username_or_uuid) | Q(wynn_username=username_or_uuid)
            )
            ts = int(player.last_online.timestamp())
            await ctx.send(
                f"{player.mc_username} was last online on <t:{ts}:F>, which was <t:{ts}:R>. (100% accurate from `TODO configurable`+ days)"
            )
        except Exception as e:
            logger.error(f"[/last_online] {e}")
            await ctx.send("That user probably does not exist, is too new or is not in the guild.")
            raise e


async def setup(bot: Bot):
    await bot.add_cog(Activity(bot))
    logger.info("Activity cog loaded successfully")

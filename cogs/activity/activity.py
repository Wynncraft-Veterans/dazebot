"""Activity cog: periodic Returners guild scan, dead/full guild alerts,
``/purgelist`` for inactive members, and the ``/shout`` group."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import discord
from discord.ext import commands, tasks
from lib.auth import is_shouter, is_staff
from lib.discord_utils.paginated_embed import Paginator, from_lines
from lib.mc import mojang as mc
from lib.mc.wynn import check_player_full
from lib.mc.wynn_api.guild import get_guild
from lib.mc.wynn_api.guild_models import BaseMember, Guild
from lib.mc.wynn_api.player import get_player_stats
from lib.mc.wynn_api.requestor import Requestor
from orm import (
    DeadGuildAlert,
    DiscordAccount,
    GuildCapacityAlert,
    MinecraftAccount,
    Shout,
    UNKNOWN_LAST_ONLINE,
    is_last_online_unknown,
)

logger = logging.getLogger("dazebot.cogs.activity.activity")
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
        # Resolve the canonical Minecraft username. We have two independent
        # sources of truth on every tick: the Wynncraft API (member.username)
        # and our Mojang-cache stack. resolve_canonical_username compares the
        # two and, on disagreement, force-refreshes from Mojang directly to
        # break the tie -- catches the failure mode where ashcon (or any
        # cached value) is serving a stale legacy name.
        try:
            mc_username = await mc.resolve_canonical_username(member.uuid, hint=member.username)
        except RuntimeError:
            # All upstream Mojang providers down + no cache: fall back to
            # whatever the Wynncraft API reports so we still record the row.
            mc_username = member.username
        # online may be null per Wynncraft privacy opt-out.
        is_online = bool(member.online)
        now = datetime.now(timezone.utc)

        # Source of truth for last_online, in priority order:
        #   1. Currently online -> right now.
        #   2. Guild endpoint's per-member ``lastJoin`` (verified to match the
        #      /player endpoint exactly, including for renamed accounts and
        #      privacy-hidden accounts; see commit history / DEVGUIDE).
        #   3. ``UNKNOWN_LAST_ONLINE`` sentinel when the player has hidden
        #      lastJoin via Wynncraft privacy.
        if is_online:
            api_last_online = now
        elif member.lastJoin is not None:
            api_last_online = member.lastJoin
        else:
            api_last_online = UNKNOWN_LAST_ONLINE

        account, created = await MinecraftAccount.get_or_create(
            uuid=member.uuid,
            defaults={
                "guild": guild.name,
                "wynn_username": member.username,
                "mc_username": mc_username,
                "last_online": api_last_online,
                "last_manual_check": UNKNOWN_LAST_ONLINE,
            },
        )

        if not created:
            account.guild = guild.name
            account.wynn_username = member.username
            account.mc_username = mc_username
            # Only advance ``last_online``; never roll it back, and never
            # overwrite a real timestamp with the unknown sentinel.
            if not is_last_online_unknown(api_last_online):
                if is_last_online_unknown(account.last_online) or account.last_online < api_last_online:
                    account.last_online = api_last_online
            await account.save()

    async def _check_guild(self, guild_name_full: str) -> Guild:
        guild = await get_guild(guild_name_full)
        members = [*guild.members.all_members()]
        logger.info(f"checking online members {[m.username for m in members if m.online]}")

        tasks = []

        # API-down write-guard: `get_guild` raises on error envelopes / network
        # errors, but a *successful-but-truncated* member list parses silently.
        # If we trust such a response, every still-present member is wrongly
        # detected as "left" \u2192 guild nulled \u2192 mass MEMBER\u2192HIATUS/REGISTERED
        # downgrades. So suppress *all* membership inference (both join- and
        # leave-detection) when the response is implausibly small vs. what we
        # have stored. The per-member `_apply_guild` "still here / refresh"
        # pass below always runs \u2014 writing fresher data for members the API
        # *did* return is safe; only inference from *absence* is unsafe.
        api_count = len(members)
        db_count = await MinecraftAccount.filter(guild=guild_name_full).count()
        trust = api_count > 0 and (
            db_count == 0
            or api_count
            >= max(
                self.bot.config.GUILD_SCAN_MIN_PLAUSIBLE_ABS,
                int(db_count * self.bot.config.GUILD_SCAN_MIN_PLAUSIBLE_FRACTION),
            )
        )
        if not trust:
            logger.error(
                "check_guild(%s): SUSPICIOUS api_count=%d vs db_count=%d \u2014 skipping "
                "membership inference (join + leave) to avoid mass false transitions",
                guild_name_full, api_count, db_count,
            )

        # Detect newly-joined members of *Returners*. (../.claude/membership_spec.md \u00a76)
        if trust and guild_name_full == "Returners":
            api_uuids = {m.uuid for m in members}
            previously_in: list[MinecraftAccount] = await MinecraftAccount.filter(guild=guild_name_full).all()
            previously_in_uuids = {a.uuid for a in previously_in}
            joined_uuids = api_uuids - previously_in_uuids
            await self._fire_role_transitions_for_uuids(joined_uuids, _trigger="joined")

        for member in members:
            tasks.append(self._apply_guild(guild, member))

        left_uuids: list[str] = []
        if trust:
            accounts = await MinecraftAccount.filter(
                Q(guild=guild_name_full) & ~Q(uuid__in=[m.uuid for m in members])
            )
            left_uuids = [a.uuid for a in accounts]
            for account in accounts:
                account.guild = None
                await account.save()

        await asyncio.gather(*tasks)

        if trust and guild_name_full == "Returners" and left_uuids:
            await self._fire_role_transitions_for_uuids(set(left_uuids), _trigger="became_guildless")
        elif trust and guild_name_full != "Returners":
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

        # The guild endpoint returns a per-member ``lastJoin`` that has been
        # verified to match ``/v3/player/{uuid}.lastJoin`` exactly (including
        # the privacy-opted-out ``null`` cases). ``_apply_guild`` above has
        # already written that into the DB, so we no longer need a per-member
        # /player fanout just to keep inactivity timestamps fresh.
        #
        # We DO still want to periodically pull each member's full stats so
        # ``ProfessionCategories`` (and any moves to a different guild) stay
        # current. Limit that to one weekly pass per account, dispatched
        # through the non-priority Requestor queue so it cannot block
        # interactive lookups.
        members_for_prof_refresh = await MinecraftAccount.filter(
            Q(guild="Returners")
            & Q(last_manual_check__lt=datetime.now(timezone.utc) - timedelta(weeks=1))  # TODO configurable
        )

        guilds_to_check: set[str] = set()
        logger.debug(f"weekly prof refresh: {len(members_for_prof_refresh)} member(s)")

        async def _refresh_prof(member: MinecraftAccount):
            try:
                _, player, _ = await check_player_full(member.uuid)
            except Exception:
                logger.exception(f"prof refresh failed for {member.mc_username}")
                return
            if player.guild and player.guild.name:
                guilds_to_check.add(player.guild.name)

        await asyncio.gather(*(_refresh_prof(m) for m in members_for_prof_refresh))

        guilds_to_check.discard("Returners")
        await asyncio.gather(*(self._check_guild(g) for g in guilds_to_check))

        alerts = []
        now = datetime.now(timezone.utc)

        # Throttle timestamps must persist across bot restarts; otherwise an
        # admin's `/alerts mute` is silently undone by the next process
        # restart (the in-memory ``LAST_*_ALERT`` resets to epoch and the
        # very next check fires regardless of the configured delta).
        last_dead_row = await DeadGuildAlert.all().order_by("-created_at").first()
        last_dead = last_dead_row.created_at if last_dead_row else self.bot.nosql.LAST_DEAD_ALERT
        if (
            len(online) <= self.bot.config.GUILD_DEAD_WHEN
            and now - last_dead >= self.bot.config.GUILD_DEAD_ALERT_DELTA
        ):
            self.bot.nosql.LAST_DEAD_ALERT = now
            await DeadGuildAlert.create()
            alerts.append(self._low_count_alert())

        last_cap_row = await GuildCapacityAlert.all().order_by("-created_at").first()
        last_cap = last_cap_row.created_at if last_cap_row else self.bot.nosql.LAST_CAP_ALERT
        if (
            guild.members.total >= self.bot.config.GUILD_FULL_WHEN
            and now - last_cap >= self.bot.config.GUILD_FULL_ALERT_DELTA
        ):
            self.bot.nosql.LAST_CAP_ALERT = now
            await GuildCapacityAlert.create()
            alerts.append(self._guild_full_alert())

        logger.info(f"{len(online)=}")

        await asyncio.gather(*alerts)

    @check_guild.before_loop
    async def _before_check_guild(self):
        # check_guild bulk-saves every guild member; each save fires the vanity
        # post_save signal -> bot.get_guild(). The guild cache isn't populated
        # until READY, and setup_hook (where this loop is .start()'d) runs
        # entirely before the gateway connects -- so without this wait the
        # first pass logs "Guild not found" once per account. (Whether it
        # actually raced safely depended on cog load order, which the
        # subfolder reorg changed.) Mirrors janitor.py's _before_janitor_loop.
        await self.bot.wait_until_ready()

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

    @commands.hybrid_command(name="purgelist")
    @commands.check_any(is_staff(), is_shouter(ROLES_ALLOWED_TO_SHOUT))
    async def purgelist(
        self,
        ctx: commands.Context,
        days: int = 9,
        refresh: bool = False,
    ):
        """Show Returners members inactive for more than ``days`` days.

        Output is split into two sections:
          - **Inactive**: members whose ``lastJoin`` is confirmed to be older
            than the cutoff. Each candidate is double-checked against the
            ``/v3/player/{uuid}`` endpoint immediately before display so that
            stale DB rows cannot produce false positives (e.g. someone who
            logged on five minutes ago must not appear here).
          - **API-disabled**: members whose ``lastJoin`` is hidden by
            Wynncraft privacy settings. Their activity status cannot be
            determined; they MUST be reviewed manually before any kick.

        ``refresh=True`` forces a full re-poll of the Returners guild before
        sampling, for use when staff want maximal accuracy at the cost of one
        extra GUILD-bucket request.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        await ctx.defer()

        if refresh:
            # Single GUILD-bucket request; ``_apply_guild`` will write the
            # latest ``lastJoin`` for every member back into the DB.
            await self._check_guild("Returners")

        candidates = await MinecraftAccount.filter(guild="Returners")

        api_disabled: list[MinecraftAccount] = []
        likely_inactive: list[MinecraftAccount] = []
        for m in candidates:
            if is_last_online_unknown(m.last_online):
                api_disabled.append(m)
            elif m.last_online < cutoff:
                likely_inactive.append(m)

        # Final-pass freshness verification: re-query /player for each likely
        # candidate. Dispatched through the non-priority Requestor queue so
        # interactive lookups are not blocked. Cost: one PLAYER-bucket request
        # per candidate (typically <20), all 2-min-cached upstream.
        confirmed: list[tuple[MinecraftAccount, datetime]] = []
        false_positives: list[tuple[MinecraftAccount, datetime]] = []

        async def _verify(m: MinecraftAccount):
            try:
                player = await get_player_stats(m.uuid, full=True)
            except Exception:
                logger.exception(f"purgelist verify failed for {m.mc_username}; trusting DB")
                confirmed.append((m, m.last_online))
                return
            if player.lastJoin is None:
                # Player flipped to privacy-hidden between the guild tick
                # and now. Reclassify.
                api_disabled.append(m)
                return
            if player.lastJoin < cutoff:
                confirmed.append((m, player.lastJoin))
            else:
                false_positives.append((m, player.lastJoin))

        await asyncio.gather(*(_verify(m) for m in likely_inactive))

        if false_positives:
            logger.info(
                f"purgelist: dropped {len(false_positives)} false positive(s) "
                f"after live verify: {[m.mc_username for m, _ in false_positives]}"
            )

        confirmed.sort(key=lambda t: t[1])
        api_disabled.sort(key=lambda a: a.mc_username.lower())

        if not confirmed and not api_disabled:
            await ctx.send(f"No members have been away for more than {days} days.")
            return

        now = datetime.now(timezone.utc)

        def _name(a: MinecraftAccount) -> str:
            # Show both the Wynncraft (legacy) name and the canonical Mojang
            # name when they have desynced (renamed account).
            if a.wynn_username and a.mc_username and a.wynn_username != a.mc_username:
                return f"{a.wynn_username}|{a.mc_username}"
            return a.wynn_username or a.mc_username

        lines: list[str] = []
        if confirmed:
            lines.append(f"__**Inactive**__ *(>{days} days)*")
            for a, lj in confirmed:
                lines.append(f"- `{_name(a)}` \u2014 last seen {(now - lj).days} day(s) ago ({lj.date()})")
        if api_disabled:
            if confirmed:
                lines.append("")
            lines.append("__**Unknown**__ *(They have their API disabled)*")
            for a in api_disabled:
                lines.append(f"- `{_name(a)}`")

        embeds = from_lines("Purgelist", lines, 15, logger)
        await ctx.send(embed=embeds[0], view=Paginator(embeds))

    @commands.hybrid_group(name="shout", description="In-game shout actions and stats")
    async def shout_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply("Use subcommands: send, board, last")

    @shout_group.command(name="send", description="Record a shout you just did in-game")
    @is_shouter(ROLES_ALLOWED_TO_SHOUT)
    async def shout_send(self, ctx: commands.Context):
        discord_acc, _ = await DiscordAccount.get_or_create(disc_uuid=str(ctx.author.id))
        await Shout.create(shouter=discord_acc)
        discord_acc.shout_count += 1
        await discord_acc.save(update_fields=["shout_count"])
        await ctx.send(f"Thank you for helping the guild, {ctx.author.mention}!\nYour shout has been recorded.")

    @shout_group.command(name="last", description="Show the most recent shouts")
    @commands.check_any(is_staff(), is_shouter(ROLES_ALLOWED_TO_SHOUT))
    async def shout_last(self, ctx: commands.Context):
        shouts = await Shout.all().order_by("-created_at").limit(3).prefetch_related("shouter")
        lines = []
        for shout in shouts:
            delta = datetime.now(timezone.utc) - shout.created_at
            user = await self.bot.fetch_user(int(shout.shouter.disc_uuid))
            lines.append(
                f"{user.mention} - {delta.seconds // 3600} hours and {(delta.seconds // 60) % 60} minutes ago"
            )
        if lines:
            await ctx.send("\n".join(lines), silent=True)
        else:
            await ctx.send("There have been no recorded shouts.")

    @shout_group.command(name="board", description="Per-shouter shout-count leaderboard")
    @commands.check_any(is_staff(), is_shouter(ROLES_ALLOWED_TO_SHOUT))
    async def shout_board(self, ctx: commands.Context):
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


async def setup(bot: Bot):
    await bot.add_cog(Activity(bot))
    logger.info("Activity cog loaded successfully")

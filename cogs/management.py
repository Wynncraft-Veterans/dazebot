"""Management cog: blocklist, force, register, add, vanity, honour, list,
username, waitlist, config, and first-install commands.

See ``.vscode/instructions1.md`` for the full requirements.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
import re
from typing import Annotated, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from tortoise.expressions import Q

from bot import Bot
from config import CurrConfig
from lib import runtime_config
from lib.auth import is_server_admin, is_staff
from lib.converters import CaseInsensitiveMember
from lib.role_state import (
    State,
    Trigger,
    apply_transition,
    force_to_registered_only,
    state_of,
)
from lib.wynn_api.guild import get_guild
from lib.wynn_api.player import get_player_full_stats
from orm import (
    Blocklist,
    DiscordAccount,
    DMSentLog,
    FirstInstallMonitor,
    LinkRequest,
    MinecraftAccount,
    MinecraftAlt,
    UNKNOWN_LAST_ONLINE,
    UserVanityChoice,
    Waitlist,
)

logger = logging.getLogger("dazebot.cogs.management")

VANITY_DATE_RE = re.compile(r"^(?P<year>\d{4})(?:[-/](?P<month>\d{1,2})(?:[-/](?P<day>\d{1,2}))?)?$")


# ---------- helpers ----------


async def _resolve_target_member(
    ctx: commands.Context, value: str
) -> Optional[discord.Member]:
    """Try to resolve `value` (a ping, id, username, or display name) to a
    Member of the guild context.
    """
    if ctx.guild is None:
        return None
    try:
        return await CaseInsensitiveMember().convert(ctx, value)
    except commands.MemberNotFound:
        return None


async def _resolve_mc_account_loose(value: str) -> Optional[MinecraftAccount]:
    return await MinecraftAccount.filter(
        Q(uuid=value)
        | Q(mc_username__iexact=value)
        | Q(wynn_username__iexact=value)
    ).first()


async def _ensure_mc_account(value: str) -> MinecraftAccount:
    """Find or create a MinecraftAccount for the given username/uuid."""
    mc = await _resolve_mc_account_loose(value)
    if mc is not None:
        return mc
    fs = await get_player_full_stats(value)
    return await MinecraftAccount.create(
        uuid=fs.uuid,
        wynn_username=fs.username,
        mc_username=fs.username,
        # fs.lastJoin / firstJoin can be None per Wynncraft privacy opt-out;
        # use UNKNOWN_LAST_ONLINE as the in-band "unknown" marker.
        last_online=fs.lastJoin or UNKNOWN_LAST_ONLINE,
        last_manual_check=UNKNOWN_LAST_ONLINE,
        first_join=fs.firstJoin,
    )


def _parse_vanity_date(value: str) -> date:
    """Accept '2014', '2014-03', '2014-03-12', '2014/3/12'. Default missing
    parts to Jan 1 / day 1.
    """
    m = VANITY_DATE_RE.match(value.strip())
    if not m:
        raise ValueError(
            "Could not parse date. Use a year (2014), year-month (2014-03), or year-month-day (2014-03-12)."
        )
    year = int(m.group("year"))
    month = int(m.group("month") or 1)
    day = int(m.group("day") or 1)
    return date(year, month, day)


def _vanity_role_for_date(value: date) -> Optional[str]:
    from lib.vanity_roles import get_vanity_role_id

    dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return get_vanity_role_id(dt, CurrConfig)


# ---------- the cog ----------


class Management(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        if (
            getattr(CurrConfig, "INACTIVITY_MEMBER_ENABLED", False)
            or getattr(CurrConfig, "INACTIVITY_WAITLIST_ENABLED", True)
        ):
            self.inactivity_loop.start()
        logger.info("Management cog initialized")

    def cog_unload(self):
        try:
            self.inactivity_loop.cancel()
        except RuntimeError:
            pass

    # =================================================================
    # FIRST INSTALL
    # =================================================================

    @commands.hybrid_command(
        name="first_install",
        description="(Admin) Post the onboarding message with a 'Link Minecraft' button.",
    )
    @is_server_admin()
    @app_commands.describe(
        channel="Channel to post the onboarding message in (defaults to here).",
        quote_message_id="Optional: an existing staff-authored message whose text to quote in the embed.",
    )
    async def first_install(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        quote_message_id: Optional[str] = None,
    ):
        """One-shot install command. See instructions1.md \u00a71."""
        logger.info(f"first_install: invoked by {ctx.author} (id={ctx.author.id}) in guild={ctx.guild}")
        from lib.first_install_view import FirstInstallView, build_welcome_embed

        if ctx.guild is None:
            await ctx.reply("This command must be used in a guild.")
            return

        # Slash commands must ack within 3s; the role-wipe loop below can take
        # much longer. defer() is a no-op for prefix invocations.
        await ctx.defer()
        logger.info("first_install: deferred, beginning channel resolution")
        target_channel = channel or (
            ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
        )
        if target_channel is None:
            await ctx.reply("Please specify a text channel.")
            return

        # Optionally quote a staff-authored message.
        quoted: discord.Message | None = None
        if quote_message_id:
            try:
                qmid = int(quote_message_id)
            except ValueError:
                await ctx.reply("`quote_message_id` must be a numeric Discord message id.")
                return
            for ch in ctx.guild.text_channels:
                try:
                    quoted = await ch.fetch_message(qmid)
                    break
                except (discord.NotFound, discord.Forbidden):
                    continue
                except discord.HTTPException as e:
                    logger.warning(f"first-install fetch failed in {ch}: {e}")
            if quoted is None:
                await ctx.reply("Couldn't find that quote message in any channel I can read.")
                return

        # Wipe old roles + vanity roles, replace any existing monitor.
        await FirstInstallMonitor.all().delete()

        wiped_legacy = 0
        wiped_vanity = 0
        vanity_role_ids = set(int(r) for _, r in CurrConfig.VanityRolesConfig.CUTOFFS)
        legacy_role_ids = set(int(r) for r in CurrConfig.ROLES_FIRST_INSTALL_WIPE)

        # NOTE: do NOT call ctx.guild.chunk() here. With members/presence
        # intents on, the cache is populated from gateway events at connect
        # time; an explicit chunk request can hang indefinitely on some
        # gateway sessions (observed: deferred command never returns). The
        # iteration below uses the cached `ctx.guild.members` directly,
        # which is sufficient for our purposes.
        async with ctx.typing():
            logger.info(f"first-install: scanning {len(ctx.guild.members)} members for legacy/vanity roles")
            for member in ctx.guild.members:
                to_remove = [
                    r for r in member.roles
                    if r.id in legacy_role_ids or r.id in vanity_role_ids
                ]
                if to_remove:
                    try:
                        await member.remove_roles(*to_remove, reason="first-install wipe")
                        for r in to_remove:
                            if r.id in legacy_role_ids:
                                wiped_legacy += 1
                            if r.id in vanity_role_ids:
                                wiped_vanity += 1
                    except discord.HTTPException as e:
                        logger.warning(f"first-install: failed to strip roles from {member}: {e}")

            # Clear stored vanity choices since vanity roles were wiped.
            await UserVanityChoice.all().delete()

            # Post the bot's onboarding message with the persistent button.
            embed = build_welcome_embed(quoted)
            try:
                posted = await target_channel.send(embed=embed, view=FirstInstallView())
            except discord.HTTPException as e:
                await ctx.reply(f"\u274c Failed to post onboarding message: {e}")
                return

        await FirstInstallMonitor.create(
            guild_id=str(ctx.guild.id),
            channel_id=str(posted.channel.id),
            message_id=str(posted.id),
        )
        logger.info(f"first-install: posted onboarding view at {posted.jump_url}")

        await ctx.reply(
            f"\u2705 First install complete.\n"
            f"\u2022 Onboarding message: {posted.jump_url}\n"
            f"\u2022 Stripped {wiped_legacy} legacy role assignment(s)\n"
            f"\u2022 Stripped {wiped_vanity} vanity role assignment(s)\n"
            f"\u2022 Cleared stored vanity choices."
        )

    # =================================================================
    # SCRIPT (one-off admin maintenance scripts)
    # =================================================================

    @commands.hybrid_group(
        name="script",
        description="(Admin) One-off maintenance scripts.",
    )
    @is_server_admin()
    async def script_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply(
                "Available scripts: `/script edit_welcome <channel> <message_id>`."
            )

    @script_group.command(
        name="edit_welcome",
        description="(Admin) Rewrite an onboarding message to the simplified copy, keeping the link button.",
    )
    @is_server_admin()
    @app_commands.describe(
        channel="Channel containing the onboarding message.",
        message_id="ID of the message to edit (must have been posted by this bot).",
    )
    async def script_edit_welcome(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        message_id: str,
    ):
        from lib.first_install_view import FirstInstallView

        await ctx.defer(ephemeral=True)
        try:
            mid = int(message_id)
        except ValueError:
            await ctx.reply("`message_id` must be numeric.", ephemeral=True)
            return

        try:
            msg = await channel.fetch_message(mid)
        except (discord.NotFound, discord.Forbidden) as e:
            await ctx.reply(f"\u274c Couldn't fetch that message: {e}", ephemeral=True)
            return

        if msg.author.id != self.bot.user.id:
            await ctx.reply(
                "\u274c That message wasn't posted by me, so I can't edit it.",
                ephemeral=True,
            )
            return

        new_content = (
            "# IN-GAME VETS GUILD MEMBERS!\n"
            "## Use this to unlock guild-specific channels!"
        )
        try:
            await msg.edit(content=new_content, embed=None, view=FirstInstallView())
        except discord.HTTPException as e:
            await ctx.reply(f"\u274c Edit failed: {e}", ephemeral=True)
            return

        await ctx.reply(f"\u2705 Edited {msg.jump_url}.", ephemeral=True)

    # =================================================================
    # BLOCK / UNBLOCK
    # =================================================================

    @commands.hybrid_group(name="block", description="(Staff) Manage the VETS blocklist.")
    @is_staff()
    async def block_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply("Use `/block add <user>` or `/block remove <user>`.")

    @block_group.command(name="add", description="Add a user to the blocklist.")
    async def block_add(self, ctx: commands.Context, target: str, *, reason: Optional[str] = None):
        member, mc = await self._resolve_target(ctx, target)
        if mc is None:
            await ctx.reply(f"No Minecraft account found for `{target}`. /register them first if needed.")
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
                    f"\u26a0\ufe0f Blocked user `{mc.mc_username}` (`{mc.uuid}`) is currently in the in-game "
                    f"guild Returners. They should be kicked.\n_(blocked by {ctx.author.mention})_",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                in_game_alert_sent = True

        msg = f"\U0001f6ab Added `{mc.mc_username}` to the blocklist."
        if in_game_alert_sent:
            msg += " (Posted alert to in-game-guild channel.)"
        await ctx.reply(msg)

    @block_group.command(name="remove", description="Remove a user from the blocklist.")
    async def block_remove(self, ctx: commands.Context, target: str):
        _, mc = await self._resolve_target(ctx, target)
        if mc is None:
            await ctx.reply(f"No Minecraft account found for `{target}`.")
            return
        deleted = await Blocklist.filter(minecraft_account=mc).delete()
        if not deleted:
            await ctx.reply(f"`{mc.mc_username}` was not on the blocklist.")
            return
        await ctx.reply(f"\u2705 Removed `{mc.mc_username}` from the blocklist.")

    # Convenience top-level alias for /unblock
    @commands.hybrid_command(name="unblock", description="(Staff) Remove a user from the blocklist.")
    @is_staff()
    async def unblock(self, ctx: commands.Context, target: str):
        await self.block_remove.callback(self, ctx, target)

    # =================================================================
    # FORCE
    # =================================================================

    @commands.hybrid_command(
        name="force", description="(Staff) Force a state transition that the automation wouldn't normally apply."
    )
    @is_staff()
    @app_commands.choices(
        transition=[
            app_commands.Choice(name="registered \u2192 hiatus", value="registered_to_hiatus"),
        ]
    )
    async def force(self, ctx: commands.Context, target: str, transition: str):
        member, mc = await self._resolve_target(ctx, target)
        if member is None:
            await ctx.reply(f"`{target}` is not in the discord server.")
            return

        if transition == "registered_to_hiatus":
            state = state_of(member)
            if State.REGISTERED not in state:
                await ctx.reply(
                    f"{member.mention} is not currently Registered (state: {state}). "
                    "Refusing to apply registered\u2192hiatus.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            reg = ctx.guild.get_role(CurrConfig.ROLE_REGISTERED) if ctx.guild else None
            hia = ctx.guild.get_role(CurrConfig.ROLE_HIATUS) if ctx.guild else None
            if reg is None or hia is None:
                await ctx.reply("Configured Registered/Hiatus role not found in this guild.")
                return
            await member.remove_roles(reg, reason="staff /force registered\u2192hiatus")
            await member.add_roles(hia, reason="staff /force registered\u2192hiatus")
            await ctx.reply(
                f"\u2705 Forced {member.mention} \u2192 Hiatus.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await ctx.reply(f"Unknown transition: `{transition}`.")

    # =================================================================
    # REGISTER (force-couple) + ADD (alt)
    # =================================================================

    @commands.hybrid_command(
        name="register",
        description="(Staff) Force-link a Minecraft username to a Discord user.",
    )
    @is_staff()
    async def register(
        self,
        ctx: commands.Context,
        username: str,
        user: Annotated[discord.Member, CaseInsensitiveMember],
    ):
        mc = await _ensure_mc_account(username)
        # If MC is claimed by a different discord, refuse.
        existing = await DiscordAccount.filter(minecraft_account_id=mc.id).first()
        if existing is not None and existing.disc_uuid != str(user.id):
            other = await self.bot.fetch_user(int(existing.disc_uuid))
            await ctx.reply(
                f"`{mc.mc_username}` is already linked to {other.mention}. Unlink them first.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(user.id))
        if disc.minecraft_account_id == mc.id:
            await ctx.reply(
                f"{user.mention} is already linked to `{mc.mc_username}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        disc.minecraft_account = mc
        await disc.save(update_fields=["minecraft_account_id"])
        await ctx.reply(
            f"\u2705 Force-linked {user.mention} \u2194 `{mc.mc_username}` (`{mc.uuid}`).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(
        name="add",
        description="(Staff) Register an alt Minecraft account for a Discord user.",
    )
    @is_staff()
    async def add_alt(
        self,
        ctx: commands.Context,
        username: str,
        user: Annotated[discord.Member, CaseInsensitiveMember],
    ):
        mc = await _ensure_mc_account(username)
        disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(user.id))

        # If they don't have a primary linked yet, treat /add as /register for the primary.
        if disc.minecraft_account_id is None:
            disc.minecraft_account = mc
            await disc.save(update_fields=["minecraft_account_id"])
            await ctx.reply(
                f"\u2139\ufe0f {user.mention} had no primary account; set `{mc.mc_username}` as primary instead.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if disc.minecraft_account_id == mc.id:
            await ctx.reply(
                f"`{mc.mc_username}` is already the primary account for {user.mention}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        existing_alt = await MinecraftAlt.filter(discord_account=disc, minecraft_account=mc).first()
        if existing_alt is not None:
            await ctx.reply(f"`{mc.mc_username}` is already an alt of {user.mention}.")
            return

        # Refuse if the alt is someone else's primary.
        other_primary = await DiscordAccount.filter(minecraft_account_id=mc.id).exclude(id=disc.id).first()
        if other_primary is not None:
            other = await self.bot.fetch_user(int(other_primary.disc_uuid))
            await ctx.reply(
                f"`{mc.mc_username}` is already the primary for {other.mention}. Cannot also be {user.mention}'s alt.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await MinecraftAlt.create(discord_account=disc, minecraft_account=mc)
        await ctx.reply(
            f"\u2705 Added `{mc.mc_username}` as an alt of {user.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # =================================================================
    # VANITY (self + staff)
    # =================================================================

    @commands.hybrid_group(name="vanity", description="Self-assign a vanity year/date role.")
    async def vanity(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply(
                "Use `/vanity set <year-or-date>` to self-assign, or "
                "`/vanity force <user> <year-or-date>` (staff only)."
            )

    @vanity.command(name="set", description="Self-assign your vanity role for a given year/date.")
    async def vanity_set(self, ctx: commands.Context, year_or_date: str):
        try:
            d = _parse_vanity_date(year_or_date)
        except ValueError as e:
            await ctx.reply(str(e))
            return
        role_id_str = _vanity_role_for_date(d)
        if role_id_str is None:
            await ctx.reply("No vanity role exists for that date (it's later than the most recent cutoff).")
            return
        role_id = int(role_id_str)
        if role_id == CurrConfig.ROLE_VANITY_ORIGINAL:
            await ctx.reply(
                "Self-assigning the original-tier (<1.0/2013) role isn't allowed. "
                "If you genuinely qualify, ask a staff member to /vanity force you."
            )
            return

        # Persist the choice so it overrides firstJoin-based automation.
        await UserVanityChoice.update_or_create(
            disc_uuid=str(ctx.author.id),
            defaults={"role_id": str(role_id), "chosen_by_staff": False},
        )

        if isinstance(ctx.author, discord.Member):
            from cogs.vanity_roles import _cog_instance as vr_cog

            if vr_cog is not None:
                await vr_cog._set_role_exclusive(ctx.author, role_id)
        await ctx.reply(f"\u2705 Vanity role updated to <@&{role_id}>.", allowed_mentions=discord.AllowedMentions.none())

    @vanity.command(name="force", description="(Staff) Force-assign a vanity role to another user.")
    @is_staff()
    async def vanity_force(
        self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember], year_or_date: str
    ):
        try:
            d = _parse_vanity_date(year_or_date)
        except ValueError as e:
            await ctx.reply(str(e))
            return
        role_id_str = _vanity_role_for_date(d)
        if role_id_str is None:
            await ctx.reply("No vanity role for that date.")
            return
        role_id = int(role_id_str)
        await UserVanityChoice.update_or_create(
            disc_uuid=str(user.id),
            defaults={"role_id": str(role_id), "chosen_by_staff": True},
        )
        from cogs.vanity_roles import _cog_instance as vr_cog

        if vr_cog is not None:
            await vr_cog._set_role_exclusive(user, role_id)
        await ctx.reply(
            f"\u2705 Forced vanity role <@&{role_id}> on {user.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # =================================================================
    # HONOUR / UNHONOUR (admin)
    # =================================================================

    @commands.hybrid_command(name="honour", description="(Admin) Mark a user as Honourary.")
    @is_server_admin()
    async def honour(self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember]):
        if ctx.guild is None:
            return
        # Refuse if blocked.
        disc = await DiscordAccount.filter(disc_uuid=str(user.id)).select_related("minecraft_account").first()
        if disc and disc.minecraft_account:
            block = await Blocklist.filter(minecraft_account=disc.minecraft_account).first()
            if block:
                await ctx.reply("Cannot honour a blocklisted user.")
                return
        hon = ctx.guild.get_role(CurrConfig.ROLE_HONOURARY)
        if hon is None:
            await ctx.reply("Configured Honourary role missing from guild.")
            return
        to_remove = []
        for rid in (CurrConfig.ROLE_REGISTERED, CurrConfig.ROLE_HIATUS, CurrConfig.ROLE_MEMBER):
            r = ctx.guild.get_role(rid)
            if r and r in user.roles:
                to_remove.append(r)
        if to_remove:
            await user.remove_roles(*to_remove, reason="/honour")
        await user.add_roles(hon, reason="/honour")
        await ctx.reply(
            f"\u2728 {user.mention} is now Honourary.", allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.hybrid_command(name="unhonour", description="(Admin) Revoke Honourary status.")
    @is_server_admin()
    async def unhonour(self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember]):
        if ctx.guild is None:
            return
        hon = ctx.guild.get_role(CurrConfig.ROLE_HONOURARY)
        reg = ctx.guild.get_role(CurrConfig.ROLE_REGISTERED)
        if hon is None or reg is None:
            await ctx.reply("Configured Honourary/Registered role missing.")
            return
        if hon in user.roles:
            await user.remove_roles(hon, reason="/unhonour")
        if reg not in user.roles:
            await user.add_roles(reg, reason="/unhonour")
        await ctx.reply(
            f"\u2705 {user.mention} is no longer Honourary.", allowed_mentions=discord.AllowedMentions.none()
        )

    # =================================================================
    # LIST UNLINKED + USERNAME LOOKUP
    # =================================================================

    @commands.hybrid_group(name="list", description="(Staff) Listings.")
    @is_staff()
    async def list_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply("Use `/list unlinked` or `/list linked`.")

    @list_group.command(name="unlinked", description="List in-game VETS members not linked to a Discord account.")
    async def list_unlinked(self, ctx: commands.Context):
        from lib.discord_paginated_embed import Paginator, from_lines

        guild_data = await get_guild("Returners")
        api_members = list(guild_data.members.all_members())
        api_uuids = {m.uuid for m in api_members}
        # Gather linked uuids from our DB.
        linked = await MinecraftAccount.filter(
            uuid__in=list(api_uuids), discord_account__id__not_isnull=True
        ).values_list("uuid", flat=True)
        linked_set = set(linked)
        # Also account for alts.
        alt_linked = await MinecraftAlt.filter(
            minecraft_account__uuid__in=list(api_uuids)
        ).values_list("minecraft_account__uuid", flat=True)
        linked_set.update(alt_linked)

        unlinked = [m for m in api_members if m.uuid not in linked_set]
        if not unlinked:
            await ctx.reply("\u2705 Every in-game member is linked to a Discord account.")
            return

        lines = [
            f"\u2022 `{m.username}`"
            + (f"  _(legacyName: `{m.legacyName}`)_" if m.legacyName and m.legacyName != m.username else "")
            for m in unlinked
        ]
        embeds = from_lines(
            title=f"Unlinked in-game members ({len(unlinked)})",
            lines=lines,
            lines_per_page=15,
            logger=logger,
        )
        await ctx.send(embed=embeds[0], view=Paginator(embeds))

    @list_group.command(
        name="linked",
        description="List every Discord<->Minecraft link the bot knows about.",
    )
    async def list_linked(self, ctx: commands.Context):
        from lib.discord_paginated_embed import Paginator, from_lines

        await ctx.defer()

        # Primary links: every DiscordAccount with a non-null minecraft_account.
        all_discs = await DiscordAccount.all().select_related("minecraft_account")
        discs = [d for d in all_discs if d.minecraft_account is not None]
        logger.info(
            f"/list linked: scanned {len(all_discs)} DiscordAccount rows, "
            f"{len(discs)} have a primary minecraft_account"
        )

        # Alts: collect per discord_account so we can show them grouped.
        alts = await MinecraftAlt.all().prefetch_related("discord_account", "minecraft_account")
        alts_by_disc: dict[str, list[MinecraftAlt]] = {}
        for a in alts:
            alts_by_disc.setdefault(a.discord_account.disc_uuid, []).append(a)

        if not discs and not alts_by_disc:
            await ctx.reply("_(no linked accounts)_")
            return

        total_alts = len(alts)

        # Sort primaries by mc_username for stable output.
        discs.sort(key=lambda d: (d.minecraft_account.mc_username or "").lower())

        lines: list[str] = []
        for d in discs:
            mc = d.minecraft_account
            assert mc is not None
            mention = f"<@{d.disc_uuid}>"
            guild_tag = f" _[{mc.guild}]_" if mc.guild else " _[guildless]_"
            lines.append(f"\u2022 {mention} \u2014 `{mc.mc_username}`{guild_tag}")
            for a in alts_by_disc.pop(d.disc_uuid, []):
                amc = a.minecraft_account
                lines.append(f"  \u21b3 alt `{amc.mc_username}`")

        # Any leftover alts whose discord_account has no primary link.
        for disc_uuid, alt_list in alts_by_disc.items():
            mention = f"<@{disc_uuid}>"
            lines.append(f"\u2022 {mention} \u2014 _(no primary, alts only)_")
            for a in alt_list:
                amc = a.minecraft_account
                lines.append(f"  \u21b3 alt `{amc.mc_username}`")

        embeds = from_lines(
            title=f"Linked accounts ({len(discs)} primary, {total_alts} alts)",
            lines=lines,
            lines_per_page=15,
            logger=logger,
        )
        await ctx.send(
            embed=embeds[0],
            view=Paginator(embeds),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(
        name="username",
        description="(Staff) Show all Minecraft accounts linked to a Discord user.",
    )
    @is_staff()
    async def username(self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember]):
        disc = await DiscordAccount.filter(disc_uuid=str(user.id)).select_related("minecraft_account").first()
        if disc is None:
            await ctx.reply(f"{user.mention} has no Discord account record.", allowed_mentions=discord.AllowedMentions.none())
            return
        primary = disc.minecraft_account
        alts = await MinecraftAlt.filter(discord_account=disc).prefetch_related("minecraft_account")
        embed = discord.Embed(title=f"Linked accounts for {user.display_name}", color=discord.Color.blue())
        if primary:
            embed.add_field(
                name="Primary",
                value=f"`{primary.mc_username}` (`{primary.uuid}`)"
                + (f"  alias: `{primary.wynn_username}`" if primary.wynn_username != primary.mc_username else ""),
                inline=False,
            )
        else:
            embed.add_field(name="Primary", value="_(none)_", inline=False)
        if alts:
            embed.add_field(
                name=f"Alts ({len(alts)})",
                value="\n".join(
                    f"\u2022 `{a.minecraft_account.mc_username}` (`{a.minecraft_account.uuid}`)" for a in alts
                ),
                inline=False,
            )
        await ctx.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # =================================================================
    # WAITLIST (override existing simple stub with the spec'd one)
    # =================================================================

    @commands.hybrid_group(name="waitlist", description="(Staff) Manage the VETS waitlist.")
    @is_staff()
    async def waitlist_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply("Use `/waitlist add <user> [username]`, `/waitlist view`, or `/waitlist remove <user>`.")

    @waitlist_group.command(
        name="add",
        description="Add a user to the waitlist; `username` is required if they aren't linked yet.",
    )
    async def waitlist_group_add(
        self,
        ctx: commands.Context,
        user: Annotated[discord.Member, CaseInsensitiveMember],
        username: Optional[str] = None,
    ):
        if ctx.guild is None:
            return
        disc = await DiscordAccount.filter(disc_uuid=str(user.id)).select_related("minecraft_account").first()
        mc: Optional[MinecraftAccount] = disc.minecraft_account if disc else None

        if mc is None:
            if not username:
                await ctx.reply(
                    f"{user.mention} is not linked yet \u2014 supply their Minecraft username so I can /register them.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            # Functional /register
            mc = await _ensure_mc_account(username)
            existing = await DiscordAccount.filter(minecraft_account_id=mc.id).first()
            if existing is not None and existing.disc_uuid != str(user.id):
                other = await self.bot.fetch_user(int(existing.disc_uuid))
                await ctx.reply(
                    f"`{mc.mc_username}` is already linked to {other.mention}.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(user.id))
            disc.minecraft_account = mc
            await disc.save(update_fields=["minecraft_account_id"])

        # Check blocklist.
        block = await Blocklist.filter(minecraft_account=mc).first()
        if block is not None:
            await ctx.reply(f"`{mc.mc_username}` is on the blocklist; cannot waitlist.")
            return

        # Add to waitlist (DB).
        existing_wl = await Waitlist.filter(minecraft_account=mc).first()
        if existing_wl is None:
            await Waitlist.create(minecraft_account=mc)

        # Apply role transition.
        result = await apply_transition(user, Trigger.ADDED_TO_WAITLIST, reason="/waitlist add")
        if result.is_error:
            await ctx.reply(
                f"\u26a0\ufe0f Added to waitlist DB but role change failed: {result.error}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.reply(
            f"\u2705 {user.mention} (`{mc.mc_username}`) added to waitlist.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @waitlist_group.command(
        name="view",
        description="View the current waitlist.",
    )
    async def waitlist_group_view(self, ctx: commands.Context):
        from lib.discord_paginated_embed import Paginator, from_lines

        entries = await Waitlist.all().prefetch_related("minecraft_account").order_by("created_at")
        lines = [
            f"{i + 1}. <t:{int(entry.created_at.timestamp())}:R> - "
            f"`{entry.minecraft_account.mc_username}` (`{entry.minecraft_account.uuid}`)"
            for i, entry in enumerate(entries)
        ]
        if not lines:
            await ctx.reply("The waitlist is empty.")
            return

        embeds = from_lines(
            title="Current waitlist",
            lines=lines,
            lines_per_page=10,
            logger=logger,
        )
        await ctx.send(
            embed=embeds[0],
            view=Paginator(embeds),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @waitlist_group.command(
        name="remove",
        description="Remove a player from the waitlist by Minecraft username or UUID.",
    )
    async def waitlist_group_remove(self, ctx: commands.Context, username_or_uuid: str):
        deleted = await Waitlist.filter(
            Q(minecraft_account__uuid=username_or_uuid)
            | Q(minecraft_account__mc_username__iexact=username_or_uuid)
        ).delete()
        if not deleted:
            await ctx.reply(f"`{username_or_uuid}` is not on the waitlist.")
            return
        await ctx.reply(f"`{username_or_uuid}` has been removed from the waitlist.")

    # =================================================================
    # /config
    # =================================================================

    @commands.hybrid_group(name="config", description="(Admin) View/modify runtime config.")
    @is_server_admin()
    async def config_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply("Use `/config list`, `/config get <key>`, `/config set <key> <value>`, `/config reset <key>`.")

    @config_group.command(name="list")
    async def config_list(self, ctx: commands.Context):
        keys = runtime_config.list_keys()
        chunks: list[str] = []
        cur = ""
        for k in keys:
            try:
                v = runtime_config.get_value(k)
            except Exception:
                continue
            line = f"`{k}` = `{v}`\n"
            if len(cur) + len(line) > 1800:
                chunks.append(cur)
                cur = ""
            cur += line
        if cur:
            chunks.append(cur)
        if not chunks:
            await ctx.reply("(no overridable keys)")
            return
        for c in chunks:
            await ctx.send(c)

    @config_group.command(name="get")
    async def config_get(self, ctx: commands.Context, key: str):
        if not hasattr(CurrConfig, key):
            await ctx.reply(f"Unknown key: `{key}`.")
            return
        try:
            v = runtime_config.get_value(key)
        except Exception as e:
            await ctx.reply(f"Error: {e}")
            return
        await ctx.reply(f"`{key}` = `{v}`")

    @config_group.command(name="set")
    async def config_set(self, ctx: commands.Context, key: str, value: str):
        try:
            new = await runtime_config.set_override(key, value)
        except (KeyError, PermissionError, ValueError) as e:
            await ctx.reply(f"\u274c {e}")
            return
        await ctx.reply(f"\u2705 `{key}` = `{new}` (persisted)")

    @config_group.command(name="reset")
    async def config_reset(self, ctx: commands.Context, key: str):
        await runtime_config.clear_override(key)
        await ctx.reply(f"\u2705 Cleared override for `{key}`. Restart bot for compiled default to take effect.")

    # =================================================================
    # /alerts  (shortcuts on top of /config for the guild dead/full alerts)
    # =================================================================

    @commands.hybrid_group(name="alerts", description="(Admin) Mute / unmute / tune guild dead+full alerts.")
    @is_server_admin()
    async def alerts_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply(
                "Use `/alerts status`, `/alerts mute [duration_hours]`, `/alerts unmute`, "
                "`/alerts thresholds <dead_when> <full_when>`."
            )

    # A duration big enough that the throttle effectively silences the alert,
    # but still finite/reset-able. ~10 years.
    _ALERTS_MUTE_SECONDS = 10 * 365 * 24 * 3600

    @alerts_group.command(
        name="status",
        description="Show current dead/full alert thresholds and throttle deltas.",
    )
    async def alerts_status(self, ctx: commands.Context):
        dead_when = runtime_config.get_value("GUILD_DEAD_WHEN")
        full_when = runtime_config.get_value("GUILD_FULL_WHEN")
        dead_delta = runtime_config.get_value("GUILD_DEAD_ALERT_DELTA")
        full_delta = runtime_config.get_value("GUILD_FULL_ALERT_DELTA")

        def _muted(delta: timedelta) -> str:
            return " \u26d4 muted" if delta.total_seconds() >= 365 * 24 * 3600 else ""

        await ctx.reply(
            "**Guild alert config**\n"
            f"\u2022 Dead alert: fires when online \u2264 `{dead_when}`, "
            f"throttled `{dead_delta}`{_muted(dead_delta)}\n"
            f"\u2022 Full alert: fires when members \u2265 `{full_when}`, "
            f"throttled `{full_delta}`{_muted(full_delta)}"
        )

    @alerts_group.command(
        name="mute",
        description="Silence dead+full guild alerts (default: ~forever).",
    )
    @app_commands.describe(duration_hours="How long to mute, in hours. Omit for ~forever.")
    async def alerts_mute(self, ctx: commands.Context, duration_hours: Optional[float] = None):
        seconds = int(duration_hours * 3600) if duration_hours else self._ALERTS_MUTE_SECONDS
        try:
            await runtime_config.set_override("GUILD_DEAD_ALERT_DELTA", str(seconds))
            await runtime_config.set_override("GUILD_FULL_ALERT_DELTA", str(seconds))
        except (KeyError, PermissionError, ValueError) as e:
            await ctx.reply(f"\u274c {e}")
            return
        td = timedelta(seconds=seconds)
        await ctx.reply(f"\ud83d\udd07 Muted dead+full alerts for `{td}`. Use `/alerts unmute` to restore.")

    @alerts_group.command(
        name="unmute",
        description="Restore the compiled default throttle for dead+full alerts.",
    )
    async def alerts_unmute(self, ctx: commands.Context):
        await runtime_config.clear_override("GUILD_DEAD_ALERT_DELTA")
        await runtime_config.clear_override("GUILD_FULL_ALERT_DELTA")
        await ctx.reply(
            "\ud83d\udd0a Cleared dead+full alert throttle overrides. "
            "Compiled defaults take effect after the next bot restart; "
            "the running process keeps the muted values until then."
        )

    @alerts_group.command(
        name="thresholds",
        description="Set dead-when (online <=) and full-when (members >=) thresholds.",
    )
    @app_commands.describe(
        dead_when="Online-player count at or below which the dead alert fires.",
        full_when="Member count at or above which the full alert fires.",
    )
    async def alerts_thresholds(
        self,
        ctx: commands.Context,
        dead_when: Optional[int] = None,
        full_when: Optional[int] = None,
    ):
        if dead_when is None and full_when is None:
            await ctx.reply("Provide at least one of `dead_when` or `full_when`.")
            return
        changes: list[str] = []
        try:
            if dead_when is not None:
                v = await runtime_config.set_override("GUILD_DEAD_WHEN", str(dead_when))
                changes.append(f"`GUILD_DEAD_WHEN` = `{v}`")
            if full_when is not None:
                v = await runtime_config.set_override("GUILD_FULL_WHEN", str(full_when))
                changes.append(f"`GUILD_FULL_WHEN` = `{v}`")
        except (KeyError, PermissionError, ValueError) as e:
            await ctx.reply(f"\u274c {e}")
            return
        await ctx.reply("\u2705 " + "; ".join(changes))

    # =================================================================
    # INACTIVITY LOOP
    # =================================================================

    @tasks.loop(hours=6)
    async def inactivity_loop(self):
        # Re-read config each tick (admin may have toggled via /config).
        guild = self.bot.get_guild(CurrConfig.GUILD)
        if guild is None:
            return

        if getattr(CurrConfig, "INACTIVITY_WAITLIST_ENABLED", True):
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(CurrConfig.INACTIVITY_WAITLIST_DAYS))
            stale = await Waitlist.filter(created_at__lt=cutoff).prefetch_related("minecraft_account__discord_account")
            for entry in stale:
                disc_list = await entry.minecraft_account.discord_account.all()
                for d in disc_list:
                    member = guild.get_member(int(d.disc_uuid))
                    if member is None:
                        continue
                    try:
                        await apply_transition(member, Trigger.INACTIVE_WAITLIST, reason="inactive waitlist")
                    except Exception as e:
                        logger.warning(f"inactivity (waitlist) failed for {member}: {e}")
                await entry.delete()

        if getattr(CurrConfig, "INACTIVITY_MEMBER_ENABLED", False):
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(CurrConfig.INACTIVITY_MEMBER_DAYS))
            members = await MinecraftAccount.filter(
                guild="Returners", last_online__lt=cutoff
            ).prefetch_related("discord_account")
            for mc in members:
                disc_list = await mc.discord_account.all()
                for d in disc_list:
                    member = guild.get_member(int(d.disc_uuid))
                    if member is None:
                        continue
                    # DM warning, no role change.
                    existing = await DMSentLog.filter(
                        disc_uuid=str(member.id), kind="member_inactivity"
                    ).first()
                    if existing is not None:
                        # Already warned in the last cycle window; refresh after 30 days.
                        if (datetime.now(timezone.utc) - existing.sent_at) < timedelta(days=30):
                            continue
                        await existing.delete()
                    try:
                        dm = await member.create_dm()
                        await dm.send(
                            f"Hi {member.display_name}! You've been inactive in VETS for "
                            f"{int(CurrConfig.INACTIVITY_MEMBER_DAYS)}+ days.\n\n"
                            "Inactive slots are a strain on the guild \u2014 if you don't plan on returning soon, "
                            "consider stepping down. You can always rejoin at any time when you get back!"
                        )
                        await DMSentLog.create(disc_uuid=str(member.id), kind="member_inactivity")
                    except discord.Forbidden:
                        logger.info(f"inactivity DM blocked for {member}")

    # =================================================================
    # internal target resolver
    # =================================================================

    async def _resolve_target(
        self, ctx: commands.Context, target: str
    ) -> tuple[Optional[discord.Member], Optional[MinecraftAccount]]:
        """Resolve a target string that may be a discord ping/id/username OR a
        Minecraft username/uuid. Returns (member_or_none, mc_account_or_none).
        """
        member: Optional[discord.Member] = None
        # Try discord first
        try:
            member = await CaseInsensitiveMember().convert(ctx, target)
        except commands.MemberNotFound:
            pass
        # Try MC account
        mc: Optional[MinecraftAccount] = None
        if member is not None:
            disc = await DiscordAccount.filter(disc_uuid=str(member.id)).select_related("minecraft_account").first()
            if disc and disc.minecraft_account:
                mc = disc.minecraft_account
        if mc is None:
            mc = await _resolve_mc_account_loose(target)
            if mc is not None and member is None:
                # Find a discord member linked to this MC.
                disc = await DiscordAccount.filter(minecraft_account_id=mc.id).first()
                if disc is not None and ctx.guild is not None:
                    member = ctx.guild.get_member(int(disc.disc_uuid))
        return member, mc


async def setup(bot: Bot):
    await bot.add_cog(Management(bot))
    logger.info("Management cog loaded successfully")

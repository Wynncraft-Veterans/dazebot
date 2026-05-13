"""Membership-state cog: role-state transitions and surrounding commands.

Owns the ``/first_install``, ``/script``, ``/force``, ``/vanity``,
``/honour``/``/unhonour``, ``/list``, and ``/info`` slash surfaces, plus
the periodic ``inactivity_loop`` that decays stale waitlist entries and
DM-warns inactive members.

The full requirements live in ``../.claude/membership_spec.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Annotated, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot import Bot
from config import CurrConfig
from lib.auth import is_admin, is_operator, is_registered, is_staff
from lib.converters import CaseInsensitiveMember
from lib.resolve import parse_vanity_date, resolve_target, vanity_role_for_date
from lib.role_state import State, Trigger, apply_transition, state_of
from lib.wynn_api.guild import get_guild
from orm import (
    Blocklist,
    DiscordAccount,
    DMSentLog,
    FirstInstallMonitor,
    MinecraftAccount,
    MinecraftAlt,
    UserVanityChoice,
    Waitlist,
)

logger = logging.getLogger("dazebot.cogs.membership_state")


class MembershipState(commands.Cog):
    """Role-state machine, vanity, honour, and listings."""

    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        if (
            getattr(CurrConfig, "INACTIVITY_MEMBER_ENABLED", False)
            or getattr(CurrConfig, "INACTIVITY_WAITLIST_ENABLED", True)
        ):
            self.inactivity_loop.start()
        logger.info("MembershipState cog initialized")

    def cog_unload(self):
        try:
            self.inactivity_loop.cancel()
        except RuntimeError:
            pass

    # ---------- /first_install ----------

    @commands.hybrid_command(
        name="first_install",
        description="(Operator) Post the onboarding message with a 'Link Minecraft' button.",
    )
    @is_operator()
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
        """One-shot install command. See ../.claude/membership_spec.md §1."""
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
                await ctx.reply(f"❌ Failed to post onboarding message: {e}")
                return

        await FirstInstallMonitor.create(
            guild_id=str(ctx.guild.id),
            channel_id=str(posted.channel.id),
            message_id=str(posted.id),
        )
        logger.info(f"first-install: posted onboarding view at {posted.jump_url}")

        await ctx.reply(
            f"✅ First install complete.\n"
            f"• Onboarding message: {posted.jump_url}\n"
            f"• Stripped {wiped_legacy} legacy role assignment(s)\n"
            f"• Stripped {wiped_vanity} vanity role assignment(s)\n"
            f"• Cleared stored vanity choices."
        )

    # ---------- /script (one-off admin maintenance scripts) ----------

    @commands.hybrid_group(
        name="script",
        description="(Operator) One-off maintenance scripts.",
    )
    @is_operator()
    async def script_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply(
                "Available scripts: `/script edit_welcome <channel> <message_id>`, "
                "`/script rename_cult`."
            )

    @script_group.command(
        name="edit_welcome",
        description="(Operator) Rewrite an onboarding message to the simplified copy, keeping the link button.",
    )
    @is_operator()
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
            await ctx.reply(f"❌ Couldn't fetch that message: {e}", ephemeral=True)
            return

        if msg.author.id != self.bot.user.id:
            await ctx.reply(
                "❌ That message wasn't posted by me, so I can't edit it.",
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
            await ctx.reply(f"❌ Edit failed: {e}", ephemeral=True)
            return

        await ctx.reply(f"✅ Edited {msg.jump_url}.", ephemeral=True)

    @script_group.command(
        name="rename_cult",
        description="(Operator) One-off: rename the `dazecult` row to `deercult`.",
    )
    @is_operator()
    async def script_rename_cult(self, ctx: commands.Context):
        from orm import Cult

        await ctx.defer(ephemeral=True)
        old, new = "dazecult", "deercult"
        if await Cult.filter(name=new).exists():
            await ctx.reply(f"❌ `{new}` already exists; aborting.", ephemeral=True)
            return
        cult = await Cult.filter(name=old).first()
        if cult is None:
            await ctx.reply(f"❌ No cult named `{old}` to rename.", ephemeral=True)
            return
        cult.name = new
        await cult.save(update_fields=["name"])
        await ctx.reply(
            f"✅ Renamed `{old}` → `{new}`. "
            "Remember to update `CULT_THREADS` in `cogs/returns/week_0.py` to match.",
            ephemeral=True,
        )

    # ---------- /force ----------

    @commands.hybrid_group(name="force", description="(Admin) Force-apply automation actions.")
    @is_admin()
    async def force_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply("Use `/force change <target> <transition>` or `/force check`.")

    @force_group.command(
        name="change",
        description="Force a state transition that the automation wouldn't normally apply.",
    )
    @app_commands.choices(
        transition=[
            app_commands.Choice(name="registered → hiatus", value="registered_to_hiatus"),
        ]
    )
    async def force_change(self, ctx: commands.Context, target: str, transition: str):
        await ctx.defer()
        member, _mc = await resolve_target(ctx, target)
        if member is None:
            await ctx.reply(f"`{target}` is not in the discord server.")
            return

        if transition == "registered_to_hiatus":
            state = state_of(member)
            if State.REGISTERED not in state:
                await ctx.reply(
                    f"{member.mention} is not currently Registered (state: {state}). "
                    "Refusing to apply registered→hiatus.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            reg = ctx.guild.get_role(CurrConfig.ROLE_REGISTERED) if ctx.guild else None
            hia = ctx.guild.get_role(CurrConfig.ROLE_HIATUS) if ctx.guild else None
            if reg is None or hia is None:
                await ctx.reply("Configured Registered/Hiatus role not found in this guild.")
                return
            await member.remove_roles(reg, reason="staff /force change registered→hiatus")
            await member.add_roles(hia, reason="staff /force change registered→hiatus")
            await ctx.reply(
                f"✅ Forced {member.mention} → Hiatus.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await ctx.reply(f"Unknown transition: `{transition}`.")

    @force_group.command(
        name="check",
        description="Run the periodic guild check immediately.",
    )
    async def force_check(self, ctx: commands.Context):
        act = self.bot.get_cog("Activity")
        if act is None:
            await ctx.reply("Activity cog is not loaded.")
            return
        await ctx.defer()
        # ``check_guild`` is a tasks.loop wrapping a coroutine; calling the
        # Loop instance invokes the underlying coroutine once.
        await act.check_guild()
        await ctx.reply("✅ Guild check completed.")

    # ---------- /vanity ----------

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
            d = parse_vanity_date(year_or_date)
        except ValueError as e:
            await ctx.reply(str(e))
            return
        role_id_str = vanity_role_for_date(d)
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
            from cogs.membership.vanity_roles import _cog_instance as vr_cog

            if vr_cog is not None:
                await vr_cog._set_role_exclusive(ctx.author, role_id)
        await ctx.reply(
            f"✅ Vanity role updated to <@&{role_id}>.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @vanity.command(name="force", description="(Staff) Force-assign a vanity role to another user.")
    @is_staff()
    async def vanity_force(
        self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember], year_or_date: str
    ):
        try:
            d = parse_vanity_date(year_or_date)
        except ValueError as e:
            await ctx.reply(str(e))
            return
        role_id_str = vanity_role_for_date(d)
        if role_id_str is None:
            await ctx.reply("No vanity role for that date.")
            return
        role_id = int(role_id_str)
        await UserVanityChoice.update_or_create(
            disc_uuid=str(user.id),
            defaults={"role_id": str(role_id), "chosen_by_staff": True},
        )
        from cogs.membership.vanity_roles import _cog_instance as vr_cog

        if vr_cog is not None:
            await vr_cog._set_role_exclusive(user, role_id)
        await ctx.reply(
            f"✅ Forced vanity role <@&{role_id}> on {user.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- /honour /unhonour ----------

    @commands.hybrid_command(name="honour", description="(Admin) Mark a user as Honourary.")
    @is_admin()
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
        # Also flip the bridge-access flag on their primary linked MC account.
        if disc and disc.minecraft_account and not disc.minecraft_account.is_honourary:
            disc.minecraft_account.is_honourary = True
            await disc.minecraft_account.save(update_fields=["is_honourary"])
        await ctx.reply(
            f"✨ {user.mention} is now Honourary.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="unhonour", description="(Admin) Revoke Honourary status.")
    @is_admin()
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
        # Clear bridge-access flag + token on their primary linked MC account.
        disc = await DiscordAccount.filter(disc_uuid=str(user.id)).select_related("minecraft_account").first()
        if disc and disc.minecraft_account and disc.minecraft_account.is_honourary:
            disc.minecraft_account.is_honourary = False
            disc.minecraft_account.token = None
            await disc.minecraft_account.save(update_fields=["is_honourary", "token"])
        await ctx.reply(
            f"✅ {user.mention} is no longer Honourary.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- /list ----------

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
            await ctx.reply("✅ Every in-game member is linked to a Discord account.")
            return

        lines = [
            f"• `{m.username}`"
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
            lines.append(f"• {mention} — `{mc.mc_username}`{guild_tag}")
            for a in alts_by_disc.pop(d.disc_uuid, []):
                amc = a.minecraft_account
                lines.append(f"  ↳ alt `{amc.mc_username}`")

        # Any leftover alts whose discord_account has no primary link.
        for disc_uuid, alt_list in alts_by_disc.items():
            mention = f"<@{disc_uuid}>"
            lines.append(f"• {mention} — _(no primary, alts only)_")
            for a in alt_list:
                amc = a.minecraft_account
                lines.append(f"  ↳ alt `{amc.mc_username}`")

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

    # ---------- /info ----------

    @commands.hybrid_command(
        name="info",
        description="(Registered) Show linked Minecraft accounts, Wynncraft join date, and last-online for a user.",
    )
    @is_registered()
    async def info(self, ctx: commands.Context, target: str):
        """Unified user/account lookup. ``target`` may be a Discord ping/id/
        username OR a Minecraft username/UUID. Replaces the older
        ``/username``, ``/last_online`` and ``/joindate`` commands.
        """
        member, mc = await resolve_target(ctx, target)
        if member is None and mc is None:
            await ctx.reply(f"Couldn't resolve `{target}` to a Discord member or Minecraft account.")
            return

        embed = discord.Embed(color=discord.Color.blue())

        # Discord side
        if member is not None:
            embed.title = f"Linked accounts for {member.display_name}"
            disc = await DiscordAccount.filter(disc_uuid=str(member.id)).select_related("minecraft_account").first()
            primary = disc.minecraft_account if disc else None
            alts = (
                await MinecraftAlt.filter(discord_account=disc).prefetch_related("minecraft_account")
                if disc is not None
                else []
            )
            embed.add_field(name="Discord", value=member.mention, inline=False)
            if primary:
                mc = mc or primary  # prefer the primary for last-online/joindate display below
                embed.add_field(
                    name="Primary",
                    value=f"`{primary.mc_username}` (`{primary.uuid}`)"
                    + (
                        f"  alias: `{primary.wynn_username}`"
                        if primary.wynn_username and primary.wynn_username != primary.mc_username
                        else ""
                    ),
                    inline=False,
                )
            else:
                embed.add_field(name="Primary", value="_(none)_", inline=False)
            if alts:
                embed.add_field(
                    name=f"Alts ({len(alts)})",
                    value="\n".join(
                        f"• `{a.minecraft_account.mc_username}` (`{a.minecraft_account.uuid}`)" for a in alts
                    ),
                    inline=False,
                )
        else:
            assert mc is not None
            embed.title = f"Minecraft account `{mc.mc_username}`"
            embed.add_field(
                name="Minecraft",
                value=f"`{mc.mc_username}` (`{mc.uuid}`)"
                + (
                    f"  alias: `{mc.wynn_username}`"
                    if mc.wynn_username and mc.wynn_username != mc.mc_username
                    else ""
                ),
                inline=False,
            )

        # Joindate / last-online from the resolved MC account.
        if mc is not None:
            if mc.first_join is not None:
                ts = int(mc.first_join.timestamp())
                embed.add_field(name="First join", value=f"<t:{ts}:F> (<t:{ts}:R>)", inline=False)
            from orm import is_last_online_unknown

            if not is_last_online_unknown(mc.last_online):
                ts = int(mc.last_online.timestamp())
                embed.add_field(name="Last online", value=f"<t:{ts}:F> (<t:{ts}:R>)", inline=False)
            else:
                embed.add_field(name="Last online", value="_(hidden by Wynncraft privacy)_", inline=False)
            if mc.guild:
                embed.add_field(name="Guild", value=mc.guild, inline=True)

        await ctx.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ---------- inactivity loop ----------

    @tasks.loop(hours=6)
    async def inactivity_loop(self):
        """Both waitlist-decay and member-inactivity DM warnings live here.

        Single loop rather than two so config changes (e.g. flipping
        INACTIVITY_MEMBER_ENABLED via /config) re-evaluate atomically each
        tick. Splitting into two loops would risk drift between cadences.
        """
        # Re-read config each tick (admin may have toggled via /config).
        guild = self.bot.get_guild(CurrConfig.GUILD)
        if guild is None:
            return

        if getattr(CurrConfig, "INACTIVITY_WAITLIST_ENABLED", True):
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(CurrConfig.INACTIVITY_WAITLIST_DAYS))
            stale = await Waitlist.filter(created_at__lt=cutoff).prefetch_related(
                "minecraft_account__discord_account"
            )
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
                            "Inactive slots are a strain on the guild — if you don't plan on returning soon, "
                            "consider stepping down. You can always rejoin at any time when you get back!"
                        )
                        await DMSentLog.create(disc_uuid=str(member.id), kind="member_inactivity")
                    except discord.Forbidden:
                        logger.info(f"inactivity DM blocked for {member}")


async def setup(bot: Bot):
    await bot.add_cog(MembershipState(bot))
    logger.info("MembershipState cog loaded successfully")

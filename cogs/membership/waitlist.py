"""Waitlist cog: the ``/waitlist`` group (add / view / remove / self / leave)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated, Optional

import discord
from discord.ext import commands
from tortoise.expressions import Q

from bot import Bot
from lib.auth import is_guild, is_registered, is_staff
from lib.discord_utils.converters import CaseInsensitiveMember
from lib.mc.resolve import ensure_mc_account
from lib.mc.wynn_api.errors import WynnApiError
from lib.role_state import (
    RoleState,
    Trigger,
    apply_transition,
    ensure_linked_baseline,
    resolve_guild_member,
    state_of,
)
from orm import Blocklist, DiscordAccount, MinecraftAccount, Waitlist, is_last_online_unknown

logger = logging.getLogger("dazebot.cogs.membership.waitlist")


class WaitlistCog(commands.Cog):
    """Discord surface for the VETS in-game guild waitlist."""

    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Waitlist cog initialized")

    @commands.hybrid_group(name="waitlist", description="Manage the VETS waitlist.")
    async def waitlist_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply(
                "Use `/waitlist add <user> [username]` (staff), `/waitlist view` (registered), "
                "`/waitlist remove <user>` (staff), `/waitlist force <user> <position>` (staff), "
                "`/waitlist self` (guild member self-add), "
                "or `/waitlist leave` (anyone, self-remove)."
            )

    @waitlist_group.command(
        name="add",
        description="(Staff) Add a user to the waitlist; `username` required if they aren't linked.",
    )
    @is_staff()
    async def waitlist_add(
        self,
        ctx: commands.Context,
        user: Annotated[discord.Member, CaseInsensitiveMember],
        username: Optional[str] = None,
    ):
        if ctx.guild is None:
            return
        disc = (
            await DiscordAccount.filter(disc_uuid=str(user.id))
            .select_related("minecraft_account")
            .first()
        )
        mc: Optional[MinecraftAccount] = disc.minecraft_account if disc else None

        if mc is None:
            if not username:
                await ctx.reply(
                    f"{user.mention} is not linked yet — supply their Minecraft username so I can /register them.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            # Functional /register
            try:
                mc = await ensure_mc_account(username)
            except WynnApiError as e:
                await ctx.reply(f"Could not find `{username}` on Wynncraft: {e.message}")
                return
            except Exception as e:  # noqa: BLE001 — third-party API
                logger.exception("/waitlist add: failed to fetch player stats")
                await ctx.reply(f"Failed to fetch Wynncraft stats for `{username}`: {e}")
                return
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

        # Heads-up for staff: an API-disabled player has no readable
        # lastJoin, so the waitlist inactivity rule can't measure them.
        # server_watcher tracks them via server-change observation instead
        # (so the entry persists rather than being purged) — surface that so
        # staff aren't surprised it won't auto-expire on inactivity.
        api_note = (
            f"\n⚠️ `{mc.mc_username}` has their Wynncraft API disabled — "
            "activity is tracked via server-change observation, so this entry "
            "won't auto-drop on the inactivity rule."
            if is_last_online_unknown(mc.last_online)
            else ""
        )

        # Add to waitlist (DB).
        existing_wl = await Waitlist.filter(minecraft_account=mc).first()
        if existing_wl is None:
            await Waitlist.create(minecraft_account=mc)

        # Enforce the linked-account baseline before the waitlist transition.
        # Users linked via this command's own auto-/register path above (or
        # via an older code path that skipped enforcement) have no state-role,
        # and ADDED_TO_WAITLIST would otherwise fail with "User has no
        # membership state". This is idempotent and also self-heals stale
        # already-linked users. blocked=False: on-blocklist users returned above.
        try:
            await ensure_linked_baseline(
                user,
                in_returners=(mc.guild == "Returners"),
                in_other_guild=(mc.guild is not None and mc.guild != "Returners"),
                blocked=False,
                reason="/waitlist add baseline",
            )
        except discord.HTTPException as e:
            logger.warning(f"/waitlist add: ensure_linked_baseline failed for {user}: {e}")
            await ctx.reply(
                f"⚠️ Added to waitlist DB but could not set baseline roles: {e}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # Apply role transition.
        result = await apply_transition(user, Trigger.ADDED_TO_WAITLIST, reason="/waitlist add")
        if result.is_error:
            # The DB row is ensured above. If the member already holds the
            # WAITLISTED role, the desired end state (DB row + role) is
            # satisfied — the "already waitlisted" error is just a stale-role /
            # missing-DB-row divergence we've now reconciled. Report success.
            if RoleState.WAITLISTED in state_of(user):
                await ctx.reply(
                    f"✅ {user.mention} (`{mc.mc_username}`) is on the waitlist "
                    f"(re-synced DB row + role).{api_note}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await ctx.reply(
                f"⚠️ Added to waitlist DB but role change failed: {result.error}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.reply(
            f"✅ {user.mention} (`{mc.mc_username}`) added to waitlist.{api_note}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @waitlist_group.command(
        name="view",
        description="(Registered) View the current waitlist.",
    )
    @is_registered()
    async def waitlist_view(self, ctx: commands.Context):
        from lib.discord_utils.paginated_embed import Paginator, from_lines

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
        description="(Staff) Remove a player from the waitlist by Minecraft username or UUID.",
    )
    @is_staff()
    async def waitlist_remove(self, ctx: commands.Context, username_or_uuid: str):
        # Fetch the matching rows first (SELECT may JOIN freely; SQLite can't
        # DELETE across a JOIN) so we can both delete by PK *and* strip the
        # dangling WAITLISTED role — keeping the DB and role state in sync.
        rows = await Waitlist.filter(
            Q(minecraft_account__uuid=username_or_uuid)
            | Q(minecraft_account__mc_username__iexact=username_or_uuid)
        ).select_related("minecraft_account")
        if not rows:
            await ctx.reply(f"`{username_or_uuid}` is not on the waitlist.")
            return
        await Waitlist.filter(id__in=[w.id for w in rows]).delete()
        for w in rows:
            disc = await DiscordAccount.filter(minecraft_account_id=w.minecraft_account.id).first()
            if disc is None:
                continue
            member = await resolve_guild_member(self.bot, disc.disc_uuid)
            if member is None:
                continue
            try:
                await apply_transition(member, Trigger.INACTIVE_WAITLIST, reason="/waitlist remove")
            except discord.HTTPException as e:
                logger.warning("waitlist remove: failed to strip WAITLISTED for %s: %s", member, e)
        await ctx.reply(f"`{username_or_uuid}` has been removed from the waitlist.")

    @waitlist_group.command(
        name="force",
        description="(Staff) Move a user to a specific waitlist position (1 = top).",
    )
    @is_staff()
    async def waitlist_force(
        self,
        ctx: commands.Context,
        user: Annotated[discord.Member, CaseInsensitiveMember],
        position: int,
    ):
        # Position is *derived* from `created_at` ascending ordering (see
        # waitlist_view); there is no position column. Forcing a user to slot N
        # therefore means rewriting only that user's `created_at` to fall just
        # before the entry currently at N. Every other row keeps its timestamp,
        # so the old #N, #N+1, ... each shift down by one automatically — the
        # legacy `/force waitlist` bump-down behaviour. This intentionally does
        # not touch roles or row existence, so the WAITLISTED state and the
        # janitor reconciler are unaffected; only the moved user's displayed
        # "added X ago" changes, which is inherent to how position is stored.
        disc = (
            await DiscordAccount.filter(disc_uuid=str(user.id))
            .select_related("minecraft_account")
            .first()
        )
        mc: Optional[MinecraftAccount] = disc.minecraft_account if disc else None
        if mc is None:
            await ctx.reply(
                f"{user.mention} is not on the waitlist. Use `/waitlist add` first.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        entries = await Waitlist.all().prefetch_related("minecraft_account").order_by("created_at")
        target_idx = next(
            (i for i, e in enumerate(entries) if e.minecraft_account_id == mc.id), None
        )
        if target_idx is None:
            await ctx.reply(
                f"`{mc.mc_username}` is not on the waitlist. Use `/waitlist add` first.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        n = len(entries)
        pos = max(1, min(position, n))  # clamp to [1, n]; 1-indexed like /waitlist view
        dst_idx = pos - 1
        if dst_idx == target_idx:
            await ctx.reply(
                f"`{mc.mc_username}` is already at waitlist position **{pos}** (of {n}).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # Others keep their relative order; inserting the target at index
        # `dst_idx` among them yields the final ordering.
        others = [e for i, e in enumerate(entries) if i != target_idx]
        prev_entry = others[dst_idx - 1] if dst_idx > 0 else None
        next_entry = others[dst_idx] if dst_idx < len(others) else None

        target_entry = entries[target_idx]
        if prev_entry is None:  # moving to the top
            new_ts = next_entry.created_at - timedelta(seconds=1)
        elif next_entry is None:  # moving to the bottom
            new_ts = prev_entry.created_at + timedelta(seconds=1)
        else:
            span = next_entry.created_at - prev_entry.created_at
            new_ts = prev_entry.created_at + span / 2
            # Defensive only: distinct rows sharing a microsecond is effectively
            # impossible, but never emit a timestamp outside (prev, next).
            if not (prev_entry.created_at < new_ts < next_entry.created_at):
                new_ts = prev_entry.created_at + timedelta(microseconds=1)
        target_entry.created_at = new_ts
        await target_entry.save(update_fields=["created_at"])

        clamp_note = (
            f" (requested {position}; clamped to valid range 1–{n})"
            if pos != position
            else ""
        )
        await ctx.reply(
            f"✅ Moved `{mc.mc_username}` to waitlist position **{pos}** (of {n}){clamp_note}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @waitlist_group.command(
        name="self",
        description="(Guild) Add yourself to the in-game guild waitlist.",
    )
    @is_guild()
    async def waitlist_self(self, ctx: commands.Context):
        disc_uuid = str(ctx.author.id)
        mc = await MinecraftAccount.filter(discord_account__disc_uuid=disc_uuid).first()
        if mc is None:
            await ctx.reply(
                "You haven't linked a Minecraft account yet. Use `/link code <username>` to link one."
            )
            return
        if mc.guild == "Returners":
            await ctx.reply(f"You (`{mc.mc_username}`) are already in the guild.")
            return
        if mc.guild is not None:
            await ctx.reply(f"You (`{mc.mc_username}`) are in a different guild called `{mc.guild}`.")
            return
        existing = await Waitlist.filter(minecraft_account__discord_account__disc_uuid=disc_uuid).first()
        if existing:
            await ctx.reply(f"You (`{mc.mc_username}`) are already on the waitlist.")
            return
        block = await Blocklist.filter(minecraft_account=mc).first()
        if block is not None:
            await ctx.reply(f"You (`{mc.mc_username}`) are on the blocklist; cannot waitlist.")
            return
        await Waitlist.create(minecraft_account=mc)
        # Apply the WAITLISTED role so the DB row and role state stay in sync
        # (mc.guild is None here — guaranteed by the checks above).
        if isinstance(ctx.author, discord.Member):
            try:
                await ensure_linked_baseline(
                    ctx.author,
                    in_returners=False,
                    in_other_guild=False,
                    blocked=False,
                    reason="/waitlist self baseline",
                )
            except discord.HTTPException as e:
                logger.warning("waitlist self: ensure_linked_baseline failed for %s: %s", ctx.author, e)
            result = await apply_transition(
                ctx.author, Trigger.ADDED_TO_WAITLIST, reason="/waitlist self"
            )
            if result.is_error and RoleState.WAITLISTED not in state_of(ctx.author):
                await ctx.reply(
                    f"⚠️ Added you (`{mc.mc_username}`) to the waitlist DB but the "
                    f"role couldn't be set: {result.error}"
                )
                return
        await ctx.reply(f"You (`{mc.mc_username}`) are now on the waitlist!")

    @waitlist_group.command(
        name="leave",
        description="Remove yourself from the in-game guild waitlist.",
    )
    async def waitlist_leave(self, ctx: commands.Context):
        # SQLite can't DELETE across a JOIN (here two relations deep); resolve
        # ids via the SELECT side first, then delete by PK.
        ids = await Waitlist.filter(
            minecraft_account__discord_account__disc_uuid=str(ctx.author.id)
        ).values_list("id", flat=True)
        deleted = await Waitlist.filter(id__in=ids).delete() if ids else 0
        if not deleted:
            await ctx.reply("You are not on the waitlist.")
            return
        # Strip the WAITLISTED role so DB and role state stay in sync.
        if isinstance(ctx.author, discord.Member):
            try:
                await apply_transition(ctx.author, Trigger.INACTIVE_WAITLIST, reason="/waitlist leave")
            except discord.HTTPException as e:
                logger.warning("waitlist leave: failed to strip WAITLISTED for %s: %s", ctx.author, e)
        await ctx.reply("You have been removed from the waitlist.")


async def setup(bot: Bot):
    await bot.add_cog(WaitlistCog(bot))
    logger.info("Waitlist cog loaded successfully")

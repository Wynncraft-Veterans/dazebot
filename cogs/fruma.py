import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from discord.ext import commands, tasks
from tortoise.expressions import F

from bot import Bot
from orm import WaitlistEntry

logger = logging.getLogger('discord.cogs.fruma')

PLAYERDB_URL = "https://playerdb.co/api/player/minecraft/{}"
WYNNCRAFT_PLAYER_URL = "https://api.wynncraft.com/v3/player/{}"
STALE_DAYS = 9


async def resolve_uuid(username: str) -> tuple[str, str] | None:
    """Resolve a Minecraft username to (uuid, canonical_username) via PlayerDB.
    Returns None on failure."""
    async with aiohttp.ClientSession() as session:
        async with session.get(PLAYERDB_URL.format(username)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            try:
                player = data["data"]["player"]
                return player["id"], player["username"]
            except (KeyError, TypeError):
                return None


async def check_wynncraft_player(uuid: str) -> dict | None:
    """Fetch a player from the Wynncraft v3 API. Returns the JSON dict or None."""
    async with aiohttp.ClientSession() as session:
        async with session.get(WYNNCRAFT_PLAYER_URL.format(uuid)) as resp:
            if resp.status != 200:
                return None
            return await resp.json()


class Fruma(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        self.check_waitlist.start()
        logger.info("Fruma cog initialized")

    def cog_unload(self):
        self.check_waitlist.cancel()

    # ── periodic check ────────────────────────────────────────────────

    @tasks.loop(minutes=15)
    async def check_waitlist(self):
        logger.info("Running waitlist check")
        entries = await WaitlistEntry.all()
        now = datetime.now(timezone.utc)

        removed = False
        for entry in entries:
            try:
                player = await check_wynncraft_player(entry.uuid)
                if player is None:
                    continue

                # Remove if they joined a guild
                if player.get("guild") is not None:
                    logger.info(f"Removing {entry.username} from waitlist — joined guild")
                    await entry.delete()
                    removed = True
                    continue

                # Remove if not seen in STALE_DAYS
                last_join_str = player.get("lastJoin")
                if last_join_str:
                    # Player API returns ISO timestamps with a trailing 'Z' (UTC).
                    # Python's datetime.fromisoformat doesn't accept 'Z', so convert
                    # it to an explicit offset before parsing.
                    last_join = datetime.fromisoformat(last_join_str.replace("Z", "+00:00"))
                    if last_join.tzinfo is None:
                        last_join = last_join.replace(tzinfo=timezone.utc)
                    if now - last_join > timedelta(days=STALE_DAYS):
                        logger.info(f"Removing {entry.username} from waitlist — stale ({last_join})")
                        await entry.delete()
                        removed = True
                        continue
            except Exception:
                logger.exception(f"Error checking waitlist entry {entry.username}")

        # Re-normalize positions to close any gaps
        if removed:
            remaining = await WaitlistEntry.all().order_by("position")
            for i, entry in enumerate(remaining, 1):
                if entry.position != i:
                    entry.position = i
                    await entry.save()

    @check_waitlist.before_loop
    async def before_check_waitlist(self):
        await self.bot.wait_until_ready()

    # ── commands ──────────────────────────────────────────────────────

    @commands.hybrid_group(name="waitlist", description="Manage the guild waitlist", invoke_without_command=True)
    async def waitlist_group(self, ctx: commands.Context):
        """Show waitlist help when invoked without a subcommand."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @waitlist_group.command(name="add", description="Add a Minecraft player to the guild waitlist")
    async def waitlist_add(self, ctx: commands.Context, username: str):
        """Add a player to the waitlist by their Minecraft username."""
        result = await resolve_uuid(username)
        if result is None:
            await ctx.send(f"❌ Could not find Minecraft player **{username}**.")
            return

        uuid, canonical_name = result

        existing = await WaitlistEntry.filter(uuid=uuid).first()
        if existing is not None:
            await ctx.send(f"ℹ️ **{canonical_name}** is already on the waitlist.")
            return

        last = await WaitlistEntry.all().order_by("-position").first()
        next_pos = (last.position + 1) if last else 1
        await WaitlistEntry.create(uuid=uuid, username=canonical_name, position=next_pos)
        await ctx.send(f"✅ **{canonical_name}** has been added to the waitlist.")

    @waitlist_group.command(name="remove", description="Remove a player from the waitlist (staff)")
    @commands.has_permissions(manage_guild=True)
    async def waitlist_remove(self, ctx: commands.Context, username: str):
        """Remove a player from the waitlist by username (staff only)."""
        entry = await WaitlistEntry.filter(username__iexact=username).first()
        if entry is None:
            await ctx.send(f"❌ **{username}** was not found on the waitlist.")
            return
        removed_pos = entry.position
        await entry.delete()
        # Shift entries that were behind the removed one
        await WaitlistEntry.filter(position__gt=removed_pos).all().update(position=F("position") - 1)
        await ctx.send(f"✅ Removed **{username}** from the waitlist.")

    @waitlist_group.command(name="view", description="Show players currently on the waitlist")
    async def waitlist_view(self, ctx: commands.Context):
        """Display all players currently on the waitlist."""
        entries = await WaitlistEntry.all().order_by("position")
        if not entries:
            await ctx.send("The waitlist is empty.")
            return

        lines = [f"**{e.position}.** {e.username}" for e in entries]
        await ctx.send("📋 **Waitlist:**\n" + "\n".join(lines))

    @waitlist_group.command(name="force", description="Force a player to a specific spot in the queue (staff)")
    @commands.has_permissions(manage_guild=True)
    async def waitlist_force(self, ctx: commands.Context, username: str, spot: int):
        """Move an existing waitlist entry to a specific position (staff only)."""
        entry = await WaitlistEntry.filter(username__iexact=username).first()
        if entry is None:
            await ctx.send(f"❌ **{username}** is not on the waitlist.")
            return

        total = await WaitlistEntry.all().count()
        spot = max(1, min(spot, total))
        old_pos = entry.position

        if old_pos == spot:
            await ctx.send(f"ℹ️ **{entry.username}** is already at position **{spot}**.")
            return

        if old_pos < spot:
            # Moving down: shift entries in (old, new] up by 1
            await WaitlistEntry.filter(
                position__gt=old_pos, position__lte=spot
            ).update(position=F("position") - 1)
        else:
            # Moving up: shift entries in [new, old) down by 1
            await WaitlistEntry.filter(
                position__gte=spot, position__lt=old_pos
            ).update(position=F("position") + 1)

        entry.position = spot
        await entry.save()
        await ctx.send(f"✅ Moved **{entry.username}** to position **{spot}**.")

    @waitlist_group.command(name="clear", description="Delete the entire waitlist (admin)")
    @commands.has_permissions(administrator=True)
    async def waitlist_clear(self, ctx: commands.Context):
        """Permanently delete all waitlist entries (admin only)."""
        deleted = await WaitlistEntry.all().delete()
        await ctx.send(f"🗑️ Cleared the waitlist ({deleted} entries removed).")


async def setup(bot: Bot):
    await bot.add_cog(Fruma(bot))
    logger.info("Loaded Fruma Cog")

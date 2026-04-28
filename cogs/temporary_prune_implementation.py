import asyncio
import discord
import aiohttp
from datetime import datetime, timezone, timedelta
from discord.ext import commands
from bot import Bot

# TODO: This is disgusting, and is a temporary workaround for api issues.
# The cached system will need to replace this once we fix the endpoints.

INACTIVE_DAYS = 7


class TemporaryPruneCog(commands.Cog):
    # Ratelimit the shit out of this
    MAX_USES = 2
    WINDOW_SECONDS = 3600

    def __init__(self, bot: Bot):
        self.bot = bot
        self._purge_list_usage: dict[int, list[float]] = {}
        self._purge_list_lock = asyncio.Lock()

    @commands.hybrid_command(name='temp-purge-list')
    @commands.has_permissions(administrator=True)
    async def temp_purge_list(self, ctx: commands.Context) -> None:
        """Temporary implementation of the purge list."""
        server_key = ctx.guild.id if ctx.guild else ctx.author.id
        now = asyncio.get_event_loop().time()

        async with self._purge_list_lock:
            usages = self._purge_list_usage.get(server_key, [])
            window_start = now - self.WINDOW_SECONDS
            usages = [t for t in usages if t > window_start]

            if len(usages) >= self.MAX_USES:
                retry_after = int(usages[0] + self.WINDOW_SECONDS - now)
                minutes = max(1, (retry_after + 59) // 60)
                embed = discord.Embed(
                    title="Temporary Purge List — Rate limited",
                    description=(
                        "Using a terrible temporary implementation of this system.\n"
                        "This will probably be very slow.\n\n"
                        f"⚠️ This command has been used {len(usages)} times in the last hour. "
                        f"Please try again in about {minutes} minute(s)."
                    ),
                    color=discord.Color.red(),
                )
                await ctx.send(embed=embed)
                return

            usages.append(now)
            self._purge_list_usage[server_key] = usages

        warning_embed = discord.Embed(
            title="Temporary Purge List — Warning",
            description=(
                "Using a terrible temporary implementation of this system.\n"
                "This will probably be very slow."
            ),
            color=discord.Color.red(),
        )
        message = await ctx.send(embed=warning_embed)

        async def updateStatus(text: str) -> None:
            e = discord.Embed(
                title="Temporary Purge List — Warning",
                description=(
                    "Using a terrible temporary implementation of this system.\n"
                    "This will probably be very slow.\n\n"
                    + text
                ),
                color=discord.Color.red(),
            )
            await message.edit(embed=e)

        async def fetchAPI(url: str):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as response:
                        while response.status == 429:
                            await updateStatus("We're getting ratelimited by an API. Trying again in a few seconds...")
                            await asyncio.sleep(2.5)
                            async with session.get(url) as response:
                                pass
                        if response.status == 200:
                            return await response.json()
                        return None
            except Exception:
                return None

        def iterate(filter_key, obj):
            if isinstance(obj, list):
                for item in obj:
                    yield from iterate(filter_key, item)
            elif isinstance(obj, dict):
                for key, item in obj.items():
                    if key == filter_key:
                        yield item
                    else:
                        yield from iterate(filter_key, item)

        def prepareMemberList(guildObject):
            parsedObject = list(iterate("uuid", guildObject))
            memberList = parsedObject[1:]
            return memberList

        # Step 1: Fetch guild member list
        await updateStatus("Fetching the guild's member list from the API...")
        guildObject = await fetchAPI("https://api.wynncraft.com/v3/guild/Returners")
        if not guildObject:
            await updateStatus("Failed to fetch guild data from the API.")
            return

        memberUUIDs = prepareMemberList(guildObject)
        total = len(memberUUIDs)

        # Steps 2–4: For each member, fetch player data and record lastJoin
        cutoff = datetime.now(timezone.utc) - timedelta(days=INACTIVE_DAYS)
        inactive: dict[str, datetime] = {}

        for i, uuid in enumerate(memberUUIDs):
            percent = round(100 * (i + 1) / total) if total else 100
            await updateStatus(f"Checking member activity via the API... [{percent}%]")
            playerObject = await fetchAPI(f"https://api.wynncraft.com/v3/player/{uuid}")
            if not playerObject:
                continue
            last_join_list = list(iterate("lastJoin", playerObject))
            if not last_join_list:
                continue
            last_join_str = last_join_list[0]
            try:
                last_join_dt = datetime.fromisoformat(last_join_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if last_join_dt < cutoff:
                inactive[uuid] = last_join_dt

        # Step 5: If nobody is inactive, say so
        if not inactive:
            result_embed = discord.Embed(
                title=f"Purge List (inactive {INACTIVE_DAYS}+ days)",
                description=f"No members have been inactive for more than {INACTIVE_DAYS} days.",
                color=discord.Color.green(),
            )
            await message.edit(embed=result_embed)
            return

        # Step 6: Resolve UUIDs to usernames
        await updateStatus("Resolving UUIDs to usernames... [0%]")
        resolved: list[tuple[str, datetime]] = []
        items = list(inactive.items())
        for i, (uuid, last_join_dt) in enumerate(items):
            percent = round(100 * (i + 1) / len(items))
            await updateStatus(f"Resolving UUIDs to usernames... [{percent}%]")
            usernameObject = await fetchAPI(f"https://api.minecraftservices.com/minecraft/profile/lookup/{uuid}")
            username = usernameObject["name"] if usernameObject and "name" in usernameObject else uuid
            resolved.append((username, last_join_dt))

        # Step 7: Sort by last seen (longest absent first) and display
        resolved.sort(key=lambda x: x[1])
        now_dt = datetime.now(timezone.utc)
        lines = []
        for username, last_join_dt in resolved:
            days_ago = (now_dt - last_join_dt).days
            date_str = last_join_dt.strftime("%Y-%m-%d")
            lines.append(f"- `{username}` — last seen {days_ago} day(s) ago ({date_str})")

        description = "\n".join(lines)
        if len(description) > 4000:
            description = description[:4000] + "\n… (truncated)"

        result_embed = discord.Embed(
            title=f"Purge List (inactive {INACTIVE_DAYS}+ days)",
            description=description,
            color=discord.Color.blurple(),
        )
        await message.edit(embed=result_embed)


async def setup(bot: Bot):
    await bot.add_cog(TemporaryPruneCog(bot))

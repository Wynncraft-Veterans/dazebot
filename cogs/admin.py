import logging
from typing import Annotated
import discord
from discord.ext import commands

from lib.auth import is_admin
from lib.converters import CaseInsensitiveMember
from lib.role_state import ensure_linked_baseline
from lib.wynn_api.errors import WynnApiError
from lib.wynn_api.player import get_player_full_stats

logger = logging.getLogger("dazebot.cogs.admin")
from bot import Bot
from tortoise.expressions import Q
from orm import Blocklist, DiscordAccount, MinecraftAccount, UNKNOWN_LAST_ONLINE


class Admin(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Admin cog initialized")

    @commands.hybrid_command(name="sync", description="Sync slash commands")
    @is_admin()
    async def sync_commands(self, ctx: commands.Context):
        """Manually sync slash commands"""
        logger.info(f"Sync command initiated by {ctx.author} ({ctx.author.id}) in {ctx.guild}")
        try:
            synced = await self.bot.tree.sync()
            logger.info(f"Successfully synced {len(synced)} slash commands")
            await ctx.send(f"Successfully synced {len(synced)} command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")
            await ctx.send(f"Failed to sync commands: {e}")

    @commands.hybrid_command(name="reload", description="Reload a specific cog")
    @is_admin()
    async def reload_cog(self, ctx: commands.Context, cog_name: str):
        """Reload a cog or all cogs"""
        logger.info(f"Reload command initiated by {ctx.author} ({ctx.author.id}) for cog '{cog_name}'")
        try:
            if cog_name == "ALL":
                for ext in self.bot.extensions:
                    await self.bot.reload_extension(ext)
                logger.info("Successfully reloaded all cogs")
                await ctx.send("Successfully reloaded all cogs")
            else:
                await self.bot.reload_extension(f"cogs.{cog_name}")
                logger.info(f"Successfully reloaded cog '{cog_name}'")
                await ctx.send(f"Successfully reloaded {cog_name}")
            await self.bot.tree.sync()
            logger.debug(f"Synced slash commands after reloading '{cog_name}'")
        except Exception as e:
            logger.error(f"Failed to reload cog '{cog_name}': {e}")
            await ctx.send(f"Failed to reload {cog_name}: {e}")

    @commands.hybrid_command(name="load", description="Load a specific cog")
    @is_admin()
    async def load_cog(self, ctx: commands.Context, cog_name: str):
        """Load a specific cog"""
        logger.info(f"Load command initiated by {ctx.author} ({ctx.author.id}) for cog '{cog_name}'")
        try:
            await self.bot.load_extension(f"cogs.{cog_name}")
            logger.info(f"Successfully loaded cog '{cog_name}'")
            await ctx.send(f"Successfully loaded {cog_name}")
            await self.bot.tree.sync()
            logger.debug(f"Synced slash commands after loading '{cog_name}'")
        except Exception as e:
            logger.error(f"Failed to load cog '{cog_name}': {e}")
            await ctx.send(f"Failed to load {cog_name}: {e}")

    @commands.hybrid_command(name="unload", description="Unload a specific cog")
    @is_admin()
    async def unload_cog(self, ctx: commands.Context, cog_name: str):
        """Unload a specific cog"""
        logger.info(f"Unload command initiated by {ctx.author} ({ctx.author.id}) for cog '{cog_name}'")
        try:
            await self.bot.unload_extension(f"cogs.{cog_name}")
            logger.info(f"Successfully unloaded cog '{cog_name}'")
            await ctx.send(f"Successfully unloaded {cog_name}")
            await self.bot.tree.sync()
            logger.debug(f"Synced slash commands after unloading '{cog_name}'")
        except Exception as e:
            logger.error(f"Failed to unload cog '{cog_name}': {e}")
            await ctx.send(f"Failed to unload {cog_name}: {e}")

    @commands.hybrid_command(name="say")
    @is_admin()
    async def say(self, ctx: commands.Context, *, msg: str):
        if msg:
            await ctx.send(msg)
        else:
            await ctx.send("I cant repeat an empty message you dummy 😡😡😡")

    # TODO: Snagged from the internet. May not be the most optimal.
    @commands.hybrid_command(name="embed")
    @is_admin()
    async def embed(self, ctx: commands.Context, color: str = None, title: str = None, *, description: str = None):
        """Create a simple embed.

        Usage (prefix): embed [colour] [title] [description]
        - If the first token looks like a color (hex like #ff0000 or a named color like 'blue'), it will be used as the embed colour.
        - For titles with spaces when using the prefix form, wrap the title in quotes: embed #ff0000 "My Title" My description here
        - Slash command usage will populate `color`, `title`, and `description` automatically.
        """
        # If the command was invoked via slash or explicit args, prefer provided params
        if title is not None:
            if description is None:
                await ctx.send("You must provide a description for the embed.")
                return
            color_token = color
        else:
            # Fallback: parse raw message content for prefix-style invocation
            if not getattr(ctx, "message", None) or not getattr(ctx.message, "content", None):
                await ctx.send("Usage: embed [colour] [title] [description]. For titles with spaces, quote the title.")
                return
            parts = ctx.message.content.split(None, 1)
            if len(parts) < 2:
                await ctx.send("Usage: embed [colour] [title] [description].")
                return
            args_str = parts[1]

            def _looks_like_color(tok: str) -> bool:
                t = tok.strip()
                if t.startswith("#"):
                    t = t[1:]
                if t.startswith("0x"):
                    t = t[2:]
                return len(t) == 6 and all(c in "0123456789abcdefABCDEF" for c in t)

            # Check whether first token is a color name/hex; if so, consume it
            first = args_str.split(None, 1)[0]
            color_token = None
            remaining = args_str
            named_tokens = (
                "default",
                "blue",
                "red",
                "green",
                "gold",
                "orange",
                "purple",
                "teal",
                "magenta",
                "dark_blue",
                "dark_red",
                "dark_green",
                "blurple",
            )
            if _looks_like_color(first) or first.lower() in named_tokens:
                color_token = first
                remaining = args_str[len(first) :].lstrip()

            if not remaining:
                await ctx.send("You must provide a title and description for the embed.")
                return

            # Title parsing: support quoted titles for spaces
            if remaining[0] in ('"', "'"):
                q = remaining[0]
                idx = remaining.find(q, 1)
                if idx == -1:
                    await ctx.send("Unterminated quote in title.")
                    return
                title_parsed = remaining[1:idx]
                description_parsed = remaining[idx + 1 :].lstrip()
            else:
                parts2 = remaining.split(None, 1)
                title_parsed = parts2[0]
                description_parsed = parts2[1] if len(parts2) > 1 else ""

            title = title_parsed
            description = description_parsed
            color = color_token

            if not description:
                await ctx.send("You must provide a description for the embed.")
                return

        # Parse the colour token into a discord.Color
        col = discord.Color.default()
        if color:
            col_str = color.strip()
            if col_str.startswith("#"):
                col_str = col_str[1:]
            if col_str.startswith("0x"):
                col_str = col_str[2:]
            if len(col_str) == 6 and all(c in "0123456789abcdefABCDEF" for c in col_str):
                try:
                    col = discord.Color(int(col_str, 16))
                except Exception:
                    pass
            else:
                # Try named color helpers on discord.Color (e.g. blue(), red(), etc.)
                try:
                    color_func = getattr(discord.Color, col_str.lower())
                    if callable(color_func):
                        col = color_func()
                    else:
                        raise AttributeError()
                except Exception:
                    await ctx.send("Invalid color. Use hex (e.g., #ff0000) or a named color like 'blue'.")
                    return

        embed = discord.Embed(title=title, description=description, color=col)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="set_shout_count")
    @is_admin()
    async def set_shout_count(
        self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember], count: int
    ):
        """Forcefully set a user's shout_count (replaces the existing value)."""
        discord_acc, _ = await DiscordAccount.get_or_create(
            disc_uuid=str(user.id),
        )

        discord_acc.shout_count = count
        await discord_acc.save(update_fields=["shout_count"])
        await ctx.reply(
            f"Set shout_count for {user.mention} to {count}", allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.hybrid_group(name="link")
    @is_admin()
    async def link(self, ctx: commands.Context):
        """Link/unlink Discord ↔ Minecraft accounts"""
        if ctx.invoked_subcommand is None:
            await ctx.send("Use subcommands: set, remove, check")

    @link.command(name="set")
    async def link_set(
        self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember], username_or_uuid: str
    ):
        """Link a Discord user to a Minecraft account"""
        # Defer immediately: ensure_linked_baseline below can issue 1-2
        # role-mutation API calls, which can easily blow past Discord's 3s
        # interaction-ack window and otherwise yield "Unknown interaction".
        await ctx.defer()
        mc = await MinecraftAccount.filter(
            Q(uuid=username_or_uuid)
            | Q(mc_username__iexact=username_or_uuid)
            | Q(wynn_username__iexact=username_or_uuid)
        ).first()

        if mc is None:
            # Not in our DB yet (e.g. player isn't in any tracked guild and has
            # never self-linked). Fall back to the Wynncraft API and create a
            # row on the fly, mirroring the /join and /waitlist add behavior.
            try:
                fs = await get_player_full_stats(username_or_uuid)
            except WynnApiError as e:
                await ctx.reply(
                    f"Could not find `{username_or_uuid}` on Wynncraft: {e.message}"
                )
                return
            except Exception as e:  # noqa: BLE001 — third-party API
                logger.exception("/link set: get_player_full_stats failed")
                await ctx.reply(f"Failed to fetch Wynncraft stats for `{username_or_uuid}`: {e}")
                return
            mc = await MinecraftAccount.create(
                uuid=fs.uuid,
                wynn_username=fs.username,
                mc_username=fs.username,
                guild=fs.guild.name if fs.guild else None,
                last_online=fs.lastJoin or UNKNOWN_LAST_ONLINE,
                last_manual_check=UNKNOWN_LAST_ONLINE,
                first_join=fs.firstJoin,
            )

        # Check if the MC account is already claimed by a different discord user
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

        # Apply the linked-account role invariant: in Returners -> MEMBER,
        # else REGISTERED. Same logic as the chat-code link path.
        baseline_note = ""
        try:
            blocked = await Blocklist.filter(minecraft_account=mc).exists()
            await ensure_linked_baseline(
                user,
                in_returners=(mc.guild == "Returners"),
                blocked=blocked,
                reason=f"/link set by {ctx.author}",
            )
        except discord.HTTPException as e:
            logger.warning(f"/link set: ensure_linked_baseline failed for {user}: {e}")
            baseline_note = f"\n\u26a0\ufe0f Could not update roles: {e}"
        except Exception:  # noqa: BLE001
            logger.exception("/link set: ensure_linked_baseline crashed")
            baseline_note = "\n\u26a0\ufe0f Could not update roles (see logs)."

        await ctx.reply(
            f"Linked {user.mention} to Minecraft account `{mc.mc_username}`.{baseline_note}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @link.command(name="remove")
    async def link_remove(self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember]):
        """Unlink a Discord account from its Minecraft account"""
        disc = await DiscordAccount.filter(disc_uuid=str(user.id)).select_related("minecraft_account").first()

        if disc is None or disc.minecraft_account_id is None:
            await ctx.reply(
                f"{user.mention} is not linked to any Minecraft account.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        assert disc.minecraft_account is not None
        mc_username = disc.minecraft_account.mc_username

        disc.minecraft_account_id = None
        await disc.save(update_fields=["minecraft_account_id"])
        await ctx.reply(
            f"Unlinked {user.mention} from Minecraft account `{mc_username}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # TODO: make this a group such that
    # - link check mc <mc username or uuid>
    # - link check disc <disc>
    @link.command(name="check")
    async def link_check(self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember]):
        """Check a Discord user's linked Minecraft account"""
        disc = await DiscordAccount.filter(disc_uuid=str(user.id)).select_related("minecraft_account").first()

        if disc is None or disc.minecraft_account is None:
            await ctx.reply(
                f"{user.mention} is not linked to any Minecraft account.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        mc = disc.minecraft_account
        embed = discord.Embed(title="Linked Account", color=discord.Color.blue())
        embed.add_field(name="Discord", value=user.mention, inline=True)
        # TODO: idk make clearer distinction between mc_username and wynn_username everywhere, or only display mc_username, maybe with a symbol to notify wynn_username differs, then admins can run a command to fetch both names
        embed.add_field(
            name="Minecraft",
            value=f"`{mc.mc_username}` {f'(`{mc.wynn_username}`)' if mc.wynn_username != mc.mc_username else ''}",
            inline=True,
        )
        embed.add_field(name="UUID", value=f"`{mc.uuid}`", inline=False)
        if mc.first_join:
            embed.add_field(name="First Join", value=f"<t:{int(mc.first_join.timestamp())}:F>", inline=True)
        await ctx.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_group(name="honourary")
    @is_admin()
    async def honourary(self, ctx: commands.Context):
        """Manage honourary bridge access"""
        if ctx.invoked_subcommand is None:
            await ctx.send("Use subcommands: set, remove")

    @honourary.command(name="set")
    async def honourary_set(self, ctx: commands.Context, username_or_uuid: str):
        """Grant honourary bridge access to a MC account"""
        mc = await MinecraftAccount.filter(Q(uuid=username_or_uuid) | Q(mc_username__iexact=username_or_uuid)).first()
        if mc is None:
            await ctx.reply(f"`{username_or_uuid}` not found.")
            return

        if mc.is_honourary:
            await ctx.reply(f"`{mc.mc_username}` is already honourary.")
            return

        mc.is_honourary = True
        await mc.save(update_fields=["is_honourary"])
        await ctx.reply(f"`{mc.mc_username}` is now honourary.")

    @honourary.command(name="remove")
    async def honourary_remove(self, ctx: commands.Context, username_or_uuid: str):
        """Revoke honourary bridge access"""
        mc = await MinecraftAccount.filter(Q(uuid=username_or_uuid) | Q(mc_username__iexact=username_or_uuid)).first()
        if mc is None:
            await ctx.reply(f"`{username_or_uuid}` not found.")
            return

        if not mc.is_honourary:
            await ctx.reply(f"`{mc.mc_username}` is not honourary.")
            return

        mc.is_honourary = False
        mc.token = None
        await mc.save(update_fields=["is_honourary", "token"])
        await ctx.reply(f"`{mc.mc_username}` is no longer honourary. Token revoked.")


async def setup(bot: Bot):
    await bot.add_cog(Admin(bot))
    logger.info("Admin cog loaded successfully")

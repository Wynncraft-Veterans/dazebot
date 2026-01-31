import logging
import discord
from discord.ext import commands

from lib.auth import is_admin

logger = logging.getLogger("discord.cogs.admin")
from bot import Bot
from tortoise.expressions import Q
from orm import DiscordAccount, MinecraftAccount, Person


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
        """Reload a specific cog"""
        logger.info(f"Reload command initiated by {ctx.author} ({ctx.author.id}) for cog '{cog_name}'")
        try:
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
    async def set_shout_count(self, ctx: commands.Context, user: discord.Member, count: int):
        """Forcefully set a user's shout_count (replaces the existing value).
        This will also create or delete `Shout` rows so `shouterboard` (which aggregates Shout rows)
        reflects the new value.
        """
        discord_acc, _ = await DiscordAccount.get_or_create(
            disc_uuid=str(user.id),
        )

        discord_acc.shout_count = count
        await discord_acc.save(update_fields=["shout_count"])
        await ctx.reply(f"Set shout_count for {user.mention} to {count}")

    @commands.hybrid_group(name="person")
    @is_admin()
    async def person(self, ctx: commands.Context):
        """Manage person accounts linking Minecraft and Discord"""
        if ctx.invoked_subcommand is None:
            await ctx.send("Use subcommands: link, unlink, check")

    @person.command(name="link")
    async def person_link(self, ctx: commands.Context, user: discord.Member, username_or_uuid: str):
        """Link a Discord user to a Minecraft account"""
        player = (
            await MinecraftAccount.filter(Q(uuid=username_or_uuid) | Q(username=username_or_uuid))
            .prefetch_related("person")
            .first()
        )

        if player is None:
            await ctx.reply("That minecraft user is not available. Try forcing a check on the guild.")
            return

        discord_acc, _ = await DiscordAccount.get_or_create(
            disc_uuid=str(user.id),
        )
        await discord_acc.fetch_related("person")

        if player.person and discord_acc.person:
            if player.person.id == discord_acc.person.id:
                await ctx.reply(f"{user.mention} is already linked to Minecraft account `{player.username}`")
            else:
                await ctx.reply("Both accounts are already linked to different persons. Please unlink first.")

        elif player.person:
            discord_acc.person = player.person
            await discord_acc.save()
            await ctx.reply(f"Linked {user.mention} to existing person with Minecraft account `{player.username}`")

        elif discord_acc.person:
            player.person = discord_acc.person
            await player.save()
            await ctx.reply(f"Linked Minecraft account `{player.username}` to {user.mention}'s existing person")

        else:
            person_obj = await Person.create(name=user.display_name)
            player.person = person_obj
            discord_acc.person = person_obj
            await player.save()
            await discord_acc.save()
            await ctx.reply(f"Created new person and linked {user.mention} to Minecraft account `{player.username}`")

    @person.group(name="unlink")
    async def person_unlink(self, ctx: commands.Context):
        """Unlink accounts from persons"""
        if ctx.invoked_subcommand is None:
            await ctx.send("Use: unlink mc <username> or unlink disc <user>")

    @person_unlink.command(name="mc")
    async def person_unlink_mc(self, ctx: commands.Context, username_or_uuid: str):
        """Unlink a Minecraft account from its person"""
        player = (
            await MinecraftAccount.filter(Q(uuid=username_or_uuid) | Q(username=username_or_uuid))
            .prefetch_related("person")
            .first()
        )

        if player is None:
            await ctx.reply("That minecraft user was not found.")
            return

        if player.person is None:
            await ctx.reply(f"Minecraft account `{player.username}` is not linked to any person.")
            return

        player.person = None
        await player.save()
        await ctx.reply(f"Unlinked Minecraft account `{player.username}` from person.")

    @person_unlink.command(name="disc")
    @is_admin()
    async def person_unlink_disc(self, ctx: commands.Context, user: discord.Member):
        """Unlink a Discord account from its person"""
        discord_acc = await DiscordAccount.filter(disc_uuid=str(user.id)).prefetch_related("person").first()

        if discord_acc is None:
            await ctx.reply(f"{user.mention} does not have a Discord account registered.")
            return

        if discord_acc.person is None:
            await ctx.reply(f"{user.mention} is not linked to any person.")
            return

        discord_acc.person = None
        await discord_acc.save()
        await ctx.reply(f"Unlinked {user.mention} from person.")

    @person.group(name="check")
    async def person_check(self, ctx: commands.Context):
        """Check linked accounts"""
        if ctx.invoked_subcommand is None:
            await ctx.send("Use: check mc <username> or check disc <user>")

    @person_check.command(name="mc")
    async def person_check_mc(self, ctx: commands.Context, username_or_uuid: str):
        """Check a Minecraft account's linked person and all associated accounts"""
        player = (
            await MinecraftAccount.filter(Q(uuid=username_or_uuid) | Q(username__iexact=username_or_uuid))
            .prefetch_related("person")
            .first()
        )

        if player is None:
            await ctx.reply("That minecraft user was not found.")
            return

        if player.person is None:
            await ctx.reply(f"Minecraft account `{player.username}` is not linked to any person.")
            return

        # Fetch all linked accounts for this person
        person_obj = await Person.get(id=player.person.id).prefetch_related("minecraft_accounts", "discord_accounts")

        embed = discord.Embed(title=f"Person: {person_obj.name or 'Unnamed'}", color=discord.Color.blue())

        # Minecraft accounts
        mc_accounts = [f"• `{acc.username}` ({acc.uuid})" for acc in person_obj.minecraft_accounts]
        if mc_accounts:
            embed.add_field(name="Minecraft Accounts", value="\n".join(mc_accounts), inline=False)

        # Discord accounts
        discord_accounts = []
        for acc in person_obj.discord_accounts:
            try:
                user_obj = await self.bot.fetch_user(int(acc.disc_uuid))
                discord_accounts.append(f"• {user_obj.mention} ({user_obj.name})")
            except:
                discord_accounts.append(f"• <@{acc.disc_uuid}>")

        if discord_accounts:
            embed.add_field(name="Discord Accounts", value="\n".join(discord_accounts), inline=False)

        await ctx.reply(embed=embed)

    @person_check.command(name="disc")
    async def person_check_disc(self, ctx: commands.Context, user: discord.Member):
        """Check a Discord account's linked person and all associated accounts"""
        discord_acc = await DiscordAccount.filter(disc_uuid=str(user.id)).prefetch_related("person").first()

        if discord_acc is None:
            await ctx.reply(f"{user.mention} does not have a Discord account registered.")
            return

        if discord_acc.person is None:
            await ctx.reply(f"{user.mention} is not linked to any person.")
            return

        # Fetch all linked accounts for this person
        person_obj = await Person.get(id=discord_acc.person.id).prefetch_related(
            "minecraft_accounts", "discord_accounts"
        )

        embed = discord.Embed(title=f"Person: {person_obj.name or 'Unnamed'}", color=discord.Color.blue())

        # Minecraft accounts
        mc_accounts = [f"• `{acc.username}` ({acc.uuid})" for acc in person_obj.minecraft_accounts]
        if mc_accounts:
            embed.add_field(name="Minecraft Accounts", value="\n".join(mc_accounts), inline=False)

        # Discord accounts
        discord_accounts = []
        for acc in person_obj.discord_accounts:
            try:
                user_obj = await self.bot.fetch_user(int(acc.disc_uuid))
                discord_accounts.append(f"• {user_obj.mention} ({user_obj.name})")
            except:
                discord_accounts.append(f"• <@{acc.disc_uuid}>")

        if discord_accounts:
            embed.add_field(name="Discord Accounts", value="\n".join(discord_accounts), inline=False)

        await ctx.reply(embed=embed)


async def setup(bot: Bot):
    await bot.add_cog(Admin(bot))
    logger.info("Admin cog loaded successfully")

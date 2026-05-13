"""Admin cog: bot-maintenance (``/admin sync|reload|load|unload``),
``/say``/``/embed``, the ``/link`` group (set/remove/check/alt/request/
requests/approve/code/info), and ``/shouts set``."""

import logging
from typing import Annotated
import discord
from discord.ext import commands
from tortoise.expressions import Q

from bot import Bot
from config import CurrConfig
from lib.auth import is_admin, is_operator, is_staff, is_registered
from lib.discord_utils.converters import CaseInsensitiveMember
from lib.discord_utils.paginated_embed import Paginator, from_lines
from lib.mc.linking import dm_or_log, get_or_issue_code
from lib.mc.resolve import ensure_mc_account
from lib.mc.wynn_api.errors import WynnApiError
from lib.role_state import ensure_linked_baseline
from orm import Blocklist, DiscordAccount, LinkRequest, MinecraftAccount, MinecraftAlt

logger = logging.getLogger("dazebot.cogs.moderation.admin")


class Admin(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Admin cog initialized")

    # =================================================================
    # /admin (operator)  — bot-internal maintenance
    # =================================================================

    @commands.hybrid_group(name="admin", description="(Operator) Bot maintenance commands")
    @is_operator()
    async def admin_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("Use subcommands: sync, reload, load, unload")

    @admin_group.command(name="sync", description="Sync slash commands")
    async def admin_sync(self, ctx: commands.Context):
        logger.info(f"/admin sync initiated by {ctx.author} ({ctx.author.id}) in {ctx.guild}")
        try:
            synced = await self.bot.tree.sync()
            logger.info(f"Successfully synced {len(synced)} slash commands")
            await ctx.send(f"Successfully synced {len(synced)} command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")
            await ctx.send(f"Failed to sync commands: {e}")

    @admin_group.command(name="reload", description="Reload a specific cog")
    async def admin_reload(self, ctx: commands.Context, cog_name: str):
        logger.info(f"/admin reload initiated by {ctx.author} ({ctx.author.id}) for cog '{cog_name}'")
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
        except Exception as e:
            logger.error(f"Failed to reload cog '{cog_name}': {e}")
            await ctx.send(f"Failed to reload {cog_name}: {e}")

    @admin_group.command(name="load", description="Load a specific cog")
    async def admin_load(self, ctx: commands.Context, cog_name: str):
        logger.info(f"/admin load initiated by {ctx.author} ({ctx.author.id}) for cog '{cog_name}'")
        try:
            await self.bot.load_extension(f"cogs.{cog_name}")
            logger.info(f"Successfully loaded cog '{cog_name}'")
            await ctx.send(f"Successfully loaded {cog_name}")
            await self.bot.tree.sync()
        except Exception as e:
            logger.error(f"Failed to load cog '{cog_name}': {e}")
            await ctx.send(f"Failed to load {cog_name}: {e}")

    @admin_group.command(name="unload", description="Unload a specific cog")
    async def admin_unload(self, ctx: commands.Context, cog_name: str):
        logger.info(f"/admin unload initiated by {ctx.author} ({ctx.author.id}) for cog '{cog_name}'")
        try:
            await self.bot.unload_extension(f"cogs.{cog_name}")
            logger.info(f"Successfully unloaded cog '{cog_name}'")
            await ctx.send(f"Successfully unloaded {cog_name}")
            await self.bot.tree.sync()
        except Exception as e:
            logger.error(f"Failed to unload cog '{cog_name}': {e}")
            await ctx.send(f"Failed to unload {cog_name}: {e}")

    # =================================================================
    # /say, /embed (admin)
    # =================================================================

    @commands.hybrid_command(name="say")
    @is_admin()
    async def say(self, ctx: commands.Context, *, msg: str):
        # Slash-modal fields can't carry real newlines; allow literal \n.
        msg = msg.replace("\\n", "\n")
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
        if title is not None:
            if description is None:
                await ctx.send("You must provide a description for the embed.")
                return
            color_token = color
        else:
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

            first = args_str.split(None, 1)[0]
            color_token = None
            remaining = args_str
            named_tokens = (
                "default", "blue", "red", "green", "gold", "orange", "purple",
                "teal", "magenta", "dark_blue", "dark_red", "dark_green", "blurple",
            )
            if _looks_like_color(first) or first.lower() in named_tokens:
                color_token = first
                remaining = args_str[len(first):].lstrip()

            if not remaining:
                await ctx.send("You must provide a title and description for the embed.")
                return

            if remaining[0] in ('"', "'"):
                q = remaining[0]
                idx = remaining.find(q, 1)
                if idx == -1:
                    await ctx.send("Unterminated quote in title.")
                    return
                title_parsed = remaining[1:idx]
                description_parsed = remaining[idx + 1:].lstrip()
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
                try:
                    color_func = getattr(discord.Color, col_str.lower())
                    if callable(color_func):
                        col = color_func()
                    else:
                        raise AttributeError()
                except Exception:
                    await ctx.send("Invalid color. Use hex (e.g., #ff0000) or a named color like 'blue'.")
                    return

        # Slash-modal fields can't carry real newlines; allow literal \n.
        if title is not None:
            title = title.replace("\\n", "\n")
        if description is not None:
            description = description.replace("\\n", "\n")
        embed = discord.Embed(title=title, description=description, color=col)
        await ctx.send(embed=embed)

    # =================================================================
    # /shouts (operator)  — set raw shout-count value
    # =================================================================

    @commands.hybrid_group(name="shouts", description="(Operator) Manage raw shout counters")
    @is_operator()
    async def shouts_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("Use subcommands: set")

    @shouts_group.command(name="set", description="Forcefully set a user's shout_count")
    async def shouts_set(
        self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember], count: int
    ):
        discord_acc, _ = await DiscordAccount.get_or_create(disc_uuid=str(user.id))
        discord_acc.shout_count = count
        await discord_acc.save(update_fields=["shout_count"])
        await ctx.reply(
            f"Set shout_count for {user.mention} to {count}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # =================================================================
    # /link (mixed tiers per subcommand — group itself is ungated)
    # =================================================================

    @commands.hybrid_group(name="link", description="Link/unlink Discord ↔ Minecraft accounts")
    async def link(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            # Prefix-mode fallback: surface the info embed.
            await self.link_info.callback(self, ctx)

    @link.command(name="set", description="(Staff) Link a Discord user to a Minecraft account")
    @is_staff()
    async def link_set(
        self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember], username_or_uuid: str
    ):
        # Defer immediately: ensure_linked_baseline below can issue 1-2
        # role-mutation API calls, which can easily blow past Discord's 3s
        # interaction-ack window and otherwise yield "Unknown interaction".
        await ctx.defer()
        try:
            mc = await ensure_mc_account(username_or_uuid)
        except WynnApiError as e:
            await ctx.reply(f"Could not find `{username_or_uuid}` on Wynncraft: {e.message}")
            return
        except Exception as e:  # noqa: BLE001 — third-party API
            logger.exception("/link set: ensure_mc_account failed")
            await ctx.reply(f"Failed to fetch Wynncraft stats for `{username_or_uuid}`: {e}")
            return

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

    @link.command(name="remove", description="(Staff) Unlink a Discord account from its Minecraft account")
    @is_staff()
    async def link_remove(self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember]):
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

    @link.command(name="check", description="(Staff) Check a Discord user's linked Minecraft account")
    @is_staff()
    async def link_check(self, ctx: commands.Context, user: Annotated[discord.Member, CaseInsensitiveMember]):
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
        embed.add_field(
            name="Minecraft",
            value=f"`{mc.mc_username}` {f'(`{mc.wynn_username}`)' if mc.wynn_username != mc.mc_username else ''}",
            inline=True,
        )
        embed.add_field(name="UUID", value=f"`{mc.uuid}`", inline=False)
        if mc.first_join:
            embed.add_field(name="First Join", value=f"<t:{int(mc.first_join.timestamp())}:F>", inline=True)
        await ctx.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @link.command(name="alt", description="(Staff) Register an alt Minecraft account for a Discord user.")
    @is_staff()
    async def link_alt(
        self,
        ctx: commands.Context,
        username: str,
        user: Annotated[discord.Member, CaseInsensitiveMember],
    ):
        try:
            mc = await ensure_mc_account(username)
        except WynnApiError as e:
            await ctx.reply(f"Could not find `{username}` on Wynncraft: {e.message}")
            return
        except Exception as e:  # noqa: BLE001 — third-party API
            logger.exception("/link alt: ensure_mc_account failed")
            await ctx.reply(f"Failed to fetch Wynncraft stats for `{username}`: {e}")
            return
        disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(user.id))

        # If they don't have a primary linked yet, treat /link alt as a primary link.
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

    @link.command(
        name="request",
        description="(Registered) Request a manual link to a Minecraft account (alts only post-migration).",
    )
    @is_registered()
    async def link_request(self, ctx: commands.Context, username_or_uuid: str):
        """Manual, staff-approved link flow. Practically only useful for alt
        accounts now; the primary link flow is the welcome-channel button or
        ``/link code``.
        """
        existing_disc = (
            await LinkRequest.filter(discord_account_id=ctx.author.id).prefetch_related("minecraft_account").first()
        )
        if existing_disc is not None:
            await ctx.reply(
                f"You ({ctx.author.mention}) already have a pending link request to "
                f"`{existing_disc.minecraft_account.mc_username}` (`{existing_disc.minecraft_account.uuid}`)",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        existing_mc = (
            await LinkRequest.filter(
                Q(minecraft_account__uuid=username_or_uuid)
                | Q(minecraft_account__mc_username__iexact=username_or_uuid)
            )
            .prefetch_related("minecraft_account", "discord_account")
            .first()
        )
        if existing_mc is not None:
            await ctx.reply(
                f"There is already a pending link request to "
                f"`{existing_mc.minecraft_account.mc_username}` (`{existing_mc.minecraft_account.uuid}`)",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        linked_mc = await MinecraftAccount.filter(discord_account__disc_uuid=str(ctx.author.id)).first()
        if linked_mc is not None:
            await ctx.reply(
                f"You ({ctx.author.mention}) are already linked to `{linked_mc.mc_username}` (`{linked_mc.uuid}`)",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        linked_disc = (
            await DiscordAccount.filter(
                Q(minecraft_account__uuid=username_or_uuid)
                | Q(minecraft_account__mc_username__iexact=username_or_uuid)
            )
            .prefetch_related("minecraft_account")
            .first()
        )
        if linked_disc is not None and linked_disc.minecraft_account is not None:
            await ctx.reply(
                f"`{linked_disc.minecraft_account.mc_username}` is already linked to another Discord account.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            mc = await ensure_mc_account(username_or_uuid)
        except WynnApiError as e:
            await ctx.reply(f"Could not find `{username_or_uuid}` on Wynncraft: {e.message}")
            return

        disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(ctx.author.id))
        await LinkRequest.create(minecraft_account=mc, discord_account=disc)

        await ctx.reply(
            f"Link request created for {ctx.author.mention} → `{mc.mc_username}` (`{mc.uuid}`). "
            "A staff member will approve it.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @link.command(name="requests", description="(Staff) List pending link requests")
    @is_staff()
    async def link_requests(self, ctx: commands.Context):
        lrs = await LinkRequest.all().prefetch_related("minecraft_account", "discord_account")
        lrs.sort(key=lambda e: e.created_at)
        lines = [
            f"{lr.id} - <t:{int(lr.created_at.timestamp())}:R> - <@{lr.discord_account.disc_uuid}> - "
            f"`{lr.minecraft_account.mc_username}` (`{lr.minecraft_account.uuid}`)"
            for lr in lrs
        ]
        if not lines:
            await ctx.reply("No pending link requests.")
            return

        embeds = from_lines(
            title="Current link requests",
            lines=lines,
            lines_per_page=10,
            logger=logger,
        )
        await ctx.send(
            embed=embeds[0],
            view=Paginator(embeds),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @link.command(name="approve", description="(Staff) Approve a pending link request by id")
    @is_staff()
    async def link_approve(self, ctx: commands.Context, id: str):
        lr = await LinkRequest.filter(id=id).prefetch_related("minecraft_account", "discord_account").first()
        if lr is None:
            await ctx.reply(f"That (`{id}`) id doesn't exist.")
            return

        disc = lr.discord_account
        mc = lr.minecraft_account

        disc.minecraft_account = mc
        await disc.save()
        await lr.delete()

        user = await self.bot.fetch_user(int(disc.disc_uuid))
        await ctx.reply(
            f"Linked {user.mention} to `{mc.mc_username}` (`{mc.uuid}`).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @link.command(name="code", description="Get a one-time link code to claim a Minecraft account")
    async def link_code(self, ctx: commands.Context, username: str):
        # Reject if username already linked to anyone.
        existing_link = await MinecraftAccount.filter(mc_username__iexact=username).first()
        if existing_link is not None:
            owner = await DiscordAccount.filter(minecraft_account=existing_link).first()
            if owner is not None:
                if owner.disc_uuid == str(ctx.author.id):
                    await ctx.reply(
                        f"You are already linked to `{existing_link.mc_username}`.",
                        ephemeral=True,
                    )
                else:
                    await ctx.reply(
                        f"`{existing_link.mc_username}` is already linked to another Discord account.",
                        ephemeral=True,
                    )
                return

        row, is_new = await get_or_issue_code(str(ctx.author.id), username)

        body = (
            f"Your link code for **{username}**:\n\n"
            f"```\n{row.code}\n```\n"
            f"1. Connect to `{CurrConfig.MC_PUBLIC_HOST}` in Minecraft.\n"
            f"2. Type the code above into chat.\n\n"
            f"The code is persistent — it does not expire and you can re-run "
            f"`/link code {username}` any time to see it again."
        )

        dmed = await dm_or_log(ctx.author, body, fallback_logger=logger)
        if dmed:
            verb = "issued" if is_new else "re-sent existing"
            await ctx.reply(f"Code {verb} via DM. Check your messages.", ephemeral=True)
        else:
            await ctx.reply(body, ephemeral=True)

    @link.command(name="info", description="How to link your Minecraft account to your Discord")
    async def link_info(self, ctx: commands.Context):
        host = getattr(CurrConfig, "MC_PUBLIC_HOST", "(server IP not configured)")
        embed = discord.Embed(
            title="Linking your Minecraft account",
            description=(
                "There are two ways to link a Minecraft account to your Discord account:\n\n"
                "**1. Self-link (recommended) — `/link code <username>`**\n"
                "I'll DM you a one-time code. Connect to "
                f"`{host}` in Minecraft and type the code into chat. Done.\n\n"
                "**2. Manual request — `/link request <username>`**\n"
                "Files a request that staff approve manually. Slower; intended for alt accounts.\n\n"
                "Staff can also force-link with `/link set`."
            ),
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: Bot):
    await bot.add_cog(Admin(bot))
    logger.info("Admin cog loaded successfully")

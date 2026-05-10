"""``/vetsmod`` — issue a vetsmod authentication key.

The user runs ``/vetsmod`` in any channel; we DM them (or, if DMs are closed,
ping them in ``LINK_FALLBACK_CHANNEL`` with retry/show buttons) the modrinth
download link plus an ``/unlock <key>`` command they paste into Minecraft.

The key authenticates that user's vetsmod client to ``api.wynnvets.org``.
See :mod:`lib.verify_keys` for issuance/introspection helpers.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import Bot
from config import CurrConfig
from lib.linking import dm_or_log
from lib.verify_keys import (
    TIER_HONOURARY,
    TIER_MEMBER,
    TIER_OTHER,
    TIER_WAITLIST,
    get_or_issue_key,
    revoke_key,
    rotate_key,
)
from orm import VerifyKey

logger = logging.getLogger("dazebot.cogs.vetsmod")

VETSMOD_MODRINTH_URL = "https://modrinth.com/mod/vetsmod/versions"

VETSMOD_FALLBACK_DM_CUSTOM_ID = "vetsmod:fallback_dm"
VETSMOD_FALLBACK_SHOW_CUSTOM_ID = "vetsmod:fallback_show"


_TIER_BLURB = {
    TIER_MEMBER: (
        "You're an in-game **VETS member** — the mod will give you full guild "
        "chat access (sending and receiving)."
    ),
    TIER_WAITLIST: (
        "You're on the **waitlist** — the mod will let you read and post in "
        "the waitlist channel until you're promoted."
    ),
    TIER_HONOURARY: (
        "You're an **honourary VETS member** — the mod will give you the "
        "honourary chat channel."
    ),
    TIER_OTHER: (
        "You don't currently have any VETS chat access — you'll be able to install "
        "the mod, but guild/waitlist/honourary channels will be hidden until your "
        "Discord roles change."
    ),
}


def _build_dm_body(row: VerifyKey, *, is_new: bool) -> str:
    blurb = _TIER_BLURB.get(row.tier, _TIER_BLURB[TIER_OTHER])
    intro = (
        "Here's your vetsmod authentication key. **Don't share it** — it's "
        "tied to your Discord account and lets your client read/post VETS chat."
    )
    if not is_new:
        intro = (
            "You already had a vetsmod key on file. Re-sending the same one. "
            "If you think it leaked, run `/vetsmod rotate` to get a fresh key "
            "(the old one stops working immediately)."
        )
    return (
        f"{intro}\n\n"
        f"**1. Install the mod:** {VETSMOD_MODRINTH_URL}\n"
        f"**2. In Minecraft, run this once:**\n"
        f"```\n/unlock {row.key}\n```\n"
        f"{blurb}\n\n"
        "_Tier: `" + row.tier + "`_"
    )


class _VetsmodFallbackView(discord.ui.View):
    """Persistent buttons posted in ``LINK_FALLBACK_CHANNEL`` when DMs fail.

    Each callback resolves the *clicker's* own VerifyKey row by ``disc_uuid``.
    Stale fallback messages still work for the right user; other users
    clicking them get a polite "no key on file" reply.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Try DM again",
        style=discord.ButtonStyle.primary,
        emoji="\U0001f4ec",  # incoming envelope
        custom_id=VETSMOD_FALLBACK_DM_CUSTOM_ID,
    )
    async def retry_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await VerifyKey.filter(disc_uuid=str(interaction.user.id)).first()
        if row is None or row.revoked_at is not None:
            await interaction.followup.send(
                "You don't have an active vetsmod key. Run `/vetsmod` to issue one.",
                ephemeral=True,
            )
            return
        body = _build_dm_body(row, is_new=False)
        if await dm_or_log(interaction.user, body, fallback_logger=logger):
            await interaction.followup.send(
                "✅ Sent your vetsmod key to your DMs.", ephemeral=True
            )
            return
        await interaction.followup.send(
            "⚠️ Still couldn't DM you. Use **Show my key** to view it "
            "privately here, or open your DMs from VETS members and try again.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Show my key",
        style=discord.ButtonStyle.secondary,
        emoji="\U0001f441️",  # eye
        custom_id=VETSMOD_FALLBACK_SHOW_CUSTOM_ID,
    )
    async def show_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await VerifyKey.filter(disc_uuid=str(interaction.user.id)).first()
        if row is None or row.revoked_at is not None:
            await interaction.followup.send(
                "You don't have an active vetsmod key. Run `/vetsmod` to issue one.",
                ephemeral=True,
            )
            return
        body = _build_dm_body(row, is_new=False)
        await interaction.followup.send(
            "Here's your vetsmod key (only visible to you):\n\n" + body,
            ephemeral=True,
        )


VetsmodFallbackView = _VetsmodFallbackView  # public re-export for bot.add_view


async def _post_fallback_ping(
    bot: Bot, user: discord.abc.User, *, body: str
) -> Optional[discord.Message]:
    channel_id = getattr(CurrConfig, "LINK_FALLBACK_CHANNEL", None)
    if not channel_id:
        logger.warning("LINK_FALLBACK_CHANNEL not configured; cannot post vetsmod fallback")
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            logger.exception("vetsmod fallback channel fetch failed (id=%s)", channel_id)
            return None
    if not isinstance(channel, discord.abc.Messageable):
        logger.error("vetsmod fallback channel %s is not messageable", channel_id)
        return None
    content = (
        f"<@{user.id}> — I couldn't DM you your vetsmod key (your DMs are "
        "closed). Use the buttons below to retry the DM or view your key "
        "privately here without exposing it publicly."
    )
    try:
        return await channel.send(
            content,
            view=_VetsmodFallbackView(),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user.id)]),
        )
    except discord.HTTPException:
        logger.exception("failed to post vetsmod fallback for %s", user.id)
        return None


class Vetsmod(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Vetsmod cog initialized")

    @commands.hybrid_group(
        name="vetsmod",
        description="Get your vetsmod authentication key (DMed by default).",
        invoke_without_command=True,
    )
    async def vetsmod_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is not None:
            return
        await self._issue_for_invoker(ctx, force_rotate=False)

    @vetsmod_group.command(
        name="rotate",
        description="Invalidate your existing vetsmod key and issue a fresh one.",
    )
    async def vetsmod_rotate(self, ctx: commands.Context):
        await self._issue_for_invoker(ctx, force_rotate=True)

    @vetsmod_group.command(
        name="revoke",
        description="(Staff) Revoke another user's vetsmod key.",
    )
    @app_commands.describe(user="The Discord user whose key should be invalidated.")
    @commands.has_permissions(manage_guild=True)
    async def vetsmod_revoke(self, ctx: commands.Context, user: discord.Member):
        revoked = await revoke_key(str(user.id), reason=f"staff:{ctx.author.id}")
        if revoked:
            await ctx.reply(
                f"✅ Revoked vetsmod key for {user.mention}. Their client "
                "will fail authentication on its next reconnect.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.reply(
                f"{user.mention} has no active vetsmod key to revoke.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _issue_for_invoker(self, ctx: commands.Context, *, force_rotate: bool):
        if not isinstance(ctx.author, discord.Member):
            await ctx.reply(
                "This command must be used in a server, not a DM.", ephemeral=True
            )
            return

        await ctx.defer(ephemeral=True)

        if force_rotate:
            row = await rotate_key(ctx.author)
            is_new = True
        else:
            issued = await get_or_issue_key(ctx.author)
            row = issued.row
            is_new = issued.is_new

        body = _build_dm_body(row, is_new=is_new)

        dmed = await dm_or_log(ctx.author, body, fallback_logger=logger)
        if dmed:
            note = (
                "✅ Sent your vetsmod key to your DMs."
                if is_new
                else "ℹ️ You already had a key; I re-DMed it to you."
            )
            if force_rotate:
                note = "✅ Issued a fresh vetsmod key and DMed it to you."
            await ctx.reply(note, ephemeral=True)
            return

        posted = await _post_fallback_ping(self.bot, ctx.author, body=body)
        if posted is not None:
            channel_mention = f"<#{posted.channel.id}>"
            await ctx.reply(
                "⚠️ I couldn't DM you (your DMs are closed). I've "
                f"pinged you in {channel_mention} — use the buttons there "
                "to retry the DM or view the key privately.",
                ephemeral=True,
            )
            return

        # Last-ditch: reveal in the ephemeral reply itself.
        await ctx.reply(
            "⚠️ Couldn't DM you and the fallback channel is "
            "unavailable. Here's your key, **only visible to you**:\n\n" + body,
            ephemeral=True,
        )


async def setup(bot: Bot):
    await bot.add_cog(Vetsmod(bot))
    logger.info("Vetsmod cog loaded successfully")

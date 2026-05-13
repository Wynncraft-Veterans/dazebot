"""``/vetsmod`` — issue a vetsmod authentication key (DMed by default).

The user runs ``/vetsmod`` in any channel; we DM them (or, if DMs are closed,
ping them in ``LINK_FALLBACK_CHANNEL`` with retry/show buttons) the modrinth
download link plus an ``/unlock <key>`` command they paste into Minecraft.

The key authenticates that user's vetsmod client to ``api.wynnvets.org``.
See :mod:`lib.verify_keys` for issuance/introspection helpers.

Staff key management lives under ``/change`` (mod-tier ops):

    /change remove key <discordID|key|ign> [reason]
    /change rotate key <discordID|key|ign> [reason]

Both DM the affected user when possible; a ``reason`` (if supplied) is
included in that DM. Users themselves can no longer self-rotate — they ask
a moderator if they think their key has leaked.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from tortoise.expressions import Q

from bot import Bot
from config import CurrConfig
from lib.auth import is_staff
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
from orm import DiscordAccount, MinecraftAccount, VerifyKey

logger = logging.getLogger("dazebot.cogs.vetsmod")

VETSMOD_MODRINTH_URL = "https://modrinth.com/mod/vetsmod/versions"

VETSMOD_FALLBACK_DM_CUSTOM_ID = "vetsmod:fallback_dm"
VETSMOD_FALLBACK_SHOW_CUSTOM_ID = "vetsmod:fallback_show"

# secrets.token_urlsafe(32) yields 43 chars from [A-Za-z0-9_-]. Allow a
# little tolerance so a future bump in token bytes doesn't silently break
# target resolution.
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{40,50}$")
# Discord snowflakes are 17–20 digits in 2026; pad to 22 for headroom.
_DISC_ID_RE = re.compile(r"^\d{17,22}$")


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
            "If you think it leaked, ask a moderator to rotate it for you."
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


async def _resolve_key_target(value: str) -> Optional[VerifyKey]:
    """Find a VerifyKey row from a Discord ID, the key string, or an MC IGN.

    Order of attempts:
      1. ``value`` looks like a Discord snowflake -> match by ``disc_uuid``
      2. ``value`` looks like a key (URL-safe base64, ~43 chars) -> match by ``key``
      3. Treat ``value`` as an MC IGN -> resolve to a MinecraftAccount, walk
         to its DiscordAccount, then fetch the VerifyKey by that disc_uuid.

    Returns ``None`` if nothing matches at any step. Revoked rows are still
    returned (callers decide what to do with them).
    """
    if _DISC_ID_RE.match(value):
        row = await VerifyKey.filter(disc_uuid=value).first()
        if row is not None:
            return row
    if _KEY_RE.match(value):
        row = await VerifyKey.filter(key=value).first()
        if row is not None:
            return row
    mc = await MinecraftAccount.filter(
        Q(mc_username__iexact=value) | Q(wynn_username__iexact=value)
    ).first()
    if mc is None:
        return None
    disc = await DiscordAccount.filter(minecraft_account=mc).first()
    if disc is None:
        return None
    return await VerifyKey.filter(disc_uuid=disc.disc_uuid).first()


async def _dm_key_change(
    bot: Bot,
    disc_uuid: str,
    *,
    action: str,  # "rotated" or "removed"
    reason: Optional[str],
) -> bool:
    """DM the affected user about a staff key action. Returns True on delivery."""
    try:
        user = bot.get_user(int(disc_uuid)) or await bot.fetch_user(int(disc_uuid))
    except (discord.NotFound, discord.HTTPException, ValueError):
        logger.exception("dm_key_change: user lookup failed disc=%s", disc_uuid)
        return False
    if action == "rotated":
        head = (
            "Your vetsmod key has been **rotated** by staff. The previous "
            "key has stopped working; run `/vetsmod` to receive the fresh one."
        )
    else:
        head = (
            "Your vetsmod key has been **removed** by staff. It has stopped "
            "working; run `/vetsmod` if you want to issue a new one."
        )
    body = head
    if reason:
        body += f"\n\n**Reason:** {reason}"
    body += "\n\nIf you didn't expect this, contact a moderator."
    return await dm_or_log(user, body, fallback_logger=logger)


def _find_member_anywhere(bot: Bot, disc_uuid: str) -> Optional[discord.Member]:
    try:
        uid = int(disc_uuid)
    except ValueError:
        return None
    for guild in bot.guilds:
        m = guild.get_member(uid)
        if m is not None:
            return m
    return None


class Vetsmod(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Vetsmod cog initialized")

    # =================================================================
    # /vetsmod  — self-issue (or re-issue) a key
    # =================================================================

    @commands.hybrid_command(
        name="vetsmod",
        description="Get vetsmod and/or unlock it!",
    )
    async def vetsmod(self, ctx: commands.Context):
        if not isinstance(ctx.author, discord.Member):
            await ctx.reply(
                "This command must be used in a server, not a DM.", ephemeral=True
            )
            return

        await ctx.defer(ephemeral=True)

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

        await ctx.reply(
            "⚠️ Couldn't DM you and the fallback channel is "
            "unavailable. Here's your key, **only visible to you**:\n\n" + body,
            ephemeral=True,
        )

    # =================================================================
    # /change  — staff (mod-tier) operations on user state
    # =================================================================

    @commands.hybrid_group(
        name="change",
        description="(Staff) Mod operations.",
    )
    @is_staff()
    async def change_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply(
                "Use `/change remove key <target>` or `/change rotate key <target>`."
            )

    # ----- /change remove ... -----

    @change_group.group(
        name="remove",
        description="(Staff) Remove things from a user.",
    )
    async def change_remove(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply("Use `/change remove key <target>`.")

    @change_remove.command(
        name="key",
        description="(Staff) Revoke a user's vetsmod key. Target = Discord ID, the key, or MC IGN.",
    )
    @app_commands.describe(
        target="Discord ID, the vetsmod key string, or Minecraft IGN.",
        reason="Optional reason — included in the DM to the affected user.",
    )
    async def change_remove_key(
        self,
        ctx: commands.Context,
        target: str,
        reason: Optional[str] = None,
    ):
        await ctx.defer(ephemeral=True)
        row = await _resolve_key_target(target)
        if row is None:
            await ctx.reply(f"No vetsmod key found for `{target}`.", ephemeral=True)
            return
        if row.revoked_at is not None:
            await ctx.reply(
                f"<@{row.disc_uuid}>'s key was already revoked at "
                f"{row.revoked_at.isoformat()}. No change.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        log_reason = f"staff:{ctx.author.id}" + (f":{reason}" if reason else "")
        await revoke_key(row.disc_uuid, reason=log_reason)
        dmed = await _dm_key_change(
            self.bot, row.disc_uuid, action="removed", reason=reason
        )
        suffix = "" if dmed else " (DM failed — couldn't notify them)."
        await ctx.reply(
            f"✅ Removed vetsmod key for <@{row.disc_uuid}>.{suffix}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ----- /change rotate ... -----

    @change_group.group(
        name="rotate",
        description="(Staff) Rotate things for a user.",
    )
    async def change_rotate(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply("Use `/change rotate key <target>`.")

    @change_rotate.command(
        name="key",
        description="(Staff) Issue a fresh vetsmod key (invalidates the old one).",
    )
    @app_commands.describe(
        target="Discord ID, the vetsmod key string, or Minecraft IGN.",
        reason="Optional reason — included in the DM to the affected user.",
    )
    async def change_rotate_key(
        self,
        ctx: commands.Context,
        target: str,
        reason: Optional[str] = None,
    ):
        await ctx.defer(ephemeral=True)
        row = await _resolve_key_target(target)
        if row is None:
            await ctx.reply(f"No vetsmod key found for `{target}`.", ephemeral=True)
            return
        # rotate_key(member) needs a live Member to refresh the tier snapshot.
        # If the user has left every guild we share, rotating is pointless
        # (they can't /vetsmod to pick up the new key) — tell staff to
        # /change remove key instead.
        member = _find_member_anywhere(self.bot, row.disc_uuid)
        if member is None:
            await ctx.reply(
                f"Can't see <@{row.disc_uuid}> in any guild — they've left, "
                "so rotating won't help (they can't `/vetsmod` to pick up the "
                "new key). Use `/change remove key` instead.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await rotate_key(member)
        logger.info(
            "verify key rotated by staff: target=%s actor=%s reason=%s",
            row.disc_uuid, ctx.author.id, reason or "unspecified",
        )
        dmed = await _dm_key_change(
            self.bot, row.disc_uuid, action="rotated", reason=reason
        )
        suffix = (
            ""
            if dmed
            else " (DM failed — couldn't notify them; they'll need to `/vetsmod` themselves)."
        )
        await ctx.reply(
            f"✅ Rotated vetsmod key for <@{row.disc_uuid}>.{suffix}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: Bot):
    await bot.add_cog(Vetsmod(bot))
    logger.info("Vetsmod cog loaded successfully")

"""Persistent View + Modal for the /first-install onboarding flow.

Posted by ``/first-install`` as a bot message. Anyone can click the button to
get DMed (or, if their DMs are closed, pinged in ``LINK_FALLBACK_CHANNEL``
with a fallback view) a Minecraft link code. Survives bot restarts because
the Views are registered with ``bot.add_view`` and the Buttons have stable
``custom_id``\u2009s.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord

from config import CurrConfig
from lib.linking import dm_or_log, get_or_issue_code
from orm import DMSentLog, DiscordAccount, LinkCode

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger("dazebot.lib.first_install_view")

LINK_BUTTON_CUSTOM_ID = "first_install:link_button"
LINK_MODAL_CUSTOM_ID = "first_install:link_modal"
LINK_MODAL_USERNAME_FIELD = "first_install:username"
FALLBACK_DM_BUTTON_CUSTOM_ID = "first_install:fallback_dm"
FALLBACK_SHOW_BUTTON_CUSTOM_ID = "first_install:fallback_show"

# Mojang username rules: 3-16 chars, alphanumeric + underscore.
_MC_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def _build_dm_body(username: str, code: str) -> str:
    host = getattr(CurrConfig, "MC_PUBLIC_HOST", "(server IP not configured)")
    return (
        f"Hi! Here's your VETS account-link code for **`{username}`**:\n\n"
        f"\u2003**`{code}`**\n\n"
        f"**To finish linking:**\n"
        f"1. Join the Minecraft server at `{host}`.\n"
        f"2. Type the code above into chat.\n"
        f"3. You'll get a confirmation DM from me.\n\n"
        f"_The code stays valid until used. If you re-click the button, you'll get the same code._"
    )


class _LinkModal(discord.ui.Modal):
    """Modal asking the clicker for their Minecraft username."""

    username = discord.ui.TextInput(
        label="Your Minecraft username",
        placeholder="e.g. Notch",
        min_length=3,
        max_length=16,
        custom_id=LINK_MODAL_USERNAME_FIELD,
    )

    def __init__(self):
        super().__init__(title="Link your Minecraft account", custom_id=LINK_MODAL_CUSTOM_ID)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        username = str(self.username.value).strip()

        if not _MC_USERNAME_RE.match(username):
            await interaction.followup.send(
                "\u274c That doesn't look like a valid Minecraft username "
                "(3\u201316 characters, letters/numbers/underscore only). Try again.",
                ephemeral=True,
            )
            return

        # Refuse if this discord user is already linked.
        disc = await DiscordAccount.filter(disc_uuid=str(interaction.user.id)).first()
        if disc is not None and disc.minecraft_account_id is not None:
            await interaction.followup.send(
                "You're already linked to a Minecraft account. "
                "Ask staff to /unlink you first if this is wrong.",
                ephemeral=True,
            )
            return

        row, is_new = await get_or_issue_code(str(interaction.user.id), username)
        body = _build_dm_body(username=username, code=row.code)

        dmed = await dm_or_log(interaction.user, body, fallback_logger=logger)
        if dmed:
            # Record outreach for analytics / DM-throttling.
            try:
                await DMSentLog.get_or_create(
                    disc_uuid=str(interaction.user.id),
                    kind="first_install_link",
                )
            except Exception:  # noqa: BLE001 \u2014 don't break UX over a logging table
                logger.exception("DMSentLog write failed")

            note = (
                "\u2705 Sent your link code to your DMs."
                if is_new
                else "\u2139\ufe0f You already had a code; I re-DMed it to you."
            )
            await interaction.followup.send(note, ephemeral=True)
            return

        # DM closed \u2192 fall back to a public ping in LINK_FALLBACK_CHANNEL with
        # a view giving them buttons to (a) retry the DM, or (b) ephemerally
        # reveal the code+instructions in-channel without leaking it publicly.
        posted = await _post_fallback_ping(
            interaction.client,  # type: ignore[arg-type]
            interaction.user,
            username=username,
        )
        if posted is not None:
            channel_mention = f"<#{posted.channel.id}>"
            await interaction.followup.send(
                "\u26a0\ufe0f I couldn't DM you (your DMs are closed for this server). "
                f"I've pinged you in {channel_mention} \u2014 click one of the buttons "
                "there to either retry the DM or view your code privately.",
                ephemeral=True,
            )
            return

        # Last-ditch fallback: channel post failed too. Reveal the code in the
        # ephemeral reply so the user is never stranded.
        await interaction.followup.send(
            "\u26a0\ufe0f I couldn't DM you and couldn't post in the fallback "
            "channel either. Here's your code, **only visible to you**:\n\n"
            + body,
            ephemeral=True,
        )


class FirstInstallView(discord.ui.View):
    """Persistent view (timeout=None) so the button works after bot restarts.

    Must be registered once via ``bot.add_view(FirstInstallView())`` in
    ``setup_hook``.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Link my Minecraft account",
        style=discord.ButtonStyle.primary,
        emoji="\U0001f517",  # \U0001f517 link emoji
        custom_id=LINK_BUTTON_CUSTOM_ID,
    )
    async def link_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Fast path: already linked? Tell them ephemerally and skip the modal.
        disc = await DiscordAccount.filter(disc_uuid=str(interaction.user.id)).first()
        if disc is not None and disc.minecraft_account_id is not None:
            await interaction.response.send_message(
                "You're already linked to a Minecraft account. \u2705",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(_LinkModal())


def build_welcome_embed(quoted_message: discord.Message | None) -> discord.Embed:
    """Build the bot-posted onboarding embed. If ``quoted_message`` is given,
    include its content (truncated) so the bot's repost feels like a wrapper
    around the staff-authored welcome text.
    """
    embed = discord.Embed(
        title="Welcome to VETS \U0001f44b",
        description=(
            "Click the button below to link your Minecraft account. "
            "I'll DM you a one-time code and the server IP."
        ),
        color=discord.Color.blurple(),
    )
    if quoted_message and quoted_message.content:
        snippet = quoted_message.content
        if len(snippet) > 1500:
            snippet = snippet[:1500] + "\u2026"
        embed.add_field(name="From staff", value=snippet, inline=False)
    embed.set_footer(text="Your code stays valid until used. Re-clicking returns the same code.")
    return embed


# ---------------------------------------------------------------------------
# DM-closed fallback flow
# ---------------------------------------------------------------------------


async def _resolve_pending_code(disc_uuid: str) -> LinkCode | None:
    """Return the (most recent) pending LinkCode for this Discord user, or
    ``None`` if there is no outstanding code (i.e. they never started a link
    or the code has already been consumed).
    """
    return (
        await LinkCode.filter(disc_uuid=disc_uuid)
        .order_by("-updated_at")
        .first()
    )


class LinkFallbackView(discord.ui.View):
    """Persistent view attached to the public ping we post when a user with
    DMs closed clicks the welcome button.

    The buttons are not gated to a specific user (custom_ids must be stable
    for persistence) \u2014 instead each callback resolves *the clicker's own*
    pending LinkCode by ``disc_uuid``. That means any stale fallback message
    still works for the right user, and other users clicking it just get a
    polite \"no pending code\" reply.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Try DM again",
        style=discord.ButtonStyle.primary,
        emoji="\U0001f4ec",  # incoming envelope
        custom_id=FALLBACK_DM_BUTTON_CUSTOM_ID,
    )
    async def retry_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await _resolve_pending_code(str(interaction.user.id))
        if row is None:
            await interaction.followup.send(
                "You don't have a pending link code. Click the main "
                "**Link my Minecraft account** button to start.",
                ephemeral=True,
            )
            return
        body = _build_dm_body(username=row.mc_username, code=row.code)
        dmed = await dm_or_log(interaction.user, body, fallback_logger=logger)
        if dmed:
            try:
                await DMSentLog.get_or_create(
                    disc_uuid=str(interaction.user.id),
                    kind="first_install_link",
                )
            except Exception:  # noqa: BLE001
                logger.exception("DMSentLog write failed")
            await interaction.followup.send(
                "\u2705 Sent your link code to your DMs.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "\u26a0\ufe0f Still couldn't DM you \u2014 your DMs look closed for "
            "this server. Use **Show my code** to see it here privately, or "
            "enable DMs from server members and try again.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Show my code",
        style=discord.ButtonStyle.secondary,
        emoji="\U0001f441\ufe0f",  # eye
        custom_id=FALLBACK_SHOW_BUTTON_CUSTOM_ID,
    )
    async def show_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await _resolve_pending_code(str(interaction.user.id))
        if row is None:
            await interaction.followup.send(
                "You don't have a pending link code. Click the main "
                "**Link my Minecraft account** button to start.",
                ephemeral=True,
            )
            return
        body = _build_dm_body(username=row.mc_username, code=row.code)
        await interaction.followup.send(
            "Here's your link code (only visible to you):\n\n" + body,
            ephemeral=True,
        )


async def _post_fallback_ping(
    bot: Bot, user: discord.abc.User, *, username: str
) -> discord.Message | None:
    """Post the public DM-closed fallback ping in ``LINK_FALLBACK_CHANNEL``.

    Returns the posted message on success, or ``None`` if the channel is
    misconfigured or the post failed.
    """
    channel_id = getattr(CurrConfig, "LINK_FALLBACK_CHANNEL", None)
    if not channel_id:
        logger.warning("LINK_FALLBACK_CHANNEL is not configured; cannot post fallback")
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            logger.exception("fallback channel fetch failed (id=%s)", channel_id)
            return None
    if not isinstance(channel, discord.abc.Messageable):
        logger.error("fallback channel %s is not messageable", channel_id)
        return None
    content = (
        f"<@{user.id}> \u2014 I couldn't DM you your link code for "
        f"**`{username}`** (your DMs are closed). "
        "Use the buttons below to retry the DM, or to view your code "
        "privately here without exposing it publicly."
    )
    try:
        return await channel.send(
            content,
            view=LinkFallbackView(),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user.id)]),
        )
    except discord.HTTPException:
        logger.exception("failed to post fallback ping for %s", user.id)
        return None


async def post_fallback_completion(
    bot: Bot, user: discord.abc.User, *, success: bool, reason: str
) -> None:
    """Public-channel confirmation ping posted after an in-game link attempt
    completes for a user whose original onboarding fell back to the channel
    flow (i.e. their DM also failed when we tried to send the result).
    """
    channel_id = getattr(CurrConfig, "LINK_FALLBACK_CHANNEL", None)
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            logger.exception("fallback channel fetch failed (id=%s)", channel_id)
            return
    if not isinstance(channel, discord.abc.Messageable):
        return
    verb = "\u2705 Link complete" if success else "\u274c Link failed"
    try:
        await channel.send(
            f"<@{user.id}> \u2014 **{verb}.** {reason}",
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user.id)]),
        )
    except discord.HTTPException:
        logger.exception("failed to post fallback completion for %s", user.id)


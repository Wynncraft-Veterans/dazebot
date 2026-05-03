"""Persistent View + Modal for the /first-install onboarding flow.

Posted by ``/first-install`` as a bot message. Anyone can click the button to
get DMed (or, if their DMs are closed, ephemerally shown) a Minecraft link
code. Survives bot restarts because the View is registered with
``bot.add_view`` and the Button has a stable ``custom_id``.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord

from config import CurrConfig
from lib.linking import dm_or_log, get_or_issue_code
from orm import DMSentLog, DiscordAccount

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger("dazebot.lib.first_install_view")

LINK_BUTTON_CUSTOM_ID = "first_install:link_button"
LINK_MODAL_CUSTOM_ID = "first_install:link_modal"
LINK_MODAL_USERNAME_FIELD = "first_install:username"

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

        # DM closed \u2192 fall back to an ephemeral reply containing the code.
        await interaction.followup.send(
            "\u26a0\ufe0f I couldn't DM you (your DMs are closed for this server). "
            "Here's your code, **only visible to you**:\n\n"
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

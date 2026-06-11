"""Week 77: confidential award nomination submissions.

Surfaces:

* ``/return 77`` / ``~return 77`` — the bot replies in-channel with a
  "Submit a nomination" button; the button opens a Modal where the
  clicker types three fields:

  * ``nominee`` — a Discord member (mention / id / username / display
    name) OR a Minecraft username / UUID. Validated against the guild
    member cache and, failing that, the Wynncraft API.
  * ``title`` — short label for the award (<= ``MAX_TITLE_LEN`` chars).
  * ``description`` — free-form justification (<= ``MAX_DESC_LEN`` chars).

  On submit the bot posts an embed into the admin-only thread
  (``NOMINATION_THREAD_ID``). The embed shows the resolved nominee, the
  raw original input, the submitter, and an affiliation label combining
  authority (Operator / Admin / Staff) with membership state (Member /
  Honourary / Hiatus / Waitlisted / Registered) so thread admins can
  weigh the nomination at a glance.

Only the *form content* is private — the modal is rendered only to the
clicker; submissions only reach the admin thread plus a brief ephemeral
ack. The fact that ``/return 77`` was run is not hidden; the bot replies
in-channel with the button. Anyone may click; ``_NominateView`` re-runs
the REGISTERED tier check before opening the modal. No DB persistence —
the thread post is the canonical record.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import discord
from discord.ext import commands

from cogs.events.returns import Tier, register
from cogs.events.returns._common import tier_allows
from config import CurrConfig
from lib.auth import (
    _has_admin_perm,
    _has_staff_role,
    _is_operator,
    _resolve_member,
)
from lib.mc.resolve import ensure_mc_account
from lib.mc.wynn_api.errors import WynnApiError
from orm import MinecraftAccount

logger = logging.getLogger("dazebot.cogs.events.returns.week_77")

WEEK = 77

# Forum thread submissions are funneled into. Admin-only — confidentiality
# of nominations is enforced by the thread's Discord ACLs, not by the bot.
NOMINATION_THREAD_ID = 1514486320851058769

MAX_TITLE_LEN = 127
MAX_DESC_LEN = 2000

# Slightly under the ephemeral interaction token's ~15-minute window.
_VIEW_TIMEOUT = 600

# Discord mention / id matchers. Kept in sync with lib/mc/resolve.py.
_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d+)>$")
_DISCORD_ID_RE = re.compile(r"^\d{15,20}$")


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


async def _resolve_nominee(
    interaction: discord.Interaction, raw: str, guild_id: int
) -> tuple[Optional[discord.Member], Optional[MinecraftAccount], Optional[str]]:
    """Resolve a nominee string to a Discord Member or MinecraftAccount.

    Branches by input shape so the failure message tells the user where the
    lookup actually missed:

      1. Discord mention ``<@id>`` or bare snowflake → guild member lookup
         only. If that fails, do *not* fall through to the Wynncraft API —
         a mention plainly isn't an MC name.
      2. Plain string → guild member name / global name / display name,
         then :func:`ensure_mc_account` (local DB + Wynncraft / Mojang
         fallback).

    Returns ``(member, mc, error)``. On success only one of ``member`` /
    ``mc`` is set. On failure ``error`` carries a user-facing message
    specific to which branch was attempted.
    """
    val = raw.strip()
    if not val:
        return None, None, "Nominee is required — leave nothing blank."

    guild = interaction.client.get_guild(guild_id)
    disc_id: Optional[str] = None
    m = _DISCORD_MENTION_RE.match(val)
    if m:
        disc_id = m.group(1)
    elif _DISCORD_ID_RE.match(val):
        disc_id = val

    if disc_id is not None:
        if guild is None:
            return None, None, (
                "I can't reach this server right now to resolve a Discord ID. "
                "Try again in a moment, or ping an admin."
            )
        member = guild.get_member(int(disc_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(disc_id))
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is None:
            return None, None, (
                f"That looks like a Discord mention/ID, but `{disc_id}` isn't "
                "a member of this server. Try a username or a Minecraft name "
                "instead."
            )
        return member, None, None

    if guild is not None:
        lower = val.lower()
        for cand in guild.members:
            if (
                cand.name.lower() == lower
                or (cand.global_name or "").lower() == lower
                or cand.display_name.lower() == lower
            ):
                return cand, None, None

    try:
        mc = await ensure_mc_account(val)
        return None, mc, None
    except WynnApiError:
        return None, None, (
            f"`{val}` doesn't match any Discord member here or any Wynncraft "
            "player. Check the spelling, or paste a Discord @mention."
        )
    except Exception:  # noqa: BLE001 - network / SDK safety net
        logger.exception(
            "return 77: unexpected error resolving MC nominee %r", val
        )
        return None, None, (
            f"Something went wrong looking up `{val}` (Wynncraft may be "
            "having issues). Try again in a moment."
        )


async def _resolve_thread(
    bot: discord.Client, thread_id: int
) -> Optional[discord.Thread]:
    """Look up a discord.Thread by id, fetching if not cached."""
    ch = bot.get_channel(thread_id)
    if isinstance(ch, discord.Thread):
        return ch
    try:
        ch = await bot.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logger.warning("return 77 thread %s not fetchable: %s", thread_id, e)
        return None
    return ch if isinstance(ch, discord.Thread) else None


def _describe_affiliation(
    user: discord.abc.User, client: Optional[discord.Client]
) -> str:
    """Build the affiliation label shown next to "Submitted by" in the embed.

    Combines two axes the thread admins care about:

    * Authority: highest of Operator / Admin / Staff (or absent).
    * Membership state: highest of Member / Honourary / Hiatus /
      Waitlisted / Registered (or absent).
    """
    parts: list[str] = []
    member = _resolve_member(user, client)

    if _is_operator(user):
        parts.append("Operator")
    elif member is not None and _has_admin_perm(member):
        parts.append("Admin")
    elif member is not None and _has_staff_role(member):
        parts.append("Staff")

    if member is not None:
        role_ids = {r.id for r in member.roles}
        if CurrConfig.ROLE_MEMBER in role_ids:
            parts.append("Member")
        elif CurrConfig.ROLE_HONOURARY in role_ids:
            parts.append("Honourary")
        elif CurrConfig.ROLE_HIATUS in role_ids:
            parts.append("Hiatus")
        elif CurrConfig.ROLE_WAITLISTED in role_ids:
            parts.append("Waitlisted")
        elif CurrConfig.ROLE_REGISTERED in role_ids:
            parts.append("Registered")

    if not parts:
        return "not in the vets server"
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Modal + View
# ---------------------------------------------------------------------------


class _NominateModal(discord.ui.Modal):
    nominee = discord.ui.TextInput(
        label="Nominee (Discord mention/name OR MC username)",
        placeholder="@someone, 123456789012345678, or PlayerName",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=100,
        required=True,
    )
    title_input = discord.ui.TextInput(
        label="Possible Award (Suggested Title)",
        placeholder="Succinctly characterise their positive contributions!",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=MAX_TITLE_LEN,
        required=True,
    )
    description = discord.ui.TextInput(
        label="Description and justification",
        placeholder="Expand on the above as to why this user is particularly deserving of recognition",
        style=discord.TextStyle.paragraph,
        min_length=0,
        max_length=MAX_DESC_LEN,
        required=False,
    )

    def __init__(self, *, guild_id: int):
        super().__init__(title="Award nomination")
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        raw_nominee = str(self.nominee.value).strip()
        title = str(self.title_input.value).strip()
        description = str(self.description.value).strip()

        if not raw_nominee or not title:
            await interaction.followup.send(
                "Nominee and award title are required.", ephemeral=True
            )
            return

        member, mc, err = await _resolve_nominee(
            interaction, raw_nominee, self.guild_id
        )
        if err is not None:
            await interaction.followup.send(err, ephemeral=True)
            return

        thread = await _resolve_thread(interaction.client, NOMINATION_THREAD_ID)
        if thread is None:
            logger.error(
                "return 77: nomination thread %s is not a Thread or not fetchable",
                NOMINATION_THREAD_ID,
            )
            await interaction.followup.send(
                "I couldn't reach the awards thread. Ping wen so he "
                "can check the bot config.",
                ephemeral=True,
            )
            return

        if member is not None:
            nominee_field = f"{member.mention} (`{member}` · `{member.id}`)"
        elif mc is not None:
            mc_name = mc.mc_username or mc.wynn_username or "unknown"
            nominee_field = f"MC: `{mc_name}` (`{mc.uuid}`)"
        else:
            nominee_field = f"`{raw_nominee}`"

        affiliation = _describe_affiliation(interaction.user, interaction.client)

        embed = discord.Embed(
            title="🏆 Award Nomination",
            description=description or "*(no description provided)*",
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Award Title", value=title, inline=False)
        embed.add_field(name="Nominee", value=nominee_field, inline=False)
        embed.add_field(
            name="Original input", value=f"`{raw_nominee}`", inline=False
        )
        embed.add_field(
            name="Submitted by",
            value=(
                f"{interaction.user.mention} "
                f"(`{interaction.user}` · `{interaction.user.id}`) — {affiliation}"
            ),
            inline=False,
        )

        try:
            posted = await thread.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "return 77: failed to post nomination to thread %s",
                NOMINATION_THREAD_ID,
            )
            await interaction.followup.send(
                "I couldn't post your nomination to the awards thread. Try "
                "again in a moment, or ping wen.",
                ephemeral=True,
            )
            return

        logger.info(
            "return 77: nomination posted by %s (%s) -> thread %s message %s (%s)",
            interaction.user,
            interaction.user.id,
            NOMINATION_THREAD_ID,
            posted.id,
            posted.jump_url,
        )
        await interaction.followup.send(
            "✅ Your nomination has been submitted.", ephemeral=True
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        """Catch-all for anything ``on_submit`` doesn't handle explicitly.

        Without this, an unhandled exception causes Discord to show a
        generic "Something went wrong" to the user with no detail and no
        log line in our logs — the exact "partially went through but
        nothing landed in the thread" symptom.
        """
        logger.exception(
            "return 77: unhandled error in modal submit (user=%s %s)",
            interaction.user,
            interaction.user.id,
            exc_info=error,
        )
        msg = (
            "Something went wrong submitting your nomination. Nothing was "
            "posted. Try again — if it keeps failing, ping wen with what "
            "you typed."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class _NominateView(discord.ui.View):
    def __init__(self, *, guild_id: int):
        super().__init__(timeout=_VIEW_TIMEOUT)
        self.guild_id = guild_id
        btn = discord.ui.Button(
            label="📝 Submit a nomination",
            style=discord.ButtonStyle.primary,
        )
        btn.callback = self._on_click
        self.add_item(btn)

    async def _on_click(self, interaction: discord.Interaction) -> None:
        # The bot's reply is public, so anyone can see and click. Re-gate
        # the modal on the same REGISTERED tier the dispatcher enforces.
        if not tier_allows(
            interaction.user, Tier.REGISTERED, client=interaction.client
        ):
            await interaction.response.send_message(
                "You don't have access to `/return 77`.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            _NominateModal(guild_id=self.guild_id)
        )


# ---------------------------------------------------------------------------
# Dispatch entry-point
# ---------------------------------------------------------------------------


@register(77, tier=Tier.REGISTERED)
async def handle(ctx: commands.Context) -> None:
    guild_id = ctx.guild.id if ctx.guild is not None else CurrConfig.GUILD
    view = _NominateView(guild_id=guild_id)
    intro = (
        "🏆 **Return 77 — Award nomination.**\n"
        "Click the button to open the form!\n"
    )
    await ctx.reply(intro, view=view)

"""``~ctp retroactive`` — form for work done before the CTP reward program.

Mirrors the ``~return 77`` shape (button → form → embed-to-thread) so
players have a familiar surface, but inserts a board-suggestion dropdown.
Discord modals can't host selects, so the picker lives on an interim
ephemeral view: pick a board → click "Open form" → modal collects the
long description and hours.

The submission has no DB side effect — it just posts an embed into
``RETROACTIVE_THREAD_ID``. Staff who decide to honour an entry run
``~ctp reward`` manually based on the thread post, picking whichever
board they actually want (the user's pick is a suggestion only).

This module is intentionally not a cog. ``bot._load_cogs`` walks all
files under ``cogs/`` but silently skips those without a ``setup`` entry
point, so ``ctp.py`` just imports :func:`handle_retroactive` and calls it
from the ``@ctp_group.command(name='retroactive')`` method.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from cogs.rewards.ctp.lib import boards as boards_svc
from config import CurrConfig
from lib.auth import (
    _has_admin_perm,
    _has_registered_role,
    _has_staff_role,
    _is_operator,
    _resolve_member,
)

logger = logging.getLogger("dazebot.cogs.rewards.ctp.retroactive")

# Staff-review thread for retroactive submissions. The bot only needs
# send permission here; thread ACLs handle visibility.
RETROACTIVE_THREAD_ID = 1517947970543161344

MAX_DESC_LEN = 2000
# Generous cap that still rejects obvious typos (5-digit hour counts).
MAX_HOURS = 9999.0

# Slightly under the ephemeral interaction token's ~15-minute window.
_VIEW_TIMEOUT = 600

# Sentinel value for the "No suggestion" SelectOption so we can disambiguate
# it from a real board enum at modal-open time.
_BOARD_NA_VALUE = "__NA__"


# ---------------------------------------------------------------------------
# Gate + helpers
# ---------------------------------------------------------------------------


def _is_registered_user(
    user: discord.abc.User, client: Optional[discord.Client]
) -> bool:
    """Sync mirror of :func:`lib.auth.is_registered` for in-view re-gating.

    The bot's reply with the entry button is in-channel and visible to
    anyone, so we re-check the registered tier when the button is clicked
    rather than trusting the command-level decorator to have covered the
    clicker too.
    """
    if _is_operator(user):
        return True
    member = _resolve_member(user, client)
    if member is None:
        return False
    return (
        _has_admin_perm(member)
        or _has_staff_role(member)
        or _has_registered_role(member)
    )


def _describe_affiliation(
    user: discord.abc.User, client: Optional[discord.Client]
) -> str:
    """Authority + membership-state label for the thread embed.

    Duplicates the helper in :mod:`cogs.events.returns.week_77`; both are
    short and the call sites have different import roots. If a third
    copy shows up, promote to ``lib.auth``.
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


async def _resolve_thread(
    bot: discord.Client, thread_id: int
) -> Optional[discord.Thread]:
    ch = bot.get_channel(thread_id)
    if isinstance(ch, discord.Thread):
        return ch
    try:
        ch = await bot.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logger.warning(
            "ctp retroactive thread %s not fetchable: %s", thread_id, e
        )
        return None
    return ch if isinstance(ch, discord.Thread) else None


async def _build_board_options() -> list[discord.SelectOption]:
    """Build the dropdown options from the live CTPBoard catalog.

    Discord caps a Select at 25 options. We reserve one slot for the
    No-suggestion sentinel; the remaining 24 take the first boards in
    ``enum`` order. If the catalog ever grows past 24 boards the suffix
    silently drops — flag this in code review when adding boards.
    """
    boards = await boards_svc.list_all()
    options = [
        discord.SelectOption(label=b.enum, value=b.enum)
        for b in boards[:24]
    ]
    options.append(
        discord.SelectOption(
            label="No suggestion",
            value=_BOARD_NA_VALUE,
            description="Staff will pick a board on review.",
        )
    )
    return options


# ---------------------------------------------------------------------------
# Modal
# ---------------------------------------------------------------------------


class _RetroactiveModal(discord.ui.Modal):
    description = discord.ui.TextInput(
        label="What did you do? (long description)",
        placeholder="Describe the task — what, when, and any links/proofs.",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=MAX_DESC_LEN,
        required=True,
    )
    hours_input = discord.ui.TextInput(
        label="Hours spent (a number, e.g. 3 or 2.5)",
        placeholder="e.g. 3",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=8,
        required=True,
    )

    def __init__(self, *, board_enum: Optional[str]):
        super().__init__(title="Retroactive CTP submission")
        # ``None`` is the explicit "No suggestion" pick from the dropdown.
        self.board_enum = board_enum

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        description = str(self.description.value).strip()
        raw_hours = str(self.hours_input.value).strip()

        try:
            hours = float(raw_hours)
        except ValueError:
            await interaction.followup.send(
                f"`{raw_hours}` isn't a number — give me hours like `3` or `2.5`.",
                ephemeral=True,
            )
            return
        if hours <= 0 or hours > MAX_HOURS:
            await interaction.followup.send(
                f"Hours must be greater than 0 and at most {MAX_HOURS:g}.",
                ephemeral=True,
            )
            return

        thread = await _resolve_thread(interaction.client, RETROACTIVE_THREAD_ID)
        if thread is None:
            logger.error(
                "ctp retroactive: thread %s is not a Thread or not fetchable",
                RETROACTIVE_THREAD_ID,
            )
            await interaction.followup.send(
                "I couldn't reach the retroactive-submissions thread. Ping "
                "wen so he can check the bot config.",
                ephemeral=True,
            )
            return

        affiliation = _describe_affiliation(interaction.user, interaction.client)
        board_field = (
            f"`{self.board_enum}`" if self.board_enum else "*(no suggestion)*"
        )

        embed = discord.Embed(
            title="⏪ Retroactive CTP submission",
            description=description,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Hours spent", value=f"{hours:g}", inline=True)
        embed.add_field(name="Suggested board", value=board_field, inline=True)
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
                "ctp retroactive: failed to post submission to thread %s",
                RETROACTIVE_THREAD_ID,
            )
            await interaction.followup.send(
                "I couldn't post your submission to the retroactive thread. "
                "Try again in a moment, or ping wen.",
                ephemeral=True,
            )
            return

        logger.info(
            "ctp retroactive: submission posted by %s (%s) -> thread %s message %s (%s)",
            interaction.user,
            interaction.user.id,
            RETROACTIVE_THREAD_ID,
            posted.id,
            posted.jump_url,
        )
        await interaction.followup.send(
            "✅ Your retroactive submission has been recorded.", ephemeral=True
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        """Mirror ``week_77._NominateModal.on_error`` — without this, an
        unhandled exception surfaces Discord's generic "Something went
        wrong" with no log line.
        """
        logger.exception(
            "ctp retroactive: unhandled error in modal submit (user=%s %s)",
            interaction.user,
            interaction.user.id,
            exc_info=error,
        )
        msg = (
            "Something went wrong submitting your retroactive entry. Nothing "
            "was posted. Try again — if it keeps failing, ping wen with what "
            "you typed."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Two-step picker: board Select then "Open form" button
# ---------------------------------------------------------------------------


class _BoardSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Suggest a board (or pick 'No suggestion')",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BoardThenFormView" = self.view  # type: ignore[assignment]
        picked = self.values[0]
        view.selected_board = None if picked == _BOARD_NA_VALUE else picked
        view.board_chosen = True
        # Defer with no message change — the Discord client keeps the
        # picked option rendered in the select, so the user can move on
        # to clicking "Open form".
        await interaction.response.defer()


class _OpenFormButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="📝 Open form",
            style=discord.ButtonStyle.primary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BoardThenFormView" = self.view  # type: ignore[assignment]
        if not view.board_chosen:
            await interaction.response.send_message(
                "Pick a board (or 'No suggestion') from the dropdown first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            _RetroactiveModal(board_enum=view.selected_board)
        )


class _BoardThenFormView(discord.ui.View):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(timeout=_VIEW_TIMEOUT)
        self.selected_board: Optional[str] = None
        self.board_chosen: bool = False
        self.add_item(_BoardSelect(options))
        self.add_item(_OpenFormButton())


# ---------------------------------------------------------------------------
# Public launch view + cog entry point
# ---------------------------------------------------------------------------


class _LaunchView(discord.ui.View):
    """The button posted to the channel by ``~ctp retroactive``.

    Anyone who can see the message can click; the callback re-runs the
    same registered-tier predicate as the command-level decorator, so
    bystanders in lower tiers get an ephemeral refusal instead of
    triggering the picker.
    """

    def __init__(self):
        super().__init__(timeout=_VIEW_TIMEOUT)

    @discord.ui.button(
        label="📝 Submit a retroactive entry",
        style=discord.ButtonStyle.primary,
    )
    async def open_picker(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,  # noqa: ARG002 - discord.py callback signature
    ) -> None:
        if not _is_registered_user(interaction.user, interaction.client):
            await interaction.response.send_message(
                "You don't have access to `~ctp retroactive`.", ephemeral=True
            )
            return
        options = await _build_board_options()
        # The "No suggestion" sentinel is always present, so a 1-option
        # list means zero boards exist — surface that explicitly.
        if len(options) <= 1:
            await interaction.response.send_message(
                "No CTP boards are configured yet — ping an admin.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            (
                "Pick the board you think best fits your task (or 'No "
                "suggestion'), then click **Open form** to fill in the "
                "description and hours."
            ),
            view=_BoardThenFormView(options),
            ephemeral=True,
        )


async def handle_retroactive(ctx: commands.Context) -> None:
    """Reply to ``~ctp retroactive`` with the launch button.

    Called from ``CTPCog.ctp_retroactive``; the command-level
    :func:`is_registered` decorator gates the invocation, and the button
    callback re-gates the clicker.
    """
    intro = (
        "⏪ **Retroactive CTP submission.**\n"
        "Use this if you did work earlier in 2026, before the CTP reward "
        "program was launched. Click the button to start — you'll pick a "
        "board, then fill in a description and the hours spent.\n"
    )
    await ctx.reply(intro, view=_LaunchView())

"""Week 75: emerald-price guessing.

Each day of the 7-day event, players submit a single integer guess (in raw
emeralds) for the day's price. Guesses must stay private — anyone seeing
another player's number defeats the point of the event, so every surface
that carries guess data routes ephemeral / DM (never in-channel).

Surfaces:

* ``/return 75``                  — ephemeral reply with a calculator link,
                                    a Day select (1-7), and the caller's
                                    existing guesses with each day's
                                    edit-window status. Picking a day pops
                                    up a Modal pre-filled with the current
                                    guess (if any).
* ``~manage_return 75 view <N>``  — STAFF: paginated listing of every
                                    guess for day ``N`` (1-7), sorted
                                    smallest to largest, each formatted as
                                    ``<username>: <stx/le/eb/e>``.

Guesses are editable for :data:`_EDIT_WINDOW` after the original
submission — the window is anchored on ``created_at`` and does **not**
slide on edits. After the window expires the guess is locked and only
staff can clear it. The unique ``(week, day, discord_account)`` constraint
on ``ReturnGuess`` is still respected because edits are ``UPDATE`` not
``INSERT``.

The emerald denomination conversion matches WynnVentory's emerald
calculator (``modules/routes/web/templates/emerald_calculator.html``):
1 stx = 64 le, 1 le = 64 eb, 1 eb = 64 e (so 1 stx = 262,144 e).
:func:`_format_emeralds` is the canonical formatter and reproduces the
calculator's display logic exactly.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands
from tortoise.exceptions import IntegrityError

from cogs.events.returns import Tier, register, register_manage
from cogs.events.returns._common import is_persist_context, send_feedback
from lib.discord_utils.paginated_embed import Paginator, from_lines
from orm import DiscordAccount, MinecraftAccount, ReturnGuess

logger = logging.getLogger("dazebot.cogs.events.returns.week_75")

WEEK = 75
DAYS = 7
EMERALD_CALCULATOR_URL = "https://www.wynnventory.com/emerald_calculator"

# Wynncraft denominations — matches WynnVentory's emerald calculator.
_E_PER_LE = 64
_E_PER_EB = 64 * 64  # 4,096
_E_PER_STX = 64 * 64 * 64  # 262,144

# How long after the original submission a guess can still be edited.
# Anchored on ``ReturnGuess.created_at`` and does not slide on edits.
_EDIT_WINDOW = timedelta(minutes=60)
_EDIT_WINDOW_MIN = int(_EDIT_WINDOW.total_seconds() // 60)

# Discord's embed body cap is 4096 chars; 15 lines/page leaves plenty of
# headroom even with long MC usernames.
_LINES_PER_PAGE = 15

# Modal text inputs are always strings. Cap at this many chars to keep
# overflow / non-numeric input cases small.
_PRICE_MAX_LEN = 12


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_emeralds(n: int) -> str:
    """Render ``n`` raw emeralds in WynnVentory's display format.

    Mirrors the JS in WynnVentory_Web's
    ``modules/routes/web/templates/emerald_calculator.html``:

    * If ``stx > 0``: render as ``"{stx}stx {decimal_le}le"`` where the
      decimal le combines the remainder under one stx as ``le + eb/64 +
      e/4096`` rounded to two decimals. Trailing zeros are stripped, so
      an exact stx renders as just ``"{stx}stx"``.
    * Otherwise: render as ``"{le}le {eb}eb {e}e"`` with zero-valued
      components omitted. ``n == 0`` renders as ``"0e"`` so the result
      is always non-empty.

    Examples (from the calculator):

    >>> _format_emeralds(1_000_000)
    '3stx 52.14le'
    >>> _format_emeralds(262_144)
    '1stx'
    >>> _format_emeralds(4_096)
    '1le'
    >>> _format_emeralds(200)
    '3eb 8e'
    >>> _format_emeralds(0)
    '0e'
    """
    if n < 0:
        return f"-{_format_emeralds(-n)}"

    stx, rem = divmod(n, _E_PER_STX)
    le, rem = divmod(rem, _E_PER_EB)
    eb, e = divmod(rem, _E_PER_LE)

    if stx > 0:
        # Match JS Math.round (half-up) rather than Python's banker's
        # rounding — otherwise 1stx 8eb (dec=0.125) would render as
        # 0.12le here but 0.13le in the calculator.
        dec = math.floor((le + eb / 64 + e / 4096) * 100 + 0.5) / 100
        if dec == 0:
            return f"{stx}stx"
        if dec.is_integer():
            return f"{stx}stx {int(dec)}le"
        # Two-decimal format with trailing zeros stripped (52.10 → 52.1,
        # 52.00 won't reach here because of the int branch above).
        dec_str = f"{dec:.2f}".rstrip("0").rstrip(".")
        return f"{stx}stx {dec_str}le"

    parts: list[str] = []
    if le:
        parts.append(f"{le}le")
    if eb:
        parts.append(f"{eb}eb")
    if e:
        parts.append(f"{e}e")
    return " ".join(parts) or "0e"


# ---------------------------------------------------------------------------
# Private send (slash → ephemeral, prefix → DM)
# ---------------------------------------------------------------------------


async def _private(
    ctx: commands.Context,
    content: Optional[str] = None,
    *,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
) -> None:
    """Send a response only the invoker should see.

    Mirrors ``week_73._private``: slash invocations get an ephemeral reply;
    prefix invocations (``~manage_return``) DM the user and delete the
    invoking command message. Falls back to a non-revealing channel
    message if DMs are closed.
    """
    kw: dict = {}
    if content is not None:
        kw["content"] = content
    if embed is not None:
        kw["embed"] = embed
    if view is not None:
        kw["view"] = view

    if ctx.interaction is not None:
        await ctx.reply(ephemeral=True, **kw)
        return

    try:
        await ctx.author.send(**kw)
    except discord.Forbidden:
        await ctx.reply(
            "I couldn't DM you. Open your DMs — posting this here would "
            "expose other players' guesses.",
            mention_author=False,
        )
        return
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound, AttributeError):
        pass


# ---------------------------------------------------------------------------
# User-facing UI: Select(day) → Modal(price)
# ---------------------------------------------------------------------------


def _edit_remaining(guess: ReturnGuess, *, now: Optional[datetime] = None) -> timedelta:
    """How long is left in this guess's edit window. Negative once locked."""
    if now is None:
        now = datetime.now(timezone.utc)
    return _EDIT_WINDOW - (now - guess.created_at)


def _editable(guess: ReturnGuess, *, now: Optional[datetime] = None) -> bool:
    return _edit_remaining(guess, now=now) > timedelta(0)


def _humanize_remaining(remaining: timedelta) -> str:
    """Whole-minute round-up so a 30-second remainder still reads ``1 min``."""
    secs = max(0, int(remaining.total_seconds()))
    return f"{(secs + 59) // 60} min"


class _PriceModal(discord.ui.Modal):
    """One-field modal. The day was picked in the Select that opened this.

    Pre-fills with the current guess (if any) so the user sees what they
    submitted and only has to edit the digits they want to change.
    """

    def __init__(self, *, day: int, prefill: Optional[int] = None):
        super().__init__(title=f"Return 75 — Day {day} guess")
        self.day = day
        self.price: discord.ui.TextInput = discord.ui.TextInput(
            label="Your guess (in emeralds)",
            placeholder="e.g. 262144 for 1stx, or 200 for 3eb 8e",
            min_length=1,
            max_length=_PRICE_MAX_LEN,
            required=True,
            default=str(prefill) if prefill is not None else None,
        )
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.price.value).strip().replace(",", "").replace("_", "")
        try:
            value = int(raw)
        except ValueError:
            await interaction.response.send_message(
                f"`{raw}` isn't a whole number of emeralds. Use the "
                f"calculator at <{EMERALD_CALCULATOR_URL}> to convert.",
                ephemeral=True,
            )
            return
        if value < 0:
            await interaction.response.send_message(
                "Your guess can't be negative.", ephemeral=True
            )
            return

        disc, _ = await DiscordAccount.get_or_create(
            disc_uuid=str(interaction.user.id)
        )
        existing = await ReturnGuess.filter(
            week=WEEK, day=self.day, discord_account=disc
        ).first()

        if existing is None:
            try:
                await ReturnGuess.create(
                    week=WEEK, day=self.day, discord_account=disc, price=value
                )
            except IntegrityError:
                # Race: another submit landed between the .first() and the
                # .create(). Treat as "couldn't save" rather than leaking
                # whatever the other write was.
                await interaction.response.send_message(
                    f"Couldn't save your day {self.day} guess — please try again.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"✅ Saved your day {self.day} guess: {_format_emeralds(value)}. "
                f"Editable for {_EDIT_WINDOW_MIN} min.",
                ephemeral=True,
            )
            return

        # Re-check the window here too — the user may have left the modal
        # open past the cutoff after opening it.
        remaining = _edit_remaining(existing)
        if remaining <= timedelta(0):
            await interaction.response.send_message(
                f"Day {self.day} is locked at {_format_emeralds(existing.price)}. "
                f"Guesses can only be edited within {_EDIT_WINDOW_MIN} min of "
                "submission — ask staff if you need it cleared.",
                ephemeral=True,
            )
            return

        if existing.price == value:
            await interaction.response.send_message(
                f"Day {self.day} is already set to {_format_emeralds(value)}. "
                f"Editable for {_humanize_remaining(remaining)} more.",
                ephemeral=True,
            )
            return

        old_price = existing.price
        existing.price = value
        await existing.save(update_fields=["price"])
        await interaction.response.send_message(
            f"✏️ Updated your day {self.day} guess: "
            f"{_format_emeralds(old_price)} → {_format_emeralds(value)}. "
            f"Editable for {_humanize_remaining(remaining)} more.",
            ephemeral=True,
        )


class _DaySelect(discord.ui.Select):
    """7-option day picker.

    Each option's description reflects the day's current state ("not yet
    submitted" / "submitted; editable Nm" / "submitted; locked"). Picking
    an unsubmitted or editable day opens the price modal (pre-filled when
    editing). Picking a locked day sends back the recorded price plus the
    "ask staff" copy — gives users a way to *check* their guesses even
    after the window closes.
    """

    def __init__(self, *, existing: dict[int, ReturnGuess]):
        now = datetime.now(timezone.utc)
        options: list[discord.SelectOption] = []
        for d in range(1, DAYS + 1):
            g = existing.get(d)
            if g is None:
                desc = "not yet submitted"
            else:
                remaining = _edit_remaining(g, now=now)
                if remaining > timedelta(0):
                    desc = f"submitted; editable {_humanize_remaining(remaining)}"
                else:
                    desc = "submitted; locked"
            options.append(
                discord.SelectOption(label=f"Day {d}", value=str(d), description=desc)
            )
        super().__init__(
            placeholder="Pick a day (1-7) to submit, view, or change",
            min_values=1,
            max_values=1,
            options=options,
        )
        self._existing = existing

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            day = int(self.values[0])
        except ValueError:
            await interaction.response.send_message(
                "Invalid day.", ephemeral=True
            )
            return

        existing = self._existing.get(day)
        if existing is None:
            await interaction.response.send_modal(_PriceModal(day=day, prefill=None))
            return

        remaining = _edit_remaining(existing)
        if remaining <= timedelta(0):
            await interaction.response.send_message(
                f"Day {day} is locked at {_format_emeralds(existing.price)}. "
                f"Edits are only allowed within {_EDIT_WINDOW_MIN} min of "
                "submission — ask staff if you need it cleared.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            _PriceModal(day=day, prefill=existing.price)
        )


def _build_guess_view(existing: dict[int, ReturnGuess]) -> discord.ui.View:
    view = discord.ui.View(timeout=600)
    view.add_item(_DaySelect(existing=existing))
    return view


# ---------------------------------------------------------------------------
# /return 75 handler
# ---------------------------------------------------------------------------


async def _existing_guesses(user_id: int) -> dict[int, ReturnGuess]:
    """All of ``user_id``'s week-75 guesses, keyed by day (1-7)."""
    disc = await DiscordAccount.filter(disc_uuid=str(user_id)).first()
    if disc is None:
        return {}
    rows = await ReturnGuess.filter(week=WEEK, discord_account=disc)
    return {r.day: r for r in rows}


@register(75)
async def handle(ctx: commands.Context) -> None:
    existing = await _existing_guesses(ctx.author.id)

    lines: list[str] = [
        "**Return 75 — emerald-price guessing**",
        (
            f"Pick a day (1-7) and enter your guess **in emeralds**. "
            f"Use the calculator at <{EMERALD_CALCULATOR_URL}> if you "
            "have a stack/le value to convert."
        ),
    ]

    if existing:
        now = datetime.now(timezone.utc)
        lines.append(
            f"**Your guesses** (editable for {_EDIT_WINDOW_MIN} min after submitting):"
        )
        for d in range(1, DAYS + 1):
            g = existing.get(d)
            if g is None:
                continue
            remaining = _edit_remaining(g, now=now)
            if remaining > timedelta(0):
                status = f"editable for {_humanize_remaining(remaining)} more"
            else:
                status = "locked"
            lines.append(f"  • Day {d}: {_format_emeralds(g.price)} ({status})")
    else:
        lines.append("You haven't submitted any guesses yet.")

    lines.append("_Guesses are private. Only staff can see the full list._")

    # Critical: must route through _private so prefix invocations (~return 75)
    # don't leak the listing. ``ctx.reply(ephemeral=True)`` is silently a
    # no-op for non-interaction contexts and would dump every guess into the
    # channel.
    await _private(ctx, "\n".join(lines), view=_build_guess_view(existing))


# ---------------------------------------------------------------------------
# Staff listing helper + ~manage_return 75 view <day>
# ---------------------------------------------------------------------------


async def _username_for(bot, disc: DiscordAccount) -> str:
    if disc.minecraft_account_id is not None:
        try:
            mc = await MinecraftAccount.get(id=disc.minecraft_account_id)
            return mc.mc_username
        except Exception:
            logger.exception(
                "MinecraftAccount lookup failed for disc %s", disc.disc_uuid
            )
    try:
        user = await bot.fetch_user(int(disc.disc_uuid))
        return f"@{user.name} (unlinked)"
    except (ValueError, discord.HTTPException):
        return f"<{disc.disc_uuid}> (unlinked)"


async def _show_day(ctx: commands.Context, day: int) -> None:
    if ctx.interaction is not None:
        await ctx.defer(ephemeral=True)

    guesses = (
        await ReturnGuess.filter(week=WEEK, day=day)
        .order_by("price")
        .prefetch_related("discord_account")
    )
    if not guesses:
        await _private(ctx, f"No guesses recorded for Return 75 day {day}.")
        return

    lines: list[str] = []
    for g in guesses:
        name = await _username_for(ctx.bot, g.discord_account)
        lines.append(f"{name}: {_format_emeralds(g.price)}")

    title = f"Return 75 — Day {day} ({len(guesses)} guess(es))"
    embeds = from_lines(title, lines, _LINES_PER_PAGE, logger)
    if not embeds:
        await _private(ctx, f"No guesses recorded for Return 75 day {day}.")
        return
    if len(embeds) == 1:
        await _private(ctx, embed=embeds[0])
    else:
        await _private(ctx, embed=embeds[0], view=Paginator(embeds))


@register_manage(
    75, "view", tier=Tier.STAFF,
    help="List every guess for day <N> (1-7), sorted smallest to largest.",
    usage="<day>",
)
async def _manage_view(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if not args:
        await send_feedback(
            ctx, "Usage: `~manage_return 75 view <day>`", persist=persist
        )
        return
    try:
        day = int(args[0])
    except ValueError:
        await send_feedback(
            ctx, f"`{args[0]}` isn't a valid day. Use 1-7.", persist=persist
        )
        return
    if not 1 <= day <= DAYS:
        await send_feedback(
            ctx, f"Day must be between 1 and {DAYS}; got {day}.", persist=persist
        )
        return
    await _show_day(ctx, day)

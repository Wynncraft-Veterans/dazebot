"""Week 75: emerald-price guessing.

Each day of the 7-day event, players submit a single integer guess (in raw
emeralds) for the day's price. Guesses must stay private — anyone seeing
another player's number defeats the point of the event, so every surface
that carries guess data routes ephemeral / DM (never in-channel).

Surfaces:

* ``/return 75``                  — ephemeral reply with a calculator link,
                                    a Day select (1-7), and the days the
                                    caller has already submitted. Picking
                                    a day pops up a Modal for the price.
* ``~manage_return 75 view <N>``  — STAFF: paginated listing of every
                                    guess for day ``N`` (1-7), sorted
                                    smallest to largest, each formatted as
                                    ``<username>: `x`stx, `y`le, `z`e``.

Re-submission for the same day is **rejected** by the unique
``(week, day, discord_account)`` constraint on ``ReturnGuess`` — players
who want to amend a guess must ask staff to clear it.

The emerald denomination conversion follows Wynncraft's standard:
1 stx = 64 le = 4096 e. ``_format_emeralds`` is the canonical formatter.
"""

from __future__ import annotations

import logging
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

# Wynncraft denominations.
_E_PER_LE = 64
_E_PER_STX = 64 * 64  # 4096

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
    """Render ``n`` raw emeralds as the Wynncraft three-denomination string.

    Format is ```x`stx, `y`le, `z`e`` (the
    numeric components are backtick-wrapped so Discord renders each as
    inline code). Zero-valued components are omitted; ``n == 0`` renders
    as ```0`e`` so the result is always non-empty.
    """
    stx, rem = divmod(n, _E_PER_STX)
    le, e = divmod(rem, _E_PER_LE)
    parts: list[str] = []
    if stx:
        parts.append(f"`{stx}`stx")
    if le:
        parts.append(f"`{le}`le")
    if e or not parts:
        parts.append(f"`{e}`e")
    return ", ".join(parts)


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


class _PriceModal(discord.ui.Modal):
    """One-field modal. The day was picked in the Select that opened this."""

    price = discord.ui.TextInput(
        label="Your guess (in emeralds)",
        placeholder="e.g. 4096 for 1stx, or 130 for 2le 2e",
        min_length=1,
        max_length=_PRICE_MAX_LEN,
        required=True,
    )

    def __init__(self, *, day: int):
        super().__init__(title=f"Return 75 — Day {day} guess")
        self.day = day

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
        try:
            await ReturnGuess.create(
                week=WEEK, day=self.day, discord_account=disc, price=value
            )
        except IntegrityError:
            await interaction.response.send_message(
                f"You already submitted a guess for day {self.day}. "
                "Ask staff if you need it cleared.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ Saved your day {self.day} guess: {_format_emeralds(value)}.",
            ephemeral=True,
        )


class _DaySelect(discord.ui.Select):
    """7-option day picker. Each option opens the price modal for that day."""

    def __init__(self, *, already: set[int]):
        options = [
            discord.SelectOption(
                label=f"Day {d}",
                value=str(d),
                description=("already submitted" if d in already else None),
                default=False,
            )
            for d in range(1, DAYS + 1)
        ]
        super().__init__(
            placeholder="Pick a day to guess (1-7)",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            day = int(self.values[0])
        except ValueError:
            await interaction.response.send_message(
                "Invalid day.", ephemeral=True
            )
            return
        await interaction.response.send_modal(_PriceModal(day=day))


def _build_guess_view(already: set[int]) -> discord.ui.View:
    view = discord.ui.View(timeout=600)
    view.add_item(_DaySelect(already=already))
    return view


# ---------------------------------------------------------------------------
# /return 75 handler
# ---------------------------------------------------------------------------


async def _already_submitted(user_id: int) -> set[int]:
    """Days the caller has already guessed for, as a set of ints in [1, 7]."""
    disc = await DiscordAccount.filter(disc_uuid=str(user_id)).first()
    if disc is None:
        return set()
    rows = await ReturnGuess.filter(week=WEEK, discord_account=disc).values("day")
    return {r["day"] for r in rows}


@register(75)
async def handle(ctx: commands.Context) -> None:
    already = await _already_submitted(ctx.author.id)
    already_line = (
        f"You've already submitted for: {', '.join(str(d) for d in sorted(already))}."
        if already
        else "You haven't submitted any guesses yet."
    )
    body = "\n".join(
        [
            "**Return 75 — emerald-price guessing**",
            (
                f"Pick a day (1-7) and enter your guess **in emeralds**. "
                f"Use the calculator at <{EMERALD_CALCULATOR_URL}> if you "
                "have a stack/le value to convert."
            ),
            already_line,
            "_Guesses are private. Only staff can see the full list._",
        ]
    )
    await ctx.reply(body, view=_build_guess_view(already), ephemeral=True)


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

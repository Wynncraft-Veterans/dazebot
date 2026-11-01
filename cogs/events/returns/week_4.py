"""Return 4: the daily sudoku. A permanent module, not a past event.

One puzzle a day, played inside Minecraft through vetsmod, with results posted
to Discord. dazebot owns the puzzle and the results; temporary-server only
carries traffic, because vetsmod may not reach dazebot:

* at the 04:00 Halifax rollover the tick runs HoDoKu and stores a
  :class:`orm.SudokuDay`,
* ``api/main.py`` serves it to temporary-server's poller and records solves
  through :func:`today` and :func:`submit`,
* on even Halifax hours the tick posts everyone who solved since the last such
  post, naming them without a ping unless they asked for one,
* from 05:00 the tick posts the previous day's final results, once the new
  puzzle exists, pinging everyone listed unless they opted out, and sets
  ``rollup_done``, which closes that day,
* the button on each post opens a player's own ping settings for those two posts
  and for a reminder before a streak expires, which is off by default.

A day takes submissions until then, so the final post really is final. Only the
tick posts, so a client retry cannot double-post. Each post is sent, then saved:
a crash between the two can cause a rare duplicate, which is accepted.

There is no ``/return 4``: everything happens in game and in the tick, bar two
staff commands: ``~manage_return 4 void`` swaps a bad puzzle for a new one, and
``~manage_return 4 difficulty`` forces the next puzzle's level. The tables are in ``orm.py`` because returns.db has a documented wipe that would
erase the streaks. ``SUDOKU_CHANNEL`` is read each tick, and 0 stops the posts
while the puzzle keeps running.

Env: ``HODOKU_JAVA`` and ``HODOKU_JAR``, set by the vets-deploy Dockerfile.
Without them generation logs and retries, which is the normal state of dev.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Optional

import aiohttp
import discord
from discord.ext import commands
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from cogs.events.returns import Tier, register_manage, register_tick
from cogs.events.returns._common import is_persist_context, send_feedback
from cogs.events.returns.lib import sudoku
from cogs.events.returns.lib.sudoku_cards import Card, render_cards
from config import CurrConfig
from lib.staff.verify_keys import _find_member
from orm import SudokuDay, SudokuNotification, SudokuResult

logger = logging.getLogger("dazebot.cogs.events.returns.week_4")

WEEK = 4

# The most players one post lists, which keeps its text under Discord's 2000
# characters. A longer post would fail on every retry and hold the day open.
LIST_CAP = 50
ALL_TIME_TOP_N = 5
GENERATE_RETRY_SECONDS = 600

REMINDER_HOURS = (1, 2, 3, 4, 6, 8, 12, 18)
REMINDER_STREAKS = (1, 2, 3, 5, 7, 10, 14, 21, 30, 50, 100)
MENU_SECONDS = 600

_PLAY_NOTE = "Play it with `/wv extras sudoku` in game."
_FINAL_NOTE = "Today's puzzle is up: `/wv extras sudoku` in game."

_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Held across "is this day open, then insert" and across "close this day, then
# read its results", so a solve can't land after the final post read the table.
# Database work only, never Discord, because temporary-server is waiting.
_lock = asyncio.Lock()
_closing: set[date] = set()
_retry_at = 0.0


class SubmitRejected(Exception):
    """A submission the API answers with ``status`` rather than a result."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def _ranked(day: date) -> list[SudokuResult]:
    return await SudokuResult.filter(day=day).order_by("seconds", "finished_at", "id")


async def _puzzle_days() -> list[date]:
    return await SudokuDay.all().values_list("day", flat=True)


async def _personal_streak(disc_uuid: str, day: date, puzzle_days: list[date]) -> int:
    solved = await SudokuResult.filter(disc_uuid=disc_uuid).values_list("day", flat=True)
    return sudoku.streak(puzzle_days, set(solved), day)


async def _group_streak(day: date, puzzle_days: list[date]) -> int:
    solved = await SudokuResult.all().distinct().values_list("day", flat=True)
    return sudoku.streak(puzzle_days, set(solved), day)


async def _all_time_top(upto: date) -> list[tuple[str, int]]:
    """Top players by score over days up to ``upto``. A later day is left out
    because its ranks can still change."""
    difficulties = dict(await SudokuDay.filter(day__lte=upto).values_list("day", "difficulty"))
    ranked = await SudokuResult.filter(day__lte=upto).order_by(
        "day", "seconds", "finished_at", "id"
    ).values_list("day", "disc_uuid")
    return sudoku.all_time_scores(ranked, difficulties).most_common(ALL_TIME_TOP_N)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def today() -> Optional[dict]:
    """The newest puzzle, or None if there is none yet. Until a new row exists,
    this keeps serving the last one."""
    row = await SudokuDay.all().order_by("-day").first()
    if row is None:
        return None
    return {"day": row.day.isoformat(), "givens": row.givens, "difficulty": row.difficulty}


def _parse_day(raw: object) -> Optional[date]:
    if not isinstance(raw, str) or _DAY_RE.fullmatch(raw) is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


async def submit(bot: discord.Client, body: dict) -> dict:
    """Record one solve. Raises :class:`SubmitRejected` for anything but a result.

    A repeat submission returns the stored result with ``duplicate`` true.
    ``seconds`` comes from ``fills`` and never from the client directly.
    """
    disc_uuid = body.get("disc_uuid")
    day = _parse_day(body.get("day"))
    if not isinstance(disc_uuid, str) or not (disc_uuid.isascii() and disc_uuid.isdigit()) or day is None:
        raise SubmitRejected(400, "malformed submission")
    if _find_member(bot, int(disc_uuid)) is None:
        raise SubmitRejected(403, "not a member of the server")

    grid, fills = body.get("grid"), body.get("fills")
    async with _lock:
        row = await SudokuDay.get_or_none(day=day)
        if row is None or row.rollup_done or day in _closing:
            raise SubmitRejected(410, "that day is closed or unknown")
        if not sudoku.is_solution(row.givens, grid):
            raise SubmitRejected(400, "wrong board")
        if not sudoku.fills_valid(row.givens, fills):
            raise SubmitRejected(400, "malformed fills")
        # Let the unique constraint decide, so a racing insert can't overwrite.
        duplicate = False
        try:
            result = await SudokuResult.create(
                day=day, disc_uuid=disc_uuid, seconds=max(fills), fills=fills, finished_at=_now()
            )
        except IntegrityError:
            duplicate = True
            result = await SudokuResult.get(day=day, disc_uuid=disc_uuid)

    ranked = await _ranked(day)
    rank = next((i for i, r in enumerate(ranked, start=1) if r.disc_uuid == disc_uuid), None)
    if rank is None:  # a void wiped it since the insert
        raise SubmitRejected(410, "that puzzle was replaced")
    return {
        "status": "ok",
        "seconds": result.seconds,
        "rank": rank,
        "streak": await _personal_streak(disc_uuid, day, await _puzzle_days()),
        "duplicate": duplicate,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _difficulty_line(difficulty: str) -> str:
    label = difficulty.capitalize()
    multiplier = sudoku.SCORE_MULTIPLIERS[difficulty]
    return f"Difficulty: {label} (x{multiplier} score)" if multiplier > 1 else f"Difficulty: {label}"


def _others(count: int) -> str:
    return f"{count} other{'' if count == 1 else 's'}"


def _playing_text(disc_uuids: list[str]) -> str:
    """``@a, @b, and @c were playing``, with anyone past the cap as a count."""
    names = [f"<@{disc_uuid}>" for disc_uuid in disc_uuids[:LIST_CAP]]
    if len(disc_uuids) > LIST_CAP:
        names.append(_others(len(disc_uuids) - LIST_CAP))
    if len(names) == 1:
        who = f"{names[0]} was"
    elif len(names) == 2:
        who = f"{names[0]} and {names[1]} were"
    else:
        who = f"{', '.join(names[:-1])}, and {names[-1]} were"
    return f"{who} playing.\n{_PLAY_NOTE}"


def _final_text(
    row: SudokuDay,
    ranked: list[SudokuResult],
    group_streak: int,
    top: list[tuple[str, int]],
    heading: str,
) -> str:
    """The day's results. ``ranked`` must not be empty."""
    first = ranked[0]
    lines = [
        f"**{heading}**",
        f"Your group is on a {group_streak} day streak!",
        _difficulty_line(row.difficulty),
        f"👑 <@{first.disc_uuid}> won with {sudoku.format_time(first.seconds)}",
    ]
    lines += [f"{sudoku.format_time(r.seconds)} <@{r.disc_uuid}>" for r in ranked[:LIST_CAP]]
    if len(ranked) > LIST_CAP:
        lines.append(f"and {_others(len(ranked) - LIST_CAP)}")
    lines.append("**All-time top 5**")
    lines += [f"{i}. <@{disc_uuid}> {points}" for i, (disc_uuid, points) in enumerate(top, start=1)]
    lines.append(_FINAL_NOTE)
    return "\n".join(lines)


def _empty_final_text(prior_streak: int) -> str:
    """A day nobody solved: the note, plus a line if that ended a streak."""
    if prior_streak:
        return f"The group's {prior_streak} day streak has ended.\n{_FINAL_NOTE}"
    return _FINAL_NOTE


def _heading(day: date, newest: date) -> str:
    """Yesterday's results, unless the bot fell behind and this is older."""
    return "Yesterday's results" if (newest - day).days == 1 else f"Results for {day.isoformat()}"


def _reminder_text(disc_uuid: str, streak: int, expires: datetime) -> str:
    when = discord.utils.format_dt(expires, "R")
    return f"<@{disc_uuid}> your {streak} day sudoku streak expires {when}. {_PLAY_NOTE}"


def _hours_label(hours: int) -> str:
    return f"{hours} hour{'' if hours == 1 else 's'}"


def _streak_label(days: int) -> str:
    return f"{days}+ day streak"


def _on_off(enabled: bool) -> str:
    return "on" if enabled else "off"


def _menu_text(settings: SudokuNotification) -> str:
    return (
        "**Sudoku notifications**\n"
        f"**Solve posts ({_on_off(settings.solve_ping)}):** ping me when a two-hourly post names me.\n"
        f"**Daily results ({_on_off(settings.daily_ping)}):** ping me when the day's results name me.\n"
        f"**Streak reminder ({_on_off(settings.streak_ping)}):** ping me "
        f"{_hours_label(settings.hours_before)} before a {_streak_label(settings.min_streak)} "
        "would expire, unless I've already solved that day's puzzle."
    )


async def _avatar(member: Optional[discord.Member]) -> Optional[bytes]:
    if member is None:
        return None
    try:
        return await member.display_avatar.with_size(64).read()
    except (discord.DiscordException, aiohttp.ClientError):
        return None


async def _render(
    bot: discord.Client, results: list[SudokuResult], filename: str, *, crown_first: bool
) -> discord.File:
    puzzle_days = await _puzzle_days()
    cards = []
    for i, result in enumerate(results):
        member = _find_member(bot, int(result.disc_uuid))
        cards.append(
            Card(
                name=member.display_name if member is not None else "Former member",
                seconds=result.seconds,
                streak=await _personal_streak(result.disc_uuid, result.day, puzzle_days),
                fills=result.fills,
                crowned=crown_first and i == 0,
                avatar=await _avatar(member),
            )
        )
    png = await asyncio.to_thread(render_cards, cards)
    return discord.File(BytesIO(png), filename=filename)


# ---------------------------------------------------------------------------
# Discord helpers
# ---------------------------------------------------------------------------


async def _resolve_channel(bot: discord.Client, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            logger.warning("sudoku channel %s not fetchable: %s", channel_id, exc)
            return None
    return channel if hasattr(channel, "send") else None


def _pings(disc_uuids: Iterable[str]) -> discord.AllowedMentions:
    """Only these users are pinged; anyone else the text names is just shown."""
    users = [discord.Object(id=int(disc_uuid)) for disc_uuid in disc_uuids]
    return discord.AllowedMentions(users=users, roles=False, everyone=False)


async def _save_settings(interaction: discord.Interaction, **changes: object) -> None:
    settings, _ = await SudokuNotification.get_or_create(disc_uuid=str(interaction.user.id))
    settings.update_from_dict(changes)
    await settings.save(update_fields=list(changes))
    await interaction.response.edit_message(content=_menu_text(settings), view=_MenuView(settings))


class _SettingSelect(discord.ui.Select):
    def __init__(self, field: str, choices: tuple[int, ...], current: int, label, row: int) -> None:
        super().__init__(
            options=[
                discord.SelectOption(label=label(choice), value=str(choice), default=choice == current)
                for choice in choices
            ],
            row=row,
        )
        self._field = field

    async def callback(self, interaction: discord.Interaction) -> None:
        await _save_settings(interaction, **{self._field: int(self.values[0])})


class _SettingToggle(discord.ui.Button):
    def __init__(self, field: str, label: str, enabled: bool) -> None:
        super().__init__(
            label=f"{label}: {_on_off(enabled)}",
            style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
            row=0,
        )
        self._field = field
        self._enable = not enabled

    async def callback(self, interaction: discord.Interaction) -> None:
        await _save_settings(interaction, **{self._field: self._enable})


class _MenuView(discord.ui.View):
    """The menu one player sees after pressing the button on a post."""

    def __init__(self, settings: SudokuNotification) -> None:
        super().__init__(timeout=MENU_SECONDS)
        self.add_item(_SettingToggle("solve_ping", "Solve posts", settings.solve_ping))
        self.add_item(_SettingToggle("daily_ping", "Daily results", settings.daily_ping))
        self.add_item(_SettingToggle("streak_ping", "Streak reminder", settings.streak_ping))
        self.add_item(_SettingSelect("hours_before", REMINDER_HOURS, settings.hours_before, _hours_label, 1))
        self.add_item(_SettingSelect("min_streak", REMINDER_STREAKS, settings.min_streak, _streak_label, 2))


class SudokuNotificationView(discord.ui.View):
    """The button on every sudoku post. Registered in ``bot.py:setup_hook``."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Notifications",
        style=discord.ButtonStyle.secondary,
        emoji="🔔",
        custom_id="sudoku:notifications",
    )
    async def open_menu(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        settings, _ = await SudokuNotification.get_or_create(disc_uuid=str(interaction.user.id))
        await interaction.response.send_message(_menu_text(settings), view=_MenuView(settings), ephemeral=True)


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


async def _ensure_puzzle(_bot: discord.Client, now: datetime) -> None:
    """Store the current day's puzzle if it's missing. A failure backs off and
    retries later; until a row exists, the API keeps serving the newest one.

    The very first puzzle takes the next day's date, so the coming rollover finds
    it already there and it stays up, rather than lasting only until that rollover.
    """
    global _retry_at
    newest = await SudokuDay.all().order_by("-day").first()
    day = sudoku.puzzle_day(now)
    if newest is None:
        day += timedelta(days=1)
    elif newest.day >= day:
        return
    if time.monotonic() < _retry_at:
        return
    if newest is None:
        difficulty = sudoku.pick_difficulty(None, [])
    elif newest.next_difficulty:
        difficulty = newest.next_difficulty  # forced by ~manage_return 4 difficulty
    else:
        seconds = await SudokuResult.filter(day=newest.day).values_list("seconds", flat=True)
        difficulty = sudoku.pick_difficulty(newest.difficulty, seconds)
    try:
        givens = await sudoku.generate_givens(difficulty)
    except Exception as exc:  # a missing java or jar, a bad run: never crash the tick
        _retry_at = time.monotonic() + GENERATE_RETRY_SECONDS
        logger.warning("sudoku generation for %s failed, will retry: %s", day, exc)
        return
    # A force set while that ran has to win, so the next tick generates again.
    if newest is not None:
        await newest.refresh_from_db(fields=["next_difficulty"])
        if newest.next_difficulty and newest.next_difficulty != difficulty:
            return
    await SudokuDay.create(day=day, givens=givens, difficulty=difficulty)
    logger.info("sudoku puzzle for %s stored (%s)", day, difficulty)


async def _send_final(
    bot: discord.Client, channel, row: SudokuDay, ranked: list[SudokuResult], newest: date
) -> None:
    puzzle_days = await _puzzle_days()
    if not ranked:
        earlier = [d for d in puzzle_days if d < row.day]
        prior = await _group_streak(max(earlier), puzzle_days) if earlier else 0
        await channel.send(
            _empty_final_text(prior), view=SudokuNotificationView(), allowed_mentions=discord.AllowedMentions.none()
        )
        return
    text = _final_text(
        row,
        ranked,
        await _group_streak(row.day, puzzle_days),
        await _all_time_top(row.day),
        _heading(row.day, newest),
    )
    listed = ranked[:LIST_CAP]
    file = await _render(bot, listed, f"sudoku-{row.day}-final.png", crown_first=True)
    opted_out = set(await SudokuNotification.filter(daily_ping=False).values_list("disc_uuid", flat=True))
    pings = _pings(r.disc_uuid for r in listed if r.disc_uuid not in opted_out)
    await channel.send(text, file=file, view=SudokuNotificationView(), allowed_mentions=pings)


async def _close_day(bot: discord.Client, row: SudokuDay, newest: date, channel_id: int) -> None:
    """Send the final post, then close the day. While the kill switch is on the
    day closes silently, or turning it back off would flood the channel."""
    async with _lock:
        _closing.add(row.day)
        ranked = await _ranked(row.day)
    if channel_id:
        channel = await _resolve_channel(bot, channel_id)
        if channel is None:
            return
        await _send_final(bot, channel, row, ranked, newest)
    row.rollup_done = True
    await row.save(update_fields=["rollup_done"])
    # The final post covers everyone, so nobody from this day is left to batch.
    await SudokuResult.filter(day=row.day, announced=False).update(announced=True)


async def _post_finals(bot: discord.Client, now: datetime) -> None:
    newest = await SudokuDay.all().order_by("-day").first()
    if newest is None:
        return
    channel_id = CurrConfig.SUDOKU_CHANNEL
    for row in await SudokuDay.filter(rollup_done=False, day__lt=newest.day).order_by("day"):
        if now < sudoku.final_post_at(row.day):
            continue
        try:
            await _close_day(bot, row, newest.day, channel_id)
        except discord.HTTPException as exc:
            logger.warning("sudoku final post for %s failed, will retry: %s", row.day, exc)
        finally:
            _closing.discard(row.day)


async def _post_batch(bot: discord.Client, now: datetime) -> None:
    """Post everyone who solved before the latest even hour. While the kill switch
    is on they count as posted, or turning it back off would flood the channel."""
    pending = await SudokuResult.filter(announced=False, finished_at__lt=sudoku.batch_cutoff(now)).order_by(
        "finished_at", "id"
    )
    if not pending:
        return
    channel_id = CurrConfig.SUDOKU_CHANNEL
    if channel_id:
        channel = await _resolve_channel(bot, channel_id)
        if channel is None:
            return
        # One player can appear twice if the bot fell behind across a rollover.
        players = list(dict.fromkeys(r.disc_uuid for r in pending))
        file = await _render(bot, pending[:LIST_CAP], f"sudoku-batch-{pending[0].id}.png", crown_first=False)
        opted_in = set(await SudokuNotification.filter(solve_ping=True).values_list("disc_uuid", flat=True))
        pings = _pings(p for p in players[:LIST_CAP] if p in opted_in)
        try:
            await channel.send(_playing_text(players), file=file, view=SudokuNotificationView(), allowed_mentions=pings)
        except discord.HTTPException as exc:
            logger.warning("sudoku batch post failed, will retry: %s", exc)
            return
    await SudokuResult.filter(id__in=[r.id for r in pending]).update(announced=True)


async def _send_reminders(bot: discord.Client, now: datetime) -> None:
    """Ping each subscriber once a day, if the newest puzzle is due to expire
    unsolved with a long enough streak riding on it."""
    channel_id = CurrConfig.SUDOKU_CHANNEL
    newest = await SudokuDay.all().order_by("-day").first()
    if not channel_id or newest is None:
        return
    due = [
        settings
        for settings in await SudokuNotification.filter(streak_ping=True)
        if settings.last_reminded_day != newest.day
        and sudoku.reminder_due(newest.day, settings.hours_before, now)
    ]
    if not due:
        return
    solved = set(await SudokuResult.filter(day=newest.day).values_list("disc_uuid", flat=True))
    puzzle_days = await _puzzle_days()
    for settings in due:
        disc_uuid = settings.disc_uuid
        if disc_uuid in solved or _find_member(bot, int(disc_uuid)) is None:
            continue
        # As of the day before, since the newest day is the one still unsolved.
        streak = await _personal_streak(disc_uuid, newest.day - timedelta(days=1), puzzle_days)
        if streak < settings.min_streak:
            continue
        channel = await _resolve_channel(bot, channel_id)
        if channel is None:
            return
        text = _reminder_text(disc_uuid, streak, sudoku.expires_at(newest.day))
        try:
            await channel.send(text, view=SudokuNotificationView(), allowed_mentions=_pings([disc_uuid]))
        except discord.HTTPException as exc:
            logger.warning("sudoku reminder for %s failed, will retry: %s", disc_uuid, exc)
            return
        settings.last_reminded_day = newest.day
        await settings.save(update_fields=["last_reminded_day"])


# ---------------------------------------------------------------------------
# Manage
# ---------------------------------------------------------------------------


async def _replace_puzzle(day: date, givens: str) -> Optional[int]:
    """Swap ``day``'s puzzle in place and wipe its solves, returning how many.

    The row keeps its date, which is what sets when a puzzle expires, so the new
    puzzle lasts exactly as long as the old one would have. None if ``day`` is no
    longer the newest, because a rollover got in while generating.
    """
    async with _lock:
        newest = await SudokuDay.all().order_by("-day").first()
        if newest is None or newest.day != day:
            return None
        async with in_transaction("default"):
            wiped = await SudokuResult.filter(day=day).delete()
            # Someone reminded today may have just lost the solve that kept their streak.
            await SudokuNotification.filter(last_reminded_day=day).update(last_reminded_day=None)
            newest.givens = givens
            await newest.save(update_fields=["givens"])
    return wiped


@register_manage(
    WEEK, "void", tier=Tier.STAFF,
    help="Replace the current puzzle with a new one at the same difficulty and wipe its results. "
         "The new puzzle expires when the old one would have.",
)
async def _manage_void(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    row = await SudokuDay.all().order_by("-day").first()
    if row is None:
        await send_feedback(ctx, "There's no puzzle to void.", persist=persist)
        return
    # New puzzles are failing to generate, and the next success would replace it.
    if _now() >= sudoku.expires_at(row.day):
        await send_feedback(ctx, f"⚠️ The puzzle for {row.day} has already expired, so nothing changed.", persist=persist)
        return
    # Generate first, so a failure leaves the puzzle and its results alone.
    try:
        givens = await sudoku.generate_givens(row.difficulty)
    except Exception as exc:
        logger.warning("sudoku void of %s could not generate: %s", row.day, exc)
        await send_feedback(ctx, f"⚠️ Couldn't generate a new puzzle, so nothing changed: {exc}", persist=persist)
        return
    wiped = await _replace_puzzle(row.day, givens)
    if wiped is None:
        await send_feedback(ctx, f"⚠️ A newer puzzle arrived, so {row.day} was left alone. Run it again.", persist=persist)
        return
    logger.info("sudoku puzzle for %s voided by %s, %d results wiped", row.day, ctx.author, wiped)
    expires = discord.utils.format_dt(sudoku.expires_at(row.day), "R")
    await send_feedback(
        ctx,
        f"✅ Replaced the {row.difficulty} puzzle for {row.day} and wiped {wiped} result(s). "
        f"It expires {expires}, as before. Players get it within about a minute.",
        persist=persist,
    )


@register_manage(
    WEEK, "difficulty", tier=Tier.STAFF,
    help="Show or force the next puzzle's difficulty, one step from the current puzzle's or from an "
         "earlier force. Overrides the solve-time rule for that puzzle only; today's is unchanged.",
    usage="[up|down]",
)
async def _manage_difficulty(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    row = await SudokuDay.all().order_by("-day").first()
    if row is None:
        await send_feedback(ctx, "There's no puzzle yet.", persist=persist)
        return
    if not args:
        next_level = f"forced to {row.next_difficulty}" if row.next_difficulty else "set by today's solve times"
        await send_feedback(ctx, f"Current puzzle: {row.difficulty}. Next puzzle: {next_level}.", persist=persist)
        return
    steps = {"up": 1, "down": -1}
    if len(args) != 1 or args[0].lower() not in steps:
        await send_feedback(ctx, "Usage: `~manage_return 4 difficulty [up|down]`", persist=persist)
        return
    row.next_difficulty = sudoku.step_difficulty(row.next_difficulty or row.difficulty, steps[args[0].lower()])
    await row.save(update_fields=["next_difficulty"])
    logger.info("sudoku next difficulty forced to %s by %s", row.next_difficulty, ctx.author)
    await send_feedback(ctx, f"✅ The next puzzle will be {row.next_difficulty}.", persist=persist)


@register_tick(WEEK)
async def tick(bot: discord.Client) -> None:
    now = _now()
    # Separate steps, so a failure in one can't hold up the others.
    for step in (_ensure_puzzle, _post_finals, _post_batch, _send_reminders):
        try:
            await step(bot, now)
        except Exception:
            logger.exception("sudoku tick step %s failed", step.__name__)

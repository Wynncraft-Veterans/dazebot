"""Return 4: the daily sudoku. A permanent module, not a past event.

One puzzle a day, played inside Minecraft through vetsmod, with results posted
to Discord. dazebot owns the puzzle and the results; temporary-server only
carries traffic, because vetsmod may not reach dazebot:

* at the 04:00 Halifax rollover the tick runs HoDoKu and stores a
  :class:`orm.SudokuDay`,
* ``api/main.py`` serves it to temporary-server's poller and records solves
  through :func:`today` and :func:`submit`,
* each minute the tick edits the day's live post if the solved count changed,
* from 05:00 the tick posts the previous day's final results, once the new
  puzzle exists, and sets ``rollup_done``, which closes that day.

A day takes submissions until then, so the final post really is final. Only the
tick posts, so a client retry cannot double-post. Each post is sent, then saved:
a crash between the two can cause a rare duplicate, which is accepted.

There is no ``/return 4``: everything happens in game and in the tick. The
tables are in ``orm.py`` because returns.db has a documented wipe that would
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
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Optional

import aiohttp
import discord
from tortoise.exceptions import IntegrityError

from cogs.events.returns import register_tick
from cogs.events.returns.lib import sudoku
from cogs.events.returns.lib.sudoku_cards import Card, render_cards
from config import CurrConfig
from lib.staff.verify_keys import _find_member
from orm import SudokuDay, SudokuResult

logger = logging.getLogger("dazebot.cogs.events.returns.week_4")

WEEK = 4

TOP_N = 10
ALL_TIME_TOP_N = 5
GENERATE_RETRY_SECONDS = 600

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
    rank = next(i for i, r in enumerate(ranked, start=1) if r.disc_uuid == disc_uuid)
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


def _results_lines(row: SudokuDay, ranked: list[SudokuResult], group_streak: int, crown: str) -> list[str]:
    """The lines the live and final posts share. ``ranked`` must not be empty."""
    first = ranked[0]
    lines = [
        f"Your group is on a {group_streak} day streak!",
        _difficulty_line(row.difficulty),
        f"👑 <@{first.disc_uuid}> {crown} {sudoku.format_time(first.seconds)}",
    ]
    lines += [f"{sudoku.format_time(r.seconds)} <@{r.disc_uuid}>" for r in ranked[:TOP_N]]
    others = len(ranked) - TOP_N
    if others > 0:
        lines.append(f"and {others} other{'' if others == 1 else 's'}")
    return lines


def _live_text(row: SudokuDay, ranked: list[SudokuResult], group_streak: int) -> str:
    return "\n".join([*_results_lines(row, ranked, group_streak, "leads with"), _PLAY_NOTE])


def _final_text(
    row: SudokuDay,
    ranked: list[SudokuResult],
    group_streak: int,
    top: list[tuple[str, int]],
    heading: str,
) -> str:
    lines = [f"**{heading}**", *_results_lines(row, ranked, group_streak, "won with")]
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


async def _avatar(member: Optional[discord.Member]) -> Optional[bytes]:
    if member is None:
        return None
    try:
        return await member.display_avatar.with_size(64).read()
    except (discord.DiscordException, aiohttp.ClientError):
        return None


async def _render(
    bot: discord.Client, ranked: list[SudokuResult], day: date, puzzle_days: list[date], filename: str
) -> discord.File:
    cards = []
    for rank, result in enumerate(ranked[:TOP_N], start=1):
        member = _find_member(bot, int(result.disc_uuid))
        cards.append(
            Card(
                name=member.display_name if member is not None else "Former member",
                seconds=result.seconds,
                streak=await _personal_streak(result.disc_uuid, day, puzzle_days),
                fills=result.fills,
                crowned=rank == 1,
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
    difficulty = sudoku.pick_difficulty(day)
    try:
        givens = await sudoku.generate_givens(difficulty)
    except Exception as exc:  # a missing java or jar, a bad run: never crash the tick
        _retry_at = time.monotonic() + GENERATE_RETRY_SECONDS
        logger.warning("sudoku generation for %s failed, will retry: %s", day, exc)
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
        await channel.send(_empty_final_text(prior), allowed_mentions=discord.AllowedMentions.none())
        return
    text = _final_text(
        row,
        ranked,
        await _group_streak(row.day, puzzle_days),
        await _all_time_top(row.day),
        _heading(row.day, newest),
    )
    file = await _render(bot, ranked, row.day, puzzle_days, f"sudoku-{row.day}-final.png")
    await channel.send(text, file=file, allowed_mentions=discord.AllowedMentions.none())


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


async def _sync_live_row(bot: discord.Client, row: SudokuDay, channel_id: int) -> None:
    ranked = await _ranked(row.day)
    count = len(ranked)
    if count == 0 or (row.live_message_id and count == row.live_count):
        return
    puzzle_days = await _puzzle_days()
    text = _live_text(row, ranked, await _group_streak(row.day, puzzle_days))
    # A new filename each time busts Discord's image cache.
    file = await _render(bot, ranked, row.day, puzzle_days, f"sudoku-{row.day}-{count}.png")
    if row.live_message_id:
        try:
            channel = bot.get_channel(row.channel_id) or await bot.fetch_channel(row.channel_id)
            message = await channel.fetch_message(row.live_message_id)
            await message.edit(content=text, attachments=[file], allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            row.channel_id = row.live_message_id = None
            row.live_count = 0
            await row.save(update_fields=["channel_id", "live_message_id", "live_count"])
            return
    else:
        channel = await _resolve_channel(bot, channel_id)
        if channel is None:
            return
        message = await channel.send(text, file=file, allowed_mentions=discord.AllowedMentions.none())
        row.channel_id = channel.id
        row.live_message_id = message.id
    row.live_count = count
    await row.save(update_fields=["channel_id", "live_message_id", "live_count"])


async def _sync_live(bot: discord.Client, _now_at: datetime) -> None:
    channel_id = CurrConfig.SUDOKU_CHANNEL
    if not channel_id:
        return
    for row in await SudokuDay.filter(rollup_done=False).order_by("day"):
        try:
            await _sync_live_row(bot, row, channel_id)
        except discord.HTTPException as exc:
            logger.warning("sudoku live post for %s failed, will retry: %s", row.day, exc)


@register_tick(WEEK)
async def tick(bot: discord.Client) -> None:
    now = _now()
    # Separate steps, so a failure in one can't hold up the others.
    for step in (_ensure_puzzle, _post_finals, _sync_live):
        try:
            await step(bot, now)
        except Exception:
            logger.exception("sudoku tick step %s failed", step.__name__)

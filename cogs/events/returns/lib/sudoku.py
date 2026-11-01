"""Pure daily-sudoku rules for Return 4, plus the HoDoKu subprocess.

Nothing here touches Discord or the database. The rule check accepts any valid
grid: uniqueness comes from HoDoKu and was checked once when it was chosen, so
there is no solver at runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import re
import shutil
from collections import Counter
from collections.abc import Collection, Iterable
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

HALIFAX = ZoneInfo("America/Halifax")
ROLLOVER_HOURS = 4  # a puzzle day runs 04:00 to 04:00 Halifax, the lowest-load time
FINAL_POST_TIME = time(5)  # gives the previous day an hour's grace
BATCH_HOURS = 2  # solves are posted together on even hours, so on the rollover too

DIFFICULTY_WEIGHTS = {"easy": 6, "medium": 1}
SCORE_MULTIPLIERS = {"easy": 1, "medium": 3}
MAX_FILL_SECONDS = 7 * 24 * 60 * 60

GENERATE_TIMEOUT_SECONDS = 120
STOP_TIMEOUT_SECONDS = 10

_SOLVED_RE = re.compile(r"[1-9]{81}")
_PUZZLE_RE = re.compile(r"(\d{81}) #(Easy|Medium)")
_LEVELS = {"easy": ("0", "Easy"), "medium": ("1", "Medium")}


class GenerationError(Exception):
    """HoDoKu could not produce a puzzle. The caller retries on a later tick."""


def puzzle_day(now: datetime) -> date:
    """The puzzle day at instant ``now``. A solve at 02:00 counts toward the
    puzzle that started the previous 04:00. Subtracting from an aware datetime
    is wall-clock arithmetic, so clock changes don't move the rollover."""
    return (now.astimezone(HALIFAX) - timedelta(hours=ROLLOVER_HOURS)).date()


def final_post_at(day: date) -> datetime:
    """The earliest instant ``day``'s final post may go out."""
    return datetime.combine(day + timedelta(days=1), FINAL_POST_TIME, tzinfo=HALIFAX)


def batch_cutoff(now: datetime) -> datetime:
    """The latest batch boundary at or before ``now``. A solve before it is due
    to be posted; a later one waits for the next boundary. In UTC, because SQLite
    compares ``finished_at`` as text, and that text is always in UTC."""
    local = now.astimezone(HALIFAX)
    cutoff = local.replace(hour=local.hour - local.hour % BATCH_HOURS, minute=0, second=0, microsecond=0)
    return cutoff.astimezone(timezone.utc)


def expires_at(day: date) -> datetime:
    """When a streak needing ``day``'s puzzle runs out: the rollover, since a
    player opening the board after it gets the next puzzle. The hour's grace
    before the final post is for boards already being played."""
    return datetime.combine(day + timedelta(days=1), time(ROLLOVER_HOURS), tzinfo=HALIFAX)


def reminder_due(day: date, hours_before: int, now: datetime) -> bool:
    """True from ``hours_before`` real hours before ``day`` expires until it does.
    In UTC, because subtracting from a Halifax datetime is wall-clock arithmetic
    and a clock change would move the ping by an hour."""
    expires = expires_at(day).astimezone(timezone.utc)
    return expires - timedelta(hours=hours_before) <= now < expires


def pick_difficulty(day: date) -> str:
    """Seeded by the day, so a retry after a failed generation keeps the level."""
    levels = list(DIFFICULTY_WEIGHTS)
    return random.Random(day.isoformat()).choices(levels, weights=list(DIFFICULTY_WEIGHTS.values()))[0]


def format_time(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def is_solution(givens: str, grid: object) -> bool:
    """True if ``grid`` keeps ``givens`` and completes every row, column and box."""
    if not isinstance(grid, str) or _SOLVED_RE.fullmatch(grid) is None:
        return False
    if any(g != "0" and g != c for g, c in zip(givens, grid)):
        return False
    rows = [grid[r * 9 : r * 9 + 9] for r in range(9)]
    columns = [grid[c::9] for c in range(9)]
    boxes = [
        "".join(rows[r + i][c : c + 3] for i in range(3))
        for r in (0, 3, 6)
        for c in (0, 3, 6)
    ]
    return all(len(set(unit)) == 9 for unit in rows + columns + boxes)


def fills_valid(givens: str, fills: object) -> bool:
    """81 real ints (not bools or floats): 0 exactly on givens, else 1 to the cap."""
    if not isinstance(fills, list) or len(fills) != 81:
        return False
    for given, fill in zip(givens, fills):
        if type(fill) is not int:
            return False
        if given != "0":
            if fill != 0:
                return False
        elif not 1 <= fill <= MAX_FILL_SECONDS:
            return False
    return True


def streak(puzzle_days: Iterable[date], solved: Collection[date], as_of: date) -> int:
    """Consecutive puzzle days up to ``as_of`` that are in ``solved``.

    A date with no puzzle (generation failed all day, the bot was down) is not
    a puzzle day, so it can't break a streak. Pass one player's days for a
    personal streak, or every day with a solve for the group's.
    """
    count = 0
    for day in sorted((d for d in puzzle_days if d <= as_of), reverse=True):
        if day not in solved:
            break
        count += 1
    return count


def score(rank: int, difficulty: str) -> int:
    """10 points for first, down to 1 for 10th and below, times the multiplier."""
    return SCORE_MULTIPLIERS[difficulty] * max(1, 11 - rank)


def all_time_scores(ranked: Iterable[tuple[date, str]], difficulties: dict[date, str]) -> Counter[str]:
    """Total score per player from ``(day, disc_uuid)`` rows in rank order per day."""
    totals: Counter[str] = Counter()
    ranks: Counter[date] = Counter()
    for day, disc_uuid in ranked:
        ranks[day] += 1
        totals[disc_uuid] += score(ranks[day], difficulties[day])
    return totals


def _command(java: str, jar: str, difficulty: str) -> list[str]:
    # Not on Windows: if Git Bash's nice is on the PATH it rewrites HoDoKu's
    # /c and /s switches as file paths, and java gets something else entirely.
    prefix = []
    if os.name == "posix":
        if shutil.which("nice"):
            prefix += ["nice", "-n", "19"]
        if shutil.which("taskset"):
            prefix += ["taskset", "-c", "1"]
    return [
        *prefix,
        java,
        "-Xmx64m",
        "-XX:+UseSerialGC",
        "-XX:ActiveProcessorCount=1",
        "-Djava.awt.headless=true",
        "-jar",
        jar,
        "/c",
        "default",
        "/s",
        "/sl",
        _LEVELS[difficulty][0],
        "/o",
        "stdout",
    ]


async def _first_puzzle(stdout: asyncio.StreamReader, difficulty: str) -> str:
    while line := await stdout.readline():
        match = _PUZZLE_RE.fullmatch(line.decode(errors="replace").strip())
        if match is None:
            continue
        if match[2] != _LEVELS[difficulty][1]:
            raise GenerationError(f"asked for {difficulty}, got {match[2]}")
        return match[1]
    raise GenerationError("HoDoKu exited without a puzzle")


async def generate_givens(difficulty: str) -> str:
    """Run HoDoKu until its first puzzle at ``difficulty``; returns the 81 digits."""
    java = os.environ.get("HODOKU_JAVA")
    jar = os.environ.get("HODOKU_JAR")
    if not java or not jar:
        raise GenerationError("HODOKU_JAVA / HODOKU_JAR are not set")
    if not os.path.exists(jar):
        raise GenerationError(f"no jar at {jar}")
    # Only logging reaches stderr. HoDoKu also exits on stdin EOF, so a dying
    # bot leaves no orphan.
    proc = await asyncio.create_subprocess_exec(
        *_command(java, jar, difficulty),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        try:
            givens = await asyncio.wait_for(_first_puzzle(proc.stdout, difficulty), GENERATE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise GenerationError(f"no puzzle within {GENERATE_TIMEOUT_SECONDS} s") from None
        # It keeps generating until told to stop, and communicate() keeps
        # draining stdout meanwhile so it can't block on a full pipe.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.communicate(b"q\n"), STOP_TIMEOUT_SECONDS)
        return givens
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            # Not wait(): with output unread the pipe never reports EOF, so it
            # would never return, and returns_tick runs every return in series.
            await proc.communicate()

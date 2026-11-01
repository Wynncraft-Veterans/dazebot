"""Tests for the pure daily-sudoku rules, the HoDoKu subprocess and the cards.

HoDoKu itself is faked with :class:`FakeProc`, so CI needs no Java.
"""

from __future__ import annotations

import asyncio
import colorsys
import sys
from datetime import date, datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from cogs.events.returns.lib import sudoku
from cogs.events.returns.lib.sudoku_cards import AVATAR, AVATAR_FALLBACK, AVATAR_TOP, GAP, PAD, Card, ramp, render_cards

SOLVED = "534678912672195348198342567859761423426853791713924856961537284287419635345286179"
GIVENS = "".join(c if i % 2 else "0" for i, c in enumerate(SOLVED))
PUZZLE = "0" * 40 + "5" + "0" * 40


def halifax(year, month, day, hour, minute=0, fold=0):
    return datetime(year, month, day, hour, minute, tzinfo=sudoku.HALIFAX, fold=fold)


def good_fills():
    return [0 if g != "0" else i + 1 for i, g in enumerate(GIVENS)]


# --- Day -------------------------------------------------------------------


@pytest.mark.parametrize(
    "now, expected",
    [
        (halifax(2026, 12, 10, 3, 59), date(2026, 12, 9)),
        (halifax(2026, 12, 10, 4, 0), date(2026, 12, 10)),
        (halifax(2026, 12, 10, 23, 59), date(2026, 12, 10)),
        (halifax(2026, 12, 11, 2, 0), date(2026, 12, 10)),
        # Clocks go back at 02:00 on 2026-11-01: 04:00 still starts the new day.
        (halifax(2026, 11, 1, 1, 30, fold=0), date(2026, 10, 31)),
        (halifax(2026, 11, 1, 1, 30, fold=1), date(2026, 10, 31)),
        (halifax(2026, 11, 1, 3, 59), date(2026, 10, 31)),
        (halifax(2026, 11, 1, 4, 0), date(2026, 11, 1)),
        # Clocks go forward at 02:00 on 2027-03-14.
        (halifax(2027, 3, 14, 3, 59), date(2027, 3, 13)),
        (halifax(2027, 3, 14, 4, 0), date(2027, 3, 14)),
    ],
)
def test_puzzle_day_rolls_over_at_four_on_the_wall_clock(now, expected):
    assert sudoku.puzzle_day(now) == expected
    assert sudoku.puzzle_day(now.astimezone(timezone.utc)) == expected


def test_final_post_at_is_five_the_next_morning_across_a_clock_change():
    assert sudoku.final_post_at(date(2026, 10, 31)) == halifax(2026, 11, 1, 5)
    assert sudoku.final_post_at(date(2026, 10, 31)).astimezone(timezone.utc).hour == 9  # AST
    assert sudoku.final_post_at(date(2027, 3, 13)).astimezone(timezone.utc).hour == 8  # ADT


@pytest.mark.parametrize(
    "now, cutoff",
    [
        (halifax(2026, 12, 10, 14), halifax(2026, 12, 10, 14)),
        (halifax(2026, 12, 10, 15, 59), halifax(2026, 12, 10, 14)),
        (halifax(2026, 12, 10, 4, 1), halifax(2026, 12, 10, 4)),
        (halifax(2026, 12, 10, 0, 30), halifax(2026, 12, 10, 0)),
    ],
)
def test_batch_cutoff_is_the_latest_even_hour(now, cutoff):
    assert sudoku.batch_cutoff(now) == cutoff
    assert sudoku.batch_cutoff(now.astimezone(timezone.utc)) == cutoff


def test_batch_cutoff_is_never_after_now_across_a_clock_change():
    # 02:00 doesn't exist on 2027-03-14, and it resolves to the instant of the jump.
    now = datetime(2027, 3, 14, 6, 30, tzinfo=timezone.utc)  # 03:30 ADT
    cutoff = sudoku.batch_cutoff(now)
    assert cutoff.astimezone(timezone.utc) == datetime(2027, 3, 14, 6, tzinfo=timezone.utc)
    # 01:30 happens twice on 2026-11-01; both round down to the same midnight.
    for fold in (0, 1):
        assert sudoku.batch_cutoff(halifax(2026, 11, 1, 1, 30, fold=fold)) == halifax(2026, 11, 1, 0)


def test_expires_at_is_the_next_rollover():
    assert sudoku.expires_at(date(2026, 10, 31)) == halifax(2026, 11, 1, 4)
    assert sudoku.puzzle_day(sudoku.expires_at(date(2026, 10, 31))) == date(2026, 11, 1)


def test_reminder_due_window():
    day = date(2026, 12, 10)
    assert not sudoku.reminder_due(day, 3, halifax(2026, 12, 11, 0, 59))
    assert sudoku.reminder_due(day, 3, halifax(2026, 12, 11, 1))
    assert sudoku.reminder_due(day, 3, halifax(2026, 12, 11, 3, 59))
    assert not sudoku.reminder_due(day, 3, halifax(2026, 12, 11, 4))


def test_reminder_due_counts_real_hours_across_a_clock_change():
    # Clocks go back at 02:00 on 2026-11-01, so expiry is 08:00 UTC. Six real
    # hours before is 02:00 UTC, where wall-clock arithmetic would say 04:00 UTC.
    day = date(2026, 10, 31)
    assert sudoku.expires_at(day).astimezone(timezone.utc).hour == 8
    assert not sudoku.reminder_due(day, 6, datetime(2026, 11, 1, 1, 59, tzinfo=timezone.utc))
    assert sudoku.reminder_due(day, 6, datetime(2026, 11, 1, 2, tzinfo=timezone.utc))


def test_pick_difficulty_starts_easy():
    assert sudoku.pick_difficulty(None, []) == "easy"


@pytest.mark.parametrize(
    ("previous", "seconds", "expected"),
    [
        # A fast median steps up, stopping at the top.
        ("easy", [200, 300, 359], "medium"),
        ("medium", [359], "hard"),
        ("hard", [60], "hard"),
        # A slow median steps down, stopping at the bottom.
        ("hard", [601, 900, 9000], "medium"),
        ("medium", [601], "easy"),
        ("easy", [5000], "easy"),
        # Inside the 6 to 10 minute target holds, both edges included.
        ("medium", [360], "medium"),
        ("medium", [600], "medium"),
        # Only ever one step, however far outside the target it was.
        ("easy", [1], "medium"),
        ("hard", [90000], "medium"),
        # The median, so one very slow solve doesn't move it.
        ("easy", [100, 200, 90000], "medium"),
        # An even count averages the middle two: 360 holds, 359.5 doesn't.
        ("easy", [300, 420], "easy"),
        ("easy", [300, 419], "medium"),
        # Nobody solved it, so no median: start over at easy, from any level.
        ("hard", [], "easy"),
        ("medium", [], "easy"),
        ("easy", [], "easy"),
    ],
)
def test_pick_difficulty_follows_the_last_days_median(previous, seconds, expected):
    assert sudoku.pick_difficulty(previous, seconds) == expected


def test_step_difficulty_stops_at_either_end():
    assert sudoku.step_difficulty("easy", 1) == "medium"
    assert sudoku.step_difficulty("hard", -1) == "medium"
    assert sudoku.step_difficulty("hard", 1) == "hard"
    assert sudoku.step_difficulty("easy", -1) == "easy"


def test_every_difficulty_has_a_multiplier():
    assert set(sudoku.SCORE_MULTIPLIERS) == set(sudoku.DIFFICULTIES)


def test_format_time():
    assert sudoku.format_time(5) == "0:05"
    assert sudoku.format_time(263) == "4:23"
    assert sudoku.format_time(3725) == "62:05"


# --- Validation ------------------------------------------------------------


def test_is_solution_accepts_a_complete_grid():
    assert sudoku.is_solution(GIVENS, SOLVED)


def test_is_solution_accepts_any_valid_grid_for_the_givens():
    # Digits relabelled, so a different grid that still fits: no solver at runtime.
    swapped = SOLVED.translate(str.maketrans("12", "21"))
    assert swapped != SOLVED
    assert sudoku.is_solution(PUZZLE, swapped)


@pytest.mark.parametrize(
    "grid",
    [
        None,
        81,
        SOLVED[:-1],
        SOLVED + "1",
        SOLVED[:-1] + "0",
        SOLVED[:-1] + "x",
        SOLVED[:-1] + "٣",  # a digit to str.isdigit, not to us
        SOLVED + "\n",
    ],
)
def test_is_solution_rejects_malformed(grid):
    assert not sudoku.is_solution(GIVENS, grid)


def test_is_solution_rejects_a_changed_given():
    assert not sudoku.is_solution("6" + "0" * 80, SOLVED)


def _swap(grid, a, b):
    cells = list(grid)
    cells[a], cells[b] = cells[b], cells[a]
    return "".join(cells)


@pytest.mark.parametrize(
    "grid, unit",
    [
        ("123456789" * 9, "columns and boxes"),
        (_swap(SOLVED, 0, 1), "columns and boxes, rows intact"),
        (_swap(SOLVED, 0, 9), "rows and boxes, columns intact"),
        # Valid rows and columns, broken boxes: a Latin square of cyclic shifts.
        ("".join("123456789"[i:] + "123456789"[:i] for i in range(9)), "boxes only"),
    ],
)
def test_is_solution_rejects_broken_units(grid, unit):
    assert not sudoku.is_solution("0" * 81, grid), unit


def test_fills_valid_accepts_good_fills():
    assert sudoku.fills_valid(GIVENS, good_fills())


def _with(index, value):
    fills = good_fills()
    fills[index] = value
    return fills


@pytest.mark.parametrize(
    "fills",
    [
        None,
        "0" * 81,
        good_fills()[:-1],
        good_fills() + [1],
        _with(0, 0),  # zero on a blank
        _with(0, -1),
        _with(0, True),  # a bool is an int to isinstance
        _with(0, 1.0),
        _with(0, "1"),
        _with(0, None),
        _with(0, sudoku.MAX_FILL_SECONDS + 1),
        _with(0, 10**30),
        _with(1, 1),  # non-zero on a given
    ],
)
def test_fills_valid_rejects(fills):
    assert not sudoku.fills_valid(GIVENS, fills)


def test_fills_valid_accepts_the_upper_bound():
    assert sudoku.fills_valid(GIVENS, _with(0, sudoku.MAX_FILL_SECONDS))


# --- Streak and score ------------------------------------------------------


D1, D2, D3, D4 = (date(2026, 12, d) for d in (1, 2, 3, 4))


def test_streak_counts_back_from_as_of():
    days = [D1, D2, D3, D4]
    assert sudoku.streak(days, {D2, D3, D4}, D4) == 3
    assert sudoku.streak(days, {D1, D2, D4}, D4) == 1
    assert sudoku.streak(days, {D1, D2, D4}, D3) == 0
    assert sudoku.streak(days, {D1, D2, D4}, D2) == 2
    assert sudoku.streak(days, set(), D4) == 0


def test_streak_is_not_broken_by_a_date_with_no_puzzle():
    assert sudoku.streak([D1, D3, D4], {D1, D3, D4}, D4) == 3


def test_streak_takes_days_in_any_order():
    assert sudoku.streak([D4, D1, D3, D2], {D3, D4}, D4) == 2


@pytest.mark.parametrize(
    "rank, difficulty, expected",
    [
        (1, "easy", 10),
        (2, "easy", 9),
        (10, "easy", 1),
        (11, "easy", 1),
        (50, "easy", 1),
        (1, "medium", 30),
        (10, "medium", 3),
        (50, "medium", 3),
    ],
)
def test_score(rank, difficulty, expected):
    assert sudoku.score(rank, difficulty) == expected


def test_all_time_scores_ranks_within_each_day():
    ranked = [(D1, "a"), (D1, "b"), (D2, "b"), (D2, "a")]
    totals = sudoku.all_time_scores(ranked, {D1: "easy", D2: "medium"})
    assert totals == {"a": 10 + 27, "b": 9 + 30}


# --- Generation ------------------------------------------------------------


EASY_LINE = PUZZLE + " #Easy"
MEDIUM_LINE = PUZZLE + " #Medium"


class FakeProc:
    def __init__(self, lines, *, hang=False, hang_on_stop=False):
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(line.encode() + b"\r\n")
        if not hang:
            self.stdout.feed_eof()
        self.hang_on_stop = hang_on_stop
        self.returncode = None
        self.killed = False
        self.sent = None

    async def communicate(self, data=None):
        if self.killed:
            return b"", b""
        self.sent = data
        if self.hang_on_stop:
            await asyncio.sleep(3600)
        self.returncode = 0
        return b"", b""

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.fixture
def hodoku(tmp_path, monkeypatch):
    jar = tmp_path / "HoDoKu.jar"
    jar.write_bytes(b"jar")
    monkeypatch.setenv("HODOKU_JAVA", "java")
    monkeypatch.setenv("HODOKU_JAR", str(jar))

    def install(proc):
        spawn = AsyncMock(return_value=proc)
        monkeypatch.setattr(sudoku.asyncio, "create_subprocess_exec", spawn)
        return spawn

    return install


async def test_generate_reads_the_first_puzzle_among_noise_then_stops(hodoku):
    proc = FakeProc(["Hodoku - v2.6.0", "Starting...", EASY_LINE, "0" * 81 + " #Easy"])
    spawn = hodoku(proc)
    assert await sudoku.generate_givens("easy") == PUZZLE
    assert proc.sent == b"q\n"
    assert not proc.killed
    args = spawn.await_args.args
    assert args[-5:] == ("/s", "/sl", "0", "/o", "stdout")
    assert "-Xmx64m" in args and "-Djava.awt.headless=true" in args


async def test_generate_medium_uses_level_one(hodoku):
    spawn = hodoku(FakeProc([MEDIUM_LINE]))
    assert await sudoku.generate_givens("medium") == PUZZLE
    assert spawn.await_args.args[-3] == "1"


async def test_generate_refuses_the_wrong_level_and_kills(hodoku):
    proc = FakeProc([EASY_LINE], hang=True)
    hodoku(proc)
    with pytest.raises(sudoku.GenerationError):
        await sudoku.generate_givens("medium")
    assert proc.killed


@pytest.mark.parametrize("bad", ["1" * 80 + " #Easy", "1" * 82 + " #Easy", "x" * 81 + " #Easy", "1" * 81 + " #Hard"])
async def test_generate_ignores_malformed_puzzle_lines(hodoku, bad):
    hodoku(FakeProc([bad]))
    with pytest.raises(sudoku.GenerationError):
        await sudoku.generate_givens("easy")


async def test_generate_times_out_and_kills(hodoku, monkeypatch):
    monkeypatch.setattr(sudoku, "GENERATE_TIMEOUT_SECONDS", 0.01)
    proc = FakeProc(["noise"], hang=True)
    hodoku(proc)
    with pytest.raises(sudoku.GenerationError):
        await sudoku.generate_givens("easy")
    assert proc.killed


async def test_generate_keeps_the_puzzle_if_stopping_hangs(hodoku, monkeypatch):
    monkeypatch.setattr(sudoku, "STOP_TIMEOUT_SECONDS", 0.01)
    proc = FakeProc([EASY_LINE], hang_on_stop=True)
    hodoku(proc)
    assert await sudoku.generate_givens("easy") == PUZZLE
    assert proc.killed


async def test_generate_does_not_hang_killing_a_child_with_unread_output(monkeypatch, tmp_path):
    # A real child, as only real pipes show this: with enough output unread the
    # transport pauses and never sees EOF, so wait() alone would never return.
    flood = tmp_path / "flood.py"
    flood.write_text("import sys\nwhile True:\n    sys.stdout.write('noise\\n' * 4096)\n")
    monkeypatch.setenv("HODOKU_JAVA", sys.executable)
    monkeypatch.setenv("HODOKU_JAR", str(flood))
    monkeypatch.setattr(sudoku, "_command", lambda java, jar, difficulty: [java, jar])
    monkeypatch.setattr(sudoku, "GENERATE_TIMEOUT_SECONDS", 0.5)
    with pytest.raises(sudoku.GenerationError):
        await asyncio.wait_for(sudoku.generate_givens("easy"), 15)


async def test_generate_without_a_jar_raises_rather_than_spawning(hodoku, monkeypatch, tmp_path):
    spawn = hodoku(FakeProc([]))
    monkeypatch.setenv("HODOKU_JAR", str(tmp_path / "missing.jar"))
    with pytest.raises(sudoku.GenerationError):
        await sudoku.generate_givens("easy")
    monkeypatch.delenv("HODOKU_JAR")
    with pytest.raises(sudoku.GenerationError):
        await sudoku.generate_givens("easy")
    spawn.assert_not_awaited()


@pytest.mark.parametrize(
    "os_name, found, prefix",
    [
        ("posix", True, ["nice", "-n", "19", "taskset", "-c", "1"]),
        ("posix", False, []),
        ("nt", True, []),
    ],
)
def test_command_prefix(monkeypatch, os_name, found, prefix):
    monkeypatch.setattr(sudoku, "os", SimpleNamespace(name=os_name))
    monkeypatch.setattr(sudoku.shutil, "which", lambda name: name if found else None)
    command = sudoku._command("java", "HoDoKu.jar", "easy")
    assert command[: len(prefix) + 1] == [*prefix, "java"]


# --- Cards -----------------------------------------------------------------


def _luminance(rgb):
    def linear(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_ramp_is_single_hue_and_monotonic_in_luminance():
    colours = [ramp(i / 100) for i in range(101)]
    luminances = [_luminance(c) for c in colours]
    assert luminances == sorted(luminances)
    assert luminances[-1] > luminances[0] * 5
    hues = [colorsys.rgb_to_hsv(*(c / 255 for c in colour))[0] * 360 for colour in colours]
    assert max(hues) - min(hues) < 2


def _card(**kwargs):
    defaults = dict(name="Nia", seconds=81, streak=3, fills=good_fills())
    return Card(**{**defaults, **kwargs})


@pytest.mark.parametrize(
    "count, size",
    [
        (1, (281, 347)),
        (2, (552, 347)),
        (4, (1094, 347)),
        (5, (1094, 684)),
        (16, (1094, 1358)),
        (17, (1365, 1358)),
        (30, (1365, 2032)),
        (31, (1636, 2032)),
        (50, (1636, 3043)),
    ],
)
def test_render_cards_widens_as_the_list_grows(count, size):
    with Image.open(BytesIO(render_cards([_card() for _ in range(count)]))) as image:
        assert image.format == "PNG"
        assert image.size == size


def test_render_cards_survives_a_long_name_and_a_bad_avatar():
    png = render_cards([_card(name="x" * 200, avatar=b"not an image", crowned=True)])
    assert png.startswith(b"\x89PNG")


def test_render_cards_survives_the_longest_time_and_a_huge_streak():
    assert render_cards([_card(seconds=sudoku.MAX_FILL_SECONDS, streak=10**6)]).startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "pixel, expected",
    [((0, 0, 0, 0), AVATAR_FALLBACK), ((200, 30, 30, 255), (200, 30, 30))],
)
def test_render_cards_shows_the_plain_disc_through_a_transparent_avatar(pixel, expected):
    avatar = BytesIO()
    Image.new("RGBA", (64, 64), pixel).save(avatar, format="PNG")
    centre = (GAP + PAD + AVATAR // 2, GAP + AVATAR_TOP + AVATAR // 2)
    with Image.open(BytesIO(render_cards([_card(avatar=avatar.getvalue())]))) as image:
        assert image.convert("RGB").getpixel(centre) == expected


def test_render_cards_shows_givens_neutral_and_the_last_cell_brightest():
    fills = [0] * 81
    fills[40] = 81
    with Image.open(BytesIO(render_cards([_card(fills=fills)]))) as image:
        colours = {colour for _, colour in image.convert("RGB").getcolors(maxcolors=100000)}
    assert ramp(1.0) in colours
    assert ramp(0.5) not in colours

"""Tests for ``cogs.events.returns.week_4`` (the daily sudoku).

Discord is mocked and HoDoKu is replaced at ``sudoku.generate_givens``. The
tables are real: each test bootstraps a fresh DB through ``init_db`` (the
``_isolated_db`` pattern from ``tests/test_migrations.py``), which also proves
the migration applies. The routes are called directly, since the project has no
HTTP test client installed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from importlib import import_module
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from fastapi import HTTPException
from PIL import Image
from tortoise import Tortoise
from tortoise.exceptions import IntegrityError

from api.main import create_app
from cogs.events.returns import MANAGE_REGISTRY, REGISTRY, TICK_REGISTRY, Tier
from cogs.events.returns import week_4 as w
from cogs.events.returns.lib import sudoku
from orm import SudokuDay, SudokuNotification, SudokuResult, init_db

SOLVED ="534678912672195348198342567859761423426853791713924856961537284287419635345286179"
GIVENS = "".join(c if i % 2 else "0" for i, c in enumerate(SOLVED))
SECRET = "test-secret"
CHANNEL = 111111111111111111

D1, D2, D3 = date(2026, 12, 9), date(2026, 12, 10), date(2026, 12, 11)


def halifax(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=sudoku.HALIFAX)


def fills(seconds: int = 90) -> list[int]:
    """Valid fills whose maximum is ``seconds``."""
    out = [0 if g != "0" else 1 for g in GIVENS]
    out[0] = seconds
    return out


@pytest.fixture
async def _isolated_db(tmp_path, monkeypatch):
    from aerich.migrate import Migrate
    from tortoise import Tortoise

    async def _reset():
        try:
            await Tortoise.close_connections()
        except Exception:
            pass
        Tortoise._inited = False
        for attr in ("_last_version_content", "migrate_location", "ddl", "ddl_class", "dialect"):
            if hasattr(Migrate, attr):
                try:
                    delattr(Migrate, attr)
                except AttributeError:
                    pass
        Migrate.upgrade_operators = []
        Migrate.downgrade_operators = []

    await _reset()
    monkeypatch.setenv("DAZEBOT_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DAZEBOT_RETURNS_DB_PATH", str(tmp_path / "test_returns.db"))
    monkeypatch.setenv("DAZEBOT_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("DAZEBOT_ALLOW_FRESH_RETURNS_DB", "1")
    await init_db()
    yield
    await _reset()


@pytest.fixture(autouse=True)
def _module_state(monkeypatch):
    monkeypatch.setattr(w, "_retry_at", 0.0)
    w._closing.clear()
    monkeypatch.setenv("DAZEBOT_INTROSPECT_SECRET", SECRET)
    monkeypatch.setattr(w.CurrConfig, "SUDOKU_CHANNEL", CHANNEL)
    yield
    w._closing.clear()


def _avatar_png() -> bytes:
    out = BytesIO()
    Image.new("RGB", (64, 64), (200, 80, 80)).save(out, format="PNG")
    return out.getvalue()


def _member(disc_uuid: str) -> MagicMock:
    member = MagicMock()
    member.display_name = f"Player {disc_uuid}"
    member.display_avatar.with_size.return_value.read = AsyncMock(return_value=_avatar_png())
    return member


class Harness:
    """A bot with one guild and one channel, plus the API routes on top of it."""

    def __init__(self):
        self.members = {n: _member(str(n)) for n in range(1, 16)}
        guild = MagicMock()
        guild.get_member.side_effect = self.members.get
        self.channel = MagicMock(id=CHANNEL)
        self.channel.send = AsyncMock()
        self.bot = MagicMock()
        self.bot.guilds = [guild]
        self.bot.is_ready.return_value = True
        self.bot.get_channel.return_value = self.channel
        self.bot.fetch_channel = AsyncMock(return_value=self.channel)
        self.routes = {r.path: r.endpoint for r in create_app(self.bot).routes}

    async def today(self, secret=SECRET):
        return await self.routes["/api/internal/sudoku/today"](x_introspect_secret=secret)

    async def submit(self, disc_uuid="1", day=D2, grid=SOLVED, secret=SECRET, seconds=90, **overrides):
        body = {
            "disc_uuid": disc_uuid,
            "day": day.isoformat() if isinstance(day, date) else day,
            "grid": grid,
            "fills": fills(seconds),
            **overrides,
        }
        return await self.routes["/api/internal/sudoku/submit"](body=body, x_introspect_secret=secret)

    def sent(self, call=-1) -> str:
        return self.channel.send.await_args_list[call].args[0]


@pytest.fixture
def h() -> Harness:
    return Harness()


@pytest.fixture
def at(monkeypatch):
    def set_now(when: datetime) -> None:
        monkeypatch.setattr(w, "_now", lambda: when)

    return set_now


async def _submit_status(h: Harness, **kwargs) -> int:
    with pytest.raises(HTTPException) as exc:
        await h.submit(**kwargs)
    return exc.value.status_code


def _http_error(cls=discord.HTTPException, status=500):
    return cls(MagicMock(status=status, reason="x"), "boom")


def _pinged(mentions: discord.AllowedMentions) -> list[int]:
    return [user.id for user in mentions.users]


async def _seed(*days: date, difficulty: str = "easy", **fields) -> None:
    for day in days:
        await SudokuDay.create(day=day, givens=GIVENS, difficulty=difficulty, **fields)


# --- Registration ----------------------------------------------------------


def test_registers_a_tick_and_no_return_command():
    assert TICK_REGISTRY[4] is w.tick
    assert 4 not in REGISTRY


# --- Generation in the tick ------------------------------------------------


async def test_tick_stores_the_days_puzzle_once(_isolated_db, h, at, monkeypatch):
    await _seed(D1)
    at(halifax(D2, 4))
    generate = AsyncMock(return_value=GIVENS)
    monkeypatch.setattr(sudoku, "generate_givens", generate)
    monkeypatch.setattr(sudoku, "pick_difficulty", lambda previous, seconds: "medium")
    await w.tick(h.bot)
    await w.tick(h.bot)
    generate.assert_awaited_once_with("medium")
    row = await SudokuDay.get(day=D2)
    assert (row.givens, row.difficulty, row.rollup_done) == (GIVENS, "medium", False)


async def _solve(day: date, disc_uuid: str, seconds: int) -> None:
    await SudokuResult.create(
        day=day, disc_uuid=disc_uuid, seconds=seconds, fills=[seconds], finished_at=halifax(day, 12)
    )


async def test_the_next_level_follows_the_last_days_median(_isolated_db, h, at, monkeypatch):
    await _seed(D1, difficulty="medium")
    await _solve(D1, "1", 100)
    await _solve(D1, "2", 200)
    await _solve(D1, "3", 90000)
    generate = AsyncMock(return_value=GIVENS)
    monkeypatch.setattr(sudoku, "generate_givens", generate)
    at(halifax(D2, 4))
    await w.tick(h.bot)
    generate.assert_awaited_once_with("hard")
    assert (await SudokuDay.get(day=D2)).difficulty == "hard"


async def test_the_next_level_starts_over_after_a_day_nobody_solved(_isolated_db, h, at, monkeypatch):
    await _seed(D1, difficulty="hard")
    monkeypatch.setattr(sudoku, "generate_givens", AsyncMock(return_value=GIVENS))
    at(halifax(D2, 4))
    await w.tick(h.bot)
    assert (await SudokuDay.get(day=D2)).difficulty == "easy"


# --- ~manage_return 4 difficulty ---------------------------------------------


def test_difficulty_is_registered_for_staff():
    assert MANAGE_REGISTRY[4]["difficulty"].tier is Tier.STAFF


async def test_forcing_up_overrides_the_solve_times_once(_isolated_db, h, at, monkeypatch):
    await _seed(D1)
    # A slow day would step an easy puzzle nowhere, and a fast one up: use slow.
    await _solve(D1, "1", 5000)
    generate = AsyncMock(return_value=GIVENS)
    monkeypatch.setattr(sudoku, "generate_givens", generate)
    ctx = _void_ctx()

    await w._manage_difficulty(ctx, ["up"])

    assert "next puzzle will be medium" in _reply(ctx)
    assert (await SudokuDay.get(day=D1)).difficulty == "easy"  # today's is untouched
    at(halifax(D2, 4))
    await w.tick(h.bot)
    generate.assert_awaited_once_with("medium")
    assert (await SudokuDay.get(day=D2)).difficulty == "medium"

    # Only that puzzle: the one after goes back to solve times, here none, so easy.
    generate.reset_mock()
    at(halifax(D3, 4))
    await w.tick(h.bot)
    generate.assert_awaited_once_with("easy")


async def test_a_force_during_generation_wins_on_the_next_tick(_isolated_db, h, at, monkeypatch):
    await _seed(D1)
    await _solve(D1, "1", 500)  # in the target, so it would stay easy
    levels = []

    async def force_meanwhile(difficulty):
        levels.append(difficulty)
        if len(levels) == 1:
            await w._manage_difficulty(_void_ctx(), ["up"])
        return GIVENS

    monkeypatch.setattr(sudoku, "generate_givens", force_meanwhile)
    at(halifax(D2, 4))
    await w.tick(h.bot)
    assert not await SudokuDay.exists(day=D2)

    await w.tick(h.bot)
    assert levels == ["easy", "medium"]
    assert (await SudokuDay.get(day=D2)).difficulty == "medium"


async def test_forcing_stacks_and_stops_at_the_ends(_isolated_db):
    await _seed(D1)
    ctx = _void_ctx()
    await w._manage_difficulty(ctx, ["up"])
    await w._manage_difficulty(ctx, ["UP"])
    await w._manage_difficulty(ctx, ["up"])
    assert (await SudokuDay.get(day=D1)).next_difficulty == "hard"
    await w._manage_difficulty(ctx, ["down"])
    assert (await SudokuDay.get(day=D1)).next_difficulty == "medium"


async def test_forcing_down_from_the_bottom_still_holds_it_there(_isolated_db, h, at, monkeypatch):
    await _seed(D1)
    await _solve(D1, "1", 60)  # fast enough to step up on its own
    generate = AsyncMock(return_value=GIVENS)
    monkeypatch.setattr(sudoku, "generate_givens", generate)
    await w._manage_difficulty(_void_ctx(), ["down"])
    at(halifax(D2, 4))
    await w.tick(h.bot)
    generate.assert_awaited_once_with("easy")


async def test_difficulty_with_no_argument_reports_it(_isolated_db):
    await _seed(D1, difficulty="medium")
    ctx = _void_ctx()
    await w._manage_difficulty(ctx, [])
    assert "Current puzzle: medium" in _reply(ctx)
    assert "set by today's solve times" in _reply(ctx)
    await w._manage_difficulty(ctx, ["up"])
    ctx = _void_ctx()
    await w._manage_difficulty(ctx, [])
    assert "forced to hard" in _reply(ctx)


async def test_difficulty_refuses_anything_else(_isolated_db):
    await _seed(D1)
    ctx = _void_ctx()
    await w._manage_difficulty(ctx, ["sideways"])
    assert "Usage" in _reply(ctx)
    assert (await SudokuDay.get(day=D1)).next_difficulty is None


async def test_difficulty_with_no_puzzle_says_so(_isolated_db):
    ctx = _void_ctx()
    await w._manage_difficulty(ctx, ["up"])
    assert "no puzzle" in _reply(ctx)


async def test_the_first_puzzle_is_easy(_isolated_db, h, at, monkeypatch):
    generate = AsyncMock(return_value=GIVENS)
    monkeypatch.setattr(sudoku, "generate_givens", generate)
    at(halifax(D1, 12))
    await w.tick(h.bot)
    generate.assert_awaited_once_with("easy")


# --- ~manage_return 4 void ---------------------------------------------------


NEW_GIVENS = "0" + GIVENS[1:]


def _void_ctx():
    ctx = MagicMock()
    ctx.channel = MagicMock(spec=discord.DMChannel)
    ctx.reply = AsyncMock()
    return ctx


def _reply(ctx) -> str:
    return " ".join(call.args[0] for call in ctx.reply.await_args_list)


def test_void_is_registered_for_staff():
    assert MANAGE_REGISTRY[4]["void"].tier is Tier.STAFF


async def test_void_swaps_the_newest_puzzle_and_wipes_only_its_results(_isolated_db, monkeypatch):
    await _seed(D1)
    await _seed(D2, difficulty="medium")
    await _solve(D1, "1", 60)
    await _solve(D2, "1", 60)
    await _solve(D2, "2", 90)
    generate = AsyncMock(return_value=NEW_GIVENS)
    monkeypatch.setattr(sudoku, "generate_givens", generate)
    ctx = _void_ctx()

    await w._manage_void(ctx, [])

    # The same level and the same date, which is what keeps the old expiry.
    generate.assert_awaited_once_with("medium")
    row = await SudokuDay.get(day=D2)
    assert (row.givens, row.difficulty, row.rollup_done) == (NEW_GIVENS, "medium", False)
    assert await SudokuResult.filter(day=D2).count() == 0
    assert await SudokuResult.filter(day=D1).count() == 1
    assert (await SudokuDay.get(day=D1)).givens == GIVENS
    assert (await w.today())["givens"] == NEW_GIVENS
    assert "wiped 2 result(s)" in _reply(ctx)


async def test_void_lets_a_player_solve_the_new_puzzle_but_not_the_old(_isolated_db, h, monkeypatch):
    new_givens = SOLVED[:40] + "0" + SOLVED[41:]
    old_givens = "0" + SOLVED[1:]
    await SudokuDay.create(day=D2, givens=old_givens, difficulty="easy")
    monkeypatch.setattr(sudoku, "generate_givens", AsyncMock(return_value=new_givens))
    monkeypatch.setattr(w, "_find_member", lambda bot, disc_uuid: _member(str(disc_uuid)))
    await w._manage_void(_void_ctx(), [])

    # A board of the old puzzle is the same solved grid here, so use a wrong one.
    wrong = "6" + SOLVED[1:]
    with pytest.raises(w.SubmitRejected):
        await w.submit(h.bot, {"disc_uuid": "1", "day": D2.isoformat(), "grid": wrong, "fills": [1] * 81})
    fills_for_new = [0 if g != "0" else 5 for g in new_givens]
    result = await w.submit(
        h.bot, {"disc_uuid": "1", "day": D2.isoformat(), "grid": SOLVED, "fills": fills_for_new}
    )
    assert (result["seconds"], result["rank"], result["duplicate"]) == (5, 1, False)


async def test_a_voided_puzzle_keeps_its_expiry(_isolated_db, h, at, monkeypatch):
    # The first puzzle is dated a day ahead; a void must not bring its end forward.
    generate = AsyncMock(return_value=GIVENS)
    monkeypatch.setattr(sudoku, "generate_givens", generate)
    at(halifax(D1, 12))
    await w.tick(h.bot)
    assert [row.day for row in await SudokuDay.all()] == [D2]

    generate.return_value = NEW_GIVENS
    await w._manage_void(_void_ctx(), [])

    # The rollover into its date finds it there; the one after that replaces it.
    generate.reset_mock()
    at(halifax(D2, 4))
    await w.tick(h.bot)
    generate.assert_not_awaited()
    assert (await SudokuDay.get(day=D2)).givens == NEW_GIVENS
    at(halifax(D3, 4))
    await w.tick(h.bot)
    generate.assert_awaited_once()
    assert [row.day for row in await SudokuDay.all().order_by("day")] == [D2, D3]


async def test_void_changes_nothing_when_generation_fails(_isolated_db, monkeypatch):
    await _seed(D2)
    await _solve(D2, "1", 60)
    monkeypatch.setattr(sudoku, "generate_givens", AsyncMock(side_effect=sudoku.GenerationError("no jar")))
    ctx = _void_ctx()

    await w._manage_void(ctx, [])

    assert (await SudokuDay.get(day=D2)).givens == GIVENS
    assert await SudokuResult.filter(day=D2).count() == 1
    assert "nothing changed" in _reply(ctx)


async def test_void_leaves_a_day_that_stopped_being_the_newest(_isolated_db, monkeypatch):
    await _seed(D1)
    await _solve(D1, "1", 60)

    async def rollover_meanwhile(_difficulty):
        await _seed(D2)
        return NEW_GIVENS

    monkeypatch.setattr(sudoku, "generate_givens", rollover_meanwhile)
    ctx = _void_ctx()

    await w._manage_void(ctx, [])

    assert (await SudokuDay.get(day=D1)).givens == GIVENS
    assert await SudokuResult.filter(day=D1).count() == 1
    assert (await SudokuDay.get(day=D2)).givens == GIVENS
    assert "left alone" in _reply(ctx)


async def test_void_refuses_a_puzzle_past_its_expiry(_isolated_db, at, monkeypatch):
    await _seed(D1)
    await _solve(D1, "1", 60)
    generate = AsyncMock(return_value=NEW_GIVENS)
    monkeypatch.setattr(sudoku, "generate_givens", generate)
    at(halifax(D2, 4))
    ctx = _void_ctx()

    await w._manage_void(ctx, [])

    generate.assert_not_awaited()
    assert (await SudokuDay.get(day=D1)).givens == GIVENS
    assert await SudokuResult.filter(day=D1).count() == 1
    assert "already expired" in _reply(ctx)


async def test_void_lets_todays_reminder_go_out_again(_isolated_db, at, monkeypatch):
    await _seed(D2)
    other_day = await SudokuNotification.create(disc_uuid="1", last_reminded_day=D1)
    today = await SudokuNotification.create(disc_uuid="2", last_reminded_day=D2)
    monkeypatch.setattr(sudoku, "generate_givens", AsyncMock(return_value=NEW_GIVENS))
    at(halifax(D2, 12))

    await w._manage_void(_void_ctx(), [])

    await other_day.refresh_from_db()
    await today.refresh_from_db()
    assert other_day.last_reminded_day == D1
    assert today.last_reminded_day is None


async def test_a_submit_a_void_wiped_at_once_is_refused(_isolated_db, h, monkeypatch):
    await SudokuDay.create(day=D2, givens="0" + SOLVED[1:], difficulty="easy")
    monkeypatch.setattr(w, "_find_member", lambda bot, disc_uuid: _member(str(disc_uuid)))
    monkeypatch.setattr(w, "_ranked", AsyncMock(return_value=[]))
    with pytest.raises(w.SubmitRejected) as rejected:
        await w.submit(h.bot, {"disc_uuid": "1", "day": D2.isoformat(), "grid": SOLVED, "fills": [5] + [0] * 80})
    assert rejected.value.status == 410


async def test_void_with_no_puzzle_says_so(_isolated_db, monkeypatch):
    generate = AsyncMock()
    monkeypatch.setattr(sudoku, "generate_givens", generate)
    ctx = _void_ctx()

    await w._manage_void(ctx, [])

    generate.assert_not_awaited()
    assert "no puzzle" in _reply(ctx)


async def test_tick_before_the_rollover_belongs_to_the_previous_day(_isolated_db, h, at, monkeypatch):
    await _seed(D1 - timedelta(days=1))
    at(halifax(D2, 3, 59))
    monkeypatch.setattr(sudoku, "generate_givens", AsyncMock(return_value=GIVENS))
    await w.tick(h.bot)
    assert await SudokuDay.exists(day=D1)
    assert not await SudokuDay.exists(day=D2)


@pytest.mark.parametrize("launch", [halifax(D1, 12), halifax(D2, 0)], ids=["noon", "midnight"])
async def test_the_first_puzzle_outlasts_the_next_rollover(_isolated_db, h, at, monkeypatch, launch):
    generate = AsyncMock(return_value=GIVENS)
    monkeypatch.setattr(sudoku, "generate_givens", generate)

    # Both launches fall in puzzle day D1, so the first puzzle is dated D2.
    at(launch)
    await w.tick(h.bot)
    assert [row.day for row in await SudokuDay.all()] == [D2]
    assert (await w.today())["day"] == D2.isoformat()

    # The rollover into D2 finds it already there, including just after launch.
    at(halifax(D2, 4))
    await w.tick(h.bot)
    generate.assert_awaited_once()

    # The next one replaces it: 40 hours from noon, 28 from midnight.
    at(halifax(D3, 4))
    await w.tick(h.bot)
    assert [row.day for row in await SudokuDay.all().order_by("day")] == [D2, D3]


async def test_generation_failure_backs_off_then_retries(_isolated_db, h, at, monkeypatch):
    await _seed(D1)
    at(halifax(D2, 4))
    generate = AsyncMock(side_effect=sudoku.GenerationError("no jar"))
    monkeypatch.setattr(sudoku, "generate_givens", generate)
    await w.tick(h.bot)
    await w.tick(h.bot)
    generate.assert_awaited_once()
    assert not await SudokuDay.exists(day=D2)

    monkeypatch.setattr(w, "_retry_at", 0.0)  # as if about 10 minutes had passed
    generate.side_effect = None
    generate.return_value = GIVENS
    await w.tick(h.bot)
    assert await SudokuDay.exists(day=D2)


async def test_missing_java_config_never_crashes_the_tick(_isolated_db, h, at, monkeypatch):
    at(halifax(D2, 4))
    monkeypatch.delenv("HODOKU_JAVA", raising=False)
    monkeypatch.delenv("HODOKU_JAR", raising=False)
    await w.tick(h.bot)
    assert not await SudokuDay.exists(day=D2)


# --- GET today -------------------------------------------------------------


async def test_today_gate_and_body(_isolated_db, h):
    for secret, status in ((None, 401), ("wrong", 401), (SECRET, 503)):
        with pytest.raises(HTTPException) as exc:
            await h.today(secret=secret)
        assert exc.value.status_code == status
    await _seed(D1)
    await _seed(D2, difficulty="medium")
    assert await h.today() == {"day": D2.isoformat(), "givens": GIVENS, "difficulty": "medium"}


async def test_routes_fail_closed_without_the_secret_configured(_isolated_db, h, monkeypatch):
    monkeypatch.delenv("DAZEBOT_INTROSPECT_SECRET")
    with pytest.raises(HTTPException) as exc:
        await h.today(secret=None)
    assert exc.value.status_code == 503
    assert await _submit_status(h, secret=None) == 503


# --- POST submit -----------------------------------------------------------


async def test_submit_stores_and_acks(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    ack = await h.submit(seconds=263)
    assert ack == {"status": "ok", "seconds": 263, "rank": 1, "streak": 1, "duplicate": False}
    row = await SudokuResult.get(day=D2, disc_uuid="1")
    assert row.seconds == 263
    assert row.fills == fills(263)


async def test_rank_is_by_seconds_then_finished_at(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    assert (await h.submit(disc_uuid="1", seconds=200))["rank"] == 1
    at(halifax(D2, 13))
    assert (await h.submit(disc_uuid="2", seconds=100))["rank"] == 1
    assert (await h.submit(disc_uuid="1", seconds=200))["rank"] == 2
    at(halifax(D2, 14))
    assert (await h.submit(disc_uuid="3", seconds=200))["rank"] == 3


async def test_streak_runs_across_consecutive_puzzle_days(_isolated_db, h, at):
    await _seed(D1, D2, D3)
    at(halifax(D3, 12))
    assert (await h.submit(day=D1))["streak"] == 1
    assert (await h.submit(day=D3))["streak"] == 1
    assert (await h.submit(day=D2))["streak"] == 2
    assert (await h.submit(day=D3))["streak"] == 3


async def test_duplicate_returns_the_stored_result_without_overwriting(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    await h.submit(seconds=100)
    at(halifax(D2, 13))
    ack = await h.submit(seconds=50)
    assert ack["duplicate"] is True
    assert ack["seconds"] == 100
    assert await SudokuResult.filter(day=D2).count() == 1
    row = await SudokuResult.get(day=D2, disc_uuid="1")
    assert row.seconds == 100
    assert row.finished_at.hour == halifax(D2, 12).astimezone(row.finished_at.tzinfo).hour


async def test_racing_insert_keeps_the_stored_row(_isolated_db, h, at, monkeypatch):
    await _seed(D2)
    at(halifax(D2, 12))
    await h.submit(seconds=100)
    create = AsyncMock(side_effect=IntegrityError("UNIQUE constraint failed"))
    monkeypatch.setattr(SudokuResult, "create", create)
    ack = await h.submit(seconds=50)
    assert (ack["duplicate"], ack["seconds"]) == (True, 100)


async def test_submit_gate(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    assert await _submit_status(h, secret="wrong") == 401
    assert await _submit_status(h, secret=None) == 401
    h.bot.is_ready.return_value = False
    assert await _submit_status(h) == 503
    h.bot.is_ready.return_value = True
    assert await SudokuResult.all().count() == 0


async def test_unknown_member_is_403_not_a_crash(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    assert await _submit_status(h, disc_uuid="999") == 403
    assert await SudokuResult.all().count() == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"disc_uuid": None},
        {"disc_uuid": 1},
        {"disc_uuid": "abc"},
        {"disc_uuid": "٣"},
        {"disc_uuid": ""},
        {"day": None},
        {"day": 20261210},
        {"day": "20261210"},
        {"day": "2026-13-45"},
        {"grid": None},
        {"grid": SOLVED[:-1]},
        {"grid": "1" * 81},
        {"grid": "6" + SOLVED[1:]},
        {"fills": None},
        {"fills": fills()[:-1]},
        {"fills": [True if f == 1 else f for f in fills()]},
        {"fills": [float(f) for f in fills()]},
        {"fills": [1] * 81},
        {"fills": [10**30 if f == 90 else f for f in fills()]},
    ],
)
async def test_malformed_or_wrong_submission_is_400(_isolated_db, h, at, overrides):
    await _seed(D2)
    at(halifax(D2, 12))
    assert await _submit_status(h, **overrides) == 400
    assert await SudokuResult.all().count() == 0


async def test_unknown_or_closed_day_is_410(_isolated_db, h, at):
    await _seed(D2)
    await _seed(D1, rollup_done=True)
    at(halifax(D2, 12))
    assert await _submit_status(h, day=D3) == 410
    assert await _submit_status(h, day=D1) == 410


async def test_day_being_closed_refuses_submissions(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    w._closing.add(D2)
    assert await _submit_status(h) == 410


# --- Batch posts -----------------------------------------------------------


async def test_batch_waits_for_the_even_hour_then_posts_once(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12, 30))
    await h.submit(disc_uuid="1", seconds=90)
    await w.tick(h.bot)
    at(halifax(D2, 13, 59))
    await h.submit(disc_uuid="2", seconds=60)
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()

    at(halifax(D2, 14))
    await h.submit(disc_uuid="3", seconds=70)  # on the boundary, so in the next batch
    await w.tick(h.bot)
    await w.tick(h.bot)
    h.channel.send.assert_awaited_once()
    call = h.channel.send.await_args
    assert call.args[0] == "<@1> and <@2> were playing.\nPlay it with `/wv extras sudoku` in game."
    assert isinstance(call.kwargs["view"], w.SudokuNotificationView)
    assert (await SudokuResult.get(disc_uuid="1")).announced is True
    assert (await SudokuResult.get(disc_uuid="3")).announced is False

    at(halifax(D2, 16))
    await w.tick(h.bot)
    assert h.sent().startswith("<@3> was playing.")


async def test_batch_shows_a_card_for_each_player_with_no_crown(_isolated_db, h, at, monkeypatch):
    rendered = []

    def capture(cards):
        rendered.extend(cards)
        return _avatar_png()

    monkeypatch.setattr(w, "render_cards", capture)
    await _seed(D2)
    at(halifax(D2, 12, 30))
    for n in (1, 2, 3):
        await h.submit(disc_uuid=str(n), seconds=60 + n)
    at(halifax(D2, 14))
    await w.tick(h.bot)
    assert h.sent().startswith("<@1>, <@2>, and <@3> were playing.")
    assert [card.name for card in rendered] == ["Player 1", "Player 2", "Player 3"]
    assert not any(card.crowned for card in rendered)
    assert h.channel.send.await_args.kwargs["file"].filename.endswith(".png")


async def test_batch_mentions_without_pinging_by_default(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    await h.submit(disc_uuid="1")
    await h.submit(disc_uuid="2")
    await SudokuNotification.create(disc_uuid="2", solve_ping=True)
    await SudokuNotification.create(disc_uuid="9", solve_ping=True)  # didn't play
    at(halifax(D2, 14))
    await w.tick(h.bot)
    mentions = h.channel.send.await_args.kwargs["allowed_mentions"]
    assert "<@1>" in h.sent()
    assert _pinged(mentions) == [2]
    assert (mentions.roles, mentions.everyone) == (False, False)


async def test_a_client_retry_does_not_repeat_a_player(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    await h.submit(disc_uuid="1")
    await h.submit(disc_uuid="1")
    at(halifax(D2, 14))
    await w.tick(h.bot)
    assert h.sent().startswith("<@1> was playing.")


async def test_batch_lists_a_player_once_across_two_days(_isolated_db, h, at):
    await _seed(D2, D3)
    at(halifax(D3, 4, 30))
    await h.submit(disc_uuid="1", day=D2)
    await h.submit(disc_uuid="1", day=D3)
    await SudokuNotification.create(disc_uuid="1", solve_ping=True)
    at(halifax(D3, 6))
    h.channel.send.side_effect = [_http_error(), None]  # the final fails, so D2 stays open
    await w.tick(h.bot)
    assert h.sent().startswith("<@1> was playing.")
    assert _pinged(h.channel.send.await_args.kwargs["allowed_mentions"]) == [1]


async def test_failed_batch_retries_next_tick(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    await h.submit()
    at(halifax(D2, 14))
    h.channel.send.side_effect = _http_error()
    await w.tick(h.bot)
    assert (await SudokuResult.get(disc_uuid="1")).announced is False
    h.channel.send.side_effect = None
    await w.tick(h.bot)
    assert (await SudokuResult.get(disc_uuid="1")).announced is True
    assert h.channel.send.await_count == 2


async def test_final_absorbs_solves_still_waiting_for_a_batch(_isolated_db, h, at):
    await _seed(D2, D3)
    at(halifax(D3, 4, 30))  # the grace hour
    await h.submit(disc_uuid="1", day=D2)
    at(halifax(D3, 5))
    await w.tick(h.bot)
    assert (await SudokuResult.get(disc_uuid="1")).announced is True
    at(halifax(D3, 6))
    await w.tick(h.bot)
    h.channel.send.assert_awaited_once()
    assert "<@1>" in (await _final_sends(h))[0]


async def test_channel_zero_stops_posting_but_not_the_puzzle(_isolated_db, h, at, monkeypatch):
    monkeypatch.setattr(w.CurrConfig, "SUDOKU_CHANNEL", 0)
    monkeypatch.setattr(sudoku, "generate_givens", AsyncMock(return_value=GIVENS))
    await _seed(D2)
    at(halifax(D2, 12))
    await h.submit()
    at(halifax(D2, 14))
    await w.tick(h.bot)
    assert (await SudokuResult.get(disc_uuid="1")).announced is True
    at(halifax(D3, 5))
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()
    assert await SudokuDay.exists(day=D3)
    # Closed silently, so turning posting back on doesn't flood the channel.
    assert (await SudokuDay.get(day=D2)).rollup_done is True
    monkeypatch.setattr(w.CurrConfig, "SUDOKU_CHANNEL", CHANNEL)
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()


async def test_migration_marks_earlier_solves_announced(_isolated_db):
    migration = import_module("migrations.models.16_20261101145011_sudoku_notifications")
    db = Tortoise.get_connection("default")
    await db.execute_script(await migration.downgrade(db))
    await db.execute_query(
        "INSERT INTO sudoku_results (day, disc_uuid, seconds, fills, finished_at)"
        " VALUES ('2026-12-10', '1', 90, '[]', '2026-12-10 12:00:00')"
    )
    await db.execute_script(await migration.upgrade(db))
    assert (await SudokuResult.get(disc_uuid="1")).announced is True
    assert await SudokuNotification.all().count() == 0


# --- Final post ------------------------------------------------------------


async def _final_sends(h: Harness) -> list[str]:
    return [c.args[0] for c in h.channel.send.await_args_list if "Today's puzzle is up" in c.args[0]]


async def test_final_waits_for_five_and_for_the_new_puzzle(_isolated_db, h, at, monkeypatch):
    generate = AsyncMock(side_effect=sudoku.GenerationError("down"))
    monkeypatch.setattr(sudoku, "generate_givens", generate)
    await _seed(D2)
    at(halifax(D2, 12))
    await h.submit()

    # After the rollover but before 05:00, the old day still takes submissions.
    at(halifax(D3, 4, 30))
    await _seed(D3)
    await w.tick(h.bot)
    assert await _final_sends(h) == []
    assert (await h.submit(disc_uuid="2", day=D2))["rank"] == 2

    # 05:00, but generation is still failing so there is no newer row.
    await SudokuDay.filter(day=D3).delete()
    monkeypatch.setattr(w, "_retry_at", 0.0)
    at(halifax(D3, 5))
    await w.tick(h.bot)
    assert await _final_sends(h) == []
    assert (await SudokuDay.get(day=D2)).rollup_done is False

    await _seed(D3)
    await w.tick(h.bot)
    finals = await _final_sends(h)
    assert len(finals) == 1
    assert (await SudokuDay.get(day=D2)).rollup_done is True
    assert await _submit_status(h, disc_uuid="3", day=D2) == 410

    await w.tick(h.bot)
    assert len(await _final_sends(h)) == 1


async def test_final_content(_isolated_db, h, at):
    await _seed(D1, difficulty="medium", rollup_done=True)
    await _seed(D2, D3)
    await SudokuResult.create(day=D1, disc_uuid="2", seconds=10, fills=fills(10), finished_at=halifax(D1, 12))
    at(halifax(D2, 12))
    await h.submit(disc_uuid="1", seconds=90)
    await h.submit(disc_uuid="2", seconds=120)
    at(halifax(D3, 5))
    await w.tick(h.bot)
    call = next(c for c in h.channel.send.await_args_list if "Today's puzzle is up" in c.args[0])
    text = call.args[0]
    assert "**Yesterday's results**" in text
    assert "Your group is on a 2 day streak!" in text
    assert "👑 <@1> won with 1:30" in text
    assert "and " not in text.split("All-time")[0].split("won with")[1]
    # Medium win 30 + easy second 9 = 39, ahead of the easy win's 10.
    assert text.index("1. <@2> 39") < text.index("2. <@1> 10")
    assert text.endswith("Today's puzzle is up: `/wv extras sudoku` in game.")
    assert call.kwargs["file"].filename == f"sudoku-{D2}-final.png"


@pytest.mark.parametrize("difficulty, line", [("medium", "Medium (x3 score)"), ("easy", "Difficulty: Easy")])
async def test_final_shows_a_multiplier_only_when_there_is_one(_isolated_db, h, at, difficulty, line):
    await _seed(D2, difficulty=difficulty)
    await _seed(D3)
    at(halifax(D2, 12))
    await h.submit()
    at(halifax(D3, 5))
    await w.tick(h.bot)
    assert line in (await _final_sends(h))[0].splitlines()[2]


async def test_final_all_time_leaves_out_the_newer_day(_isolated_db, h, at):
    await _seed(D2, D3)
    at(halifax(D2, 12))
    await h.submit(disc_uuid="1")
    at(halifax(D3, 4, 30))
    await h.submit(disc_uuid="2", day=D3)
    at(halifax(D3, 5))
    await w.tick(h.bot)
    text = next(c.args[0] for c in h.channel.send.await_args_list if "All-time" in c.args[0])
    assert "<@2>" not in text


async def test_zero_solve_day_posts_the_note_only(_isolated_db, h, at):
    await _seed(D2, D3)
    at(halifax(D3, 5))
    await w.tick(h.bot)
    h.channel.send.assert_awaited_once()
    assert h.sent() == "Today's puzzle is up: `/wv extras sudoku` in game."
    assert "file" not in h.channel.send.await_args.kwargs


async def test_zero_solve_day_ends_a_live_streak(_isolated_db, h, at):
    await _seed(D1, rollup_done=True)
    await _seed(D2, D3)
    await SudokuResult.create(
        day=D1, disc_uuid="1", seconds=10, fills=fills(10), finished_at=halifax(D1, 12), announced=True
    )
    at(halifax(D3, 5))
    await w.tick(h.bot)
    assert h.sent() == (
        "The group's 1 day streak has ended.\nToday's puzzle is up: `/wv extras sudoku` in game."
    )


async def test_failed_final_stays_open_and_retries(_isolated_db, h, at):
    await _seed(D2, D3)
    at(halifax(D2, 12))
    await h.submit(disc_uuid="1")
    h.channel.send.side_effect = _http_error()
    at(halifax(D3, 5))
    await w.tick(h.bot)
    assert (await SudokuDay.get(day=D2)).rollup_done is False
    assert D2 not in w._closing
    assert (await h.submit(disc_uuid="2", day=D2))["rank"] == 2

    h.channel.send.side_effect = None
    await w.tick(h.bot)
    assert (await SudokuDay.get(day=D2)).rollup_done is True
    assert "<@2>" in (await _final_sends(h))[-1]


async def test_an_older_day_is_labelled_by_date(_isolated_db, h, at):
    await _seed(D1, D3)
    at(halifax(D1, 12))
    await h.submit(day=D1)
    at(halifax(D3, 5))
    await w.tick(h.bot)
    text = next(c.args[0] for c in h.channel.send.await_args_list if "All-time" in c.args[0])
    assert f"**Results for {D1.isoformat()}**" in text


async def test_group_streak_ignores_a_date_with_no_puzzle(_isolated_db, h, at):
    await _seed(D1, D3, D3 + timedelta(days=1))
    at(halifax(D1, 12))
    await h.submit(day=D1)
    at(halifax(D3, 12))
    await h.submit(day=D3)
    at(halifax(D3 + timedelta(days=1), 5))
    await w.tick(h.bot)
    assert "on a 2 day streak" in (await _final_sends(h))[-1]


# --- Daily pings -----------------------------------------------------------


async def test_final_pings_everyone_listed_unless_opted_out(_isolated_db, h, at):
    await _seed(D1, rollup_done=True)
    await _seed(D2, D3)
    # On the all-time list but didn't play this day, so shown and not pinged.
    await SudokuResult.create(
        day=D1, disc_uuid="5", seconds=10, fills=fills(10), finished_at=halifax(D1, 12), announced=True
    )
    at(halifax(D2, 12))
    for n in ("1", "2", "3"):
        await h.submit(disc_uuid=n)
    await SudokuNotification.create(disc_uuid="2", daily_ping=False)
    at(halifax(D3, 5))
    await w.tick(h.bot)
    call = next(c for c in h.channel.send.await_args_list if "Today's puzzle is up" in c.args[0])
    assert "<@2>" in call.args[0]
    assert "<@5>" in call.args[0]
    assert _pinged(call.kwargs["allowed_mentions"]) == [1, 3]
    assert (call.kwargs["allowed_mentions"].roles, call.kwargs["allowed_mentions"].everyone) == (False, False)


async def test_final_lists_everyone_past_the_old_top_ten(_isolated_db, h, at):
    await _seed(D2, D3)
    at(halifax(D2, 12))
    for n in range(1, 13):
        await h.submit(disc_uuid=str(n), seconds=60 + n)
    at(halifax(D3, 5))
    await w.tick(h.bot)
    call = next(c for c in h.channel.send.await_args_list if "Today's puzzle is up" in c.args[0])
    assert "<@12>" in call.args[0]
    assert " others" not in call.args[0]
    assert len(_pinged(call.kwargs["allowed_mentions"])) == 12
    with Image.open(call.kwargs["file"].fp) as image:
        assert image.height == 3 * 327 + 4 * 10


async def _crowd(day: date, count: int) -> None:
    """``count`` solves by former members with the longest realistic IDs."""
    for n in range(count):
        await SudokuResult.create(
            day=day,
            disc_uuid=str(1_000_000_000_000_000_000 + n),
            seconds=36000 + n,
            fills=fills(36000 + n),
            finished_at=halifax(D2, 12),
        )


async def test_a_crowded_final_stays_within_discords_limits(_isolated_db, h, at):
    await _seed(D2, D3)
    await _crowd(D2, w.LIST_CAP + 10)
    at(halifax(D3, 5))
    await w.tick(h.bot)
    call = h.channel.send.await_args
    assert len(call.args[0]) <= 2000
    assert "and 10 others" in call.args[0]
    assert len(_pinged(call.kwargs["allowed_mentions"])) == w.LIST_CAP
    assert (await SudokuDay.get(day=D2)).rollup_done is True


async def test_a_crowded_batch_stays_within_discords_limits(_isolated_db, h, at):
    await _seed(D2)
    await _crowd(D2, w.LIST_CAP + 1)
    at(halifax(D2, 14))
    await w.tick(h.bot)
    call = h.channel.send.await_args
    assert len(call.args[0]) <= 2000
    assert ", and 1 other were playing." in call.args[0]
    assert await SudokuResult.filter(announced=False).count() == 0


# --- Notification menu -----------------------------------------------------


async def test_every_kind_of_post_carries_the_notification_button(_isolated_db, h, at):
    await _seed(D2, D3)
    at(halifax(D2, 12))
    await h.submit(disc_uuid="1")
    at(halifax(D2, 14))
    await w.tick(h.bot)
    at(halifax(D3, 5))
    await w.tick(h.bot)
    await _seed(D3 + timedelta(days=1))
    at(halifax(D3 + timedelta(days=1), 5))
    await w.tick(h.bot)
    kwargs = [c.kwargs for c in h.channel.send.await_args_list]
    assert "file" in kwargs[0]  # a batch, then a final, then a final nobody solved
    assert "file" in kwargs[1]
    assert "file" not in kwargs[2]
    for sent in kwargs:
        view = sent["view"]
        assert isinstance(view, w.SudokuNotificationView)
        assert view.is_persistent()
        assert view.children[0].custom_id == "sudoku:notifications"


def _interaction(user_id: int = 1) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


async def _open_menu(user_id: int = 1) -> tuple[str, w._MenuView]:
    interaction = _interaction(user_id)
    await w.SudokuNotificationView().children[0].callback(interaction)
    call = interaction.response.send_message.await_args
    assert call.kwargs["ephemeral"] is True
    return call.args[0], call.kwargs["view"]


async def _click(menu: w._MenuView, index: int) -> tuple[str, w._MenuView]:
    interaction = _interaction()
    await menu.children[index].callback(interaction)
    kwargs = interaction.response.edit_message.await_args.kwargs
    return kwargs["content"], kwargs["view"]


# The menu's items, in order.
SOLVE, DAILY, STREAK, HOURS, MIN_STREAK = range(5)


async def test_menu_shows_the_defaults(_isolated_db):
    text, menu = await _open_menu()
    assert "**Solve posts (off):**" in text
    assert "**Daily results (on):**" in text
    assert "**Streak reminder (off):**" in text
    assert "3 hours before a 3+ day streak" in text
    assert [item.label for item in menu.children[:3]] == [
        "Solve posts: off",
        "Daily results: on",
        "Streak reminder: off",
    ]
    assert [o.value for o in menu.children[HOURS].options if o.default] == ["3"]
    assert [o.value for o in menu.children[MIN_STREAK].options if o.default] == ["3"]


@pytest.mark.parametrize(
    "index, field, default",
    [(SOLVE, "solve_ping", False), (DAILY, "daily_ping", True), (STREAK, "streak_ping", False)],
)
async def test_each_toggle_flips_only_its_own_setting(_isolated_db, index, field, default):
    _, menu = await _open_menu()
    before = await SudokuNotification.get(disc_uuid="1")
    assert getattr(before, field) is default

    _, menu = await _click(menu, index)
    after = await SudokuNotification.get(disc_uuid="1")
    assert getattr(after, field) is (not default)
    for other in {"solve_ping", "daily_ping", "streak_ping"} - {field}:
        assert getattr(after, other) is getattr(before, other)

    await _click(menu, index)
    assert getattr(await SudokuNotification.get(disc_uuid="1"), field) is default


async def test_streak_choices_save_without_turning_the_reminder_on(_isolated_db, monkeypatch):
    _, menu = await _open_menu()
    monkeypatch.setattr(w._SettingSelect, "values", ["1"])
    text, menu = await _click(menu, HOURS)
    assert "1 hour before a 3+ day streak" in text
    monkeypatch.setattr(w._SettingSelect, "values", ["7"])
    text, menu = await _click(menu, MIN_STREAK)
    assert "1 hour before a 7+ day streak" in text
    assert "**Streak reminder (off):**" in text
    assert [o.value for o in menu.children[HOURS].options if o.default] == ["1"]
    assert [o.value for o in menu.children[MIN_STREAK].options if o.default] == ["7"]
    row = await SudokuNotification.get(disc_uuid="1")
    assert (row.streak_ping, row.hours_before, row.min_streak) == (False, 1, 7)


async def test_menu_is_per_player(_isolated_db):
    _, menu = await _open_menu(1)
    await _click(menu, STREAK)
    text, _ = await _open_menu(2)
    assert "**Streak reminder (off):**" in text
    assert (await SudokuNotification.get(disc_uuid="1")).streak_ping is True


# --- Streak reminders ------------------------------------------------------


async def _streak_of(days: int, disc_uuid: str = "1", newest: date = D3) -> None:
    """``days`` solved days ending the day before ``newest``, which is unsolved."""
    for back in range(days, 0, -1):
        day = newest - timedelta(days=back)
        await SudokuDay.create(day=day, givens=GIVENS, difficulty="easy", rollup_done=True)
        await SudokuResult.create(
            day=day, disc_uuid=disc_uuid, seconds=10, fills=fills(10), finished_at=halifax(day, 12), announced=True
        )
    await _seed(newest)


async def _subscribe(disc_uuid: str = "1", enabled: bool = True, hours: int = 3, streak: int = 3) -> None:
    await SudokuNotification.create(disc_uuid=disc_uuid, streak_ping=enabled, hours_before=hours, min_streak=streak)


def _reminders(h: Harness) -> list[str]:
    return [c.args[0] for c in h.channel.send.await_args_list if "expires" in c.args[0]]


# D3's puzzle expires at 04:00 on D3 + 1.
EXPIRY = D3 + timedelta(days=1)


async def test_no_settings_means_no_reminder(_isolated_db, h, at):
    await _streak_of(5)
    at(halifax(EXPIRY, 3, 59))
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()


async def test_reminder_off_stays_quiet(_isolated_db, h, at):
    await _streak_of(5)
    await _subscribe(enabled=False)
    at(halifax(EXPIRY, 3, 59))
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()


async def test_reminder_fires_once_inside_the_window(_isolated_db, h, at):
    await _streak_of(5)
    await _subscribe(hours=3)
    at(halifax(EXPIRY, 0, 59))
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()

    at(halifax(EXPIRY, 1))
    await w.tick(h.bot)
    await w.tick(h.bot)
    h.channel.send.assert_awaited_once()
    call = h.channel.send.await_args
    expires = int(halifax(EXPIRY, 4).timestamp())
    assert call.args[0] == (
        f"<@1> your 5 day sudoku streak expires <t:{expires}:R>. Play it with `/wv extras sudoku` in game."
    )
    assert _pinged(call.kwargs["allowed_mentions"]) == [1]
    assert isinstance(call.kwargs["view"], w.SudokuNotificationView)
    assert (await SudokuNotification.get(disc_uuid="1")).last_reminded_day == D3

    # Changing the settings doesn't ping again the same day.
    await SudokuNotification.filter(disc_uuid="1").update(hours_before=1)
    at(halifax(EXPIRY, 3, 30))
    await w.tick(h.bot)
    h.channel.send.assert_awaited_once()


async def test_reminder_respects_the_minimum_streak(_isolated_db, h, at):
    await _streak_of(4)
    await _subscribe(streak=5)
    at(halifax(EXPIRY, 2))
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()
    await SudokuNotification.filter(disc_uuid="1").update(min_streak=4)
    await w.tick(h.bot)
    h.channel.send.assert_awaited_once()


async def test_solving_first_means_no_reminder(_isolated_db, h, at):
    await _streak_of(5)
    await _subscribe()
    at(halifax(D3, 12))
    await h.submit(day=D3)
    at(halifax(EXPIRY, 2))
    await w.tick(h.bot)
    assert _reminders(h) == []


async def test_a_broken_streak_gets_no_reminder(_isolated_db, h, at):
    await _streak_of(5)
    await SudokuResult.filter(day=D2).delete()
    await _subscribe(streak=1)
    at(halifax(EXPIRY, 2))
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()


async def test_no_reminder_at_or_after_expiry(_isolated_db, h, at, monkeypatch):
    monkeypatch.setattr(sudoku, "generate_givens", AsyncMock(side_effect=sudoku.GenerationError("down")))
    await _streak_of(5)
    await _subscribe()
    at(halifax(EXPIRY, 4))
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()


async def test_a_new_day_reminds_again(_isolated_db, h, at):
    await _streak_of(5)
    await _subscribe()
    at(halifax(EXPIRY, 2))
    await w.tick(h.bot)
    await SudokuResult.create(
        day=D3, disc_uuid="1", seconds=10, fills=fills(10), finished_at=halifax(D3, 12), announced=True
    )
    await _seed(EXPIRY)
    at(halifax(EXPIRY + timedelta(days=1), 2))
    await w.tick(h.bot)
    reminders = _reminders(h)
    assert len(reminders) == 2
    assert "your 6 day" in reminders[1]


async def test_failed_reminder_retries_next_tick(_isolated_db, h, at):
    await _streak_of(5)
    await _subscribe()
    at(halifax(EXPIRY, 2))
    h.channel.send.side_effect = _http_error()
    await w.tick(h.bot)
    assert (await SudokuNotification.get(disc_uuid="1")).last_reminded_day is None
    h.channel.send.side_effect = None
    await w.tick(h.bot)
    assert (await SudokuNotification.get(disc_uuid="1")).last_reminded_day == D3


async def test_former_member_gets_no_reminder(_isolated_db, h, at):
    await _streak_of(5, disc_uuid="999")
    await _subscribe(disc_uuid="999")
    at(halifax(EXPIRY, 2))
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()


async def test_channel_zero_stops_reminders(_isolated_db, h, at, monkeypatch):
    await _streak_of(5)
    await _subscribe()
    monkeypatch.setattr(w.CurrConfig, "SUDOKU_CHANNEL", 0)
    at(halifax(EXPIRY, 2))
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()
    assert (await SudokuNotification.get(disc_uuid="1")).last_reminded_day is None

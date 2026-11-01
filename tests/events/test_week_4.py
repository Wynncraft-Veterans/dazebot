"""Tests for ``cogs.events.returns.week_4`` (the daily sudoku).

Discord is mocked and HoDoKu is replaced at ``sudoku.generate_givens``. The
tables are real: each test bootstraps a fresh DB through ``init_db`` (the
``_isolated_db`` pattern from ``tests/test_migrations.py``), which also proves
the migration applies. The routes are called directly, since the project has no
HTTP test client installed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from fastapi import HTTPException
from PIL import Image
from tortoise.exceptions import IntegrityError

from api.main import create_app
from cogs.events.returns import REGISTRY, TICK_REGISTRY
from cogs.events.returns import week_4 as w
from cogs.events.returns.lib import sudoku
from orm import SudokuDay, SudokuResult, init_db

SOLVED = "534678912672195348198342567859761423426853791713924856961537284287419635345286179"
GIVENS = "".join(c if i % 2 else "0" for i, c in enumerate(SOLVED))
SECRET = "test-secret"
CHANNEL = 111111111111111111
MESSAGE = 222222222222222222

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
        self.message = MagicMock(id=MESSAGE)
        self.message.edit = AsyncMock()
        self.channel = MagicMock(id=CHANNEL)
        self.channel.send = AsyncMock(return_value=self.message)
        self.channel.fetch_message = AsyncMock(return_value=self.message)
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
    monkeypatch.setattr(sudoku, "pick_difficulty", lambda day: "medium")
    await w.tick(h.bot)
    await w.tick(h.bot)
    generate.assert_awaited_once_with("medium")
    row = await SudokuDay.get(day=D2)
    assert (row.givens, row.difficulty, row.rollup_done) == (GIVENS, "medium", False)


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


# --- Live post -------------------------------------------------------------


async def test_live_post_sends_then_edits_only_when_the_count_changes(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()

    await h.submit(disc_uuid="1", seconds=90)
    await w.tick(h.bot)
    h.channel.send.assert_awaited_once()
    kwargs = h.channel.send.await_args.kwargs
    assert kwargs["file"].filename == f"sudoku-{D2}-1.png"
    assert kwargs["allowed_mentions"].users is False
    assert kwargs["allowed_mentions"].roles is False
    assert kwargs["allowed_mentions"].everyone is False
    text = h.sent()
    assert "Your group is on a 1 day streak!" in text
    assert "<@1>" in text
    assert "1:30" in text
    assert "`/wv extras sudoku`" in text
    row = await SudokuDay.get(day=D2)
    assert (row.channel_id, row.live_message_id, row.live_count) == (CHANNEL, MESSAGE, 1)

    await w.tick(h.bot)
    h.message.edit.assert_not_awaited()

    await h.submit(disc_uuid="2", seconds=60)
    await w.tick(h.bot)
    h.channel.send.assert_awaited_once()
    kwargs = h.message.edit.await_args.kwargs
    assert kwargs["attachments"][0].filename == f"sudoku-{D2}-2.png"
    assert kwargs["content"].index("<@2>") < kwargs["content"].index("<@1>")
    assert kwargs["allowed_mentions"].users is False
    assert (await SudokuDay.get(day=D2)).live_count == 2


async def test_live_post_caps_at_ten_with_others(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    for n in range(1, 13):
        await h.submit(disc_uuid=str(n), seconds=60 + n)
    await w.tick(h.bot)
    text = h.sent()
    assert "<@10>" in text
    assert "<@11>" not in text
    assert "and 2 others" in text
    with Image.open(h.channel.send.await_args.kwargs["file"].fp) as image:
        assert image.height == 5 * 134 + 6 * 10


async def test_medium_day_shows_the_multiplier(_isolated_db, h, at):
    await _seed(D2, difficulty="medium")
    at(halifax(D2, 12))
    await h.submit()
    await w.tick(h.bot)
    assert "Medium (x3 score)" in h.sent()


async def test_easy_day_shows_no_multiplier(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    await h.submit()
    await w.tick(h.bot)
    assert "Difficulty: Easy\n" in h.sent()


async def test_deleted_live_post_is_cleared_then_reposted(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    await h.submit(disc_uuid="1")
    await w.tick(h.bot)
    await h.submit(disc_uuid="2")
    h.channel.fetch_message.side_effect = _http_error(discord.NotFound, 404)
    await w.tick(h.bot)
    row = await SudokuDay.get(day=D2)
    assert (row.channel_id, row.live_message_id, row.live_count) == (None, None, 0)
    await w.tick(h.bot)
    assert h.channel.send.await_count == 2
    assert (await SudokuDay.get(day=D2)).live_message_id == MESSAGE


async def test_live_http_error_retries_next_tick(_isolated_db, h, at):
    await _seed(D2)
    at(halifax(D2, 12))
    await h.submit()
    h.channel.send.side_effect = _http_error()
    await w.tick(h.bot)
    assert (await SudokuDay.get(day=D2)).live_message_id is None
    h.channel.send.side_effect = None
    await w.tick(h.bot)
    assert (await SudokuDay.get(day=D2)).live_message_id == MESSAGE


async def test_channel_zero_stops_posting_but_not_the_puzzle(_isolated_db, h, at, monkeypatch):
    monkeypatch.setattr(w.CurrConfig, "SUDOKU_CHANNEL", 0)
    monkeypatch.setattr(sudoku, "generate_givens", AsyncMock(return_value=GIVENS))
    await _seed(D2)
    at(halifax(D2, 12))
    await h.submit()
    await w.tick(h.bot)
    at(halifax(D3, 5))
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()
    assert await SudokuDay.exists(day=D3)
    # Closed silently, so turning posting back on doesn't flood the channel.
    assert (await SudokuDay.get(day=D2)).rollup_done is True
    monkeypatch.setattr(w.CurrConfig, "SUDOKU_CHANNEL", CHANNEL)
    await w.tick(h.bot)
    h.channel.send.assert_not_awaited()


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
    assert call.kwargs["allowed_mentions"].users is False


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
    await SudokuResult.create(day=D1, disc_uuid="1", seconds=10, fills=fills(10), finished_at=halifax(D1, 12))
    at(halifax(D3, 5))
    await w.tick(h.bot)
    assert h.sent() == (
        "The group's 1 day streak has ended.\nToday's puzzle is up: `/wv extras sudoku` in game."
    )


async def test_final_is_independent_of_a_failing_live_edit(_isolated_db, h, at):
    await _seed(D2, D3)
    at(halifax(D2, 12))
    await h.submit(disc_uuid="1")
    await w.tick(h.bot)
    await h.submit(disc_uuid="2")
    h.channel.fetch_message.side_effect = _http_error()
    at(halifax(D3, 5))
    await w.tick(h.bot)
    assert len(await _final_sends(h)) == 1
    assert (await SudokuDay.get(day=D2)).rollup_done is True


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
    await _seed(D1, D3)
    at(halifax(D1, 12))
    await h.submit(day=D1)
    at(halifax(D3, 12))
    await h.submit(day=D3)
    await w.tick(h.bot)
    assert "on a 2 day streak" in h.sent()

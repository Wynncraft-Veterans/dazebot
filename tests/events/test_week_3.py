"""Tests for ``cogs.events.returns.week_3`` turn rollover.

Discord is mocked out (dashboard refreshes and DM alerts); the Return3
rows are real, on a fresh returns DB bootstrapped through ``init_db``
(the ``_isolated_db`` pattern from ``tests/test_migrations.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.events.returns import week_3 as w
from orm import init_db
from orm_returns import Return3GameState, Return3Tile

ORDER = "fishcult,deercult,wencult,nazcult"


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


@pytest.fixture
def discord_calls(monkeypatch):
    refresh = AsyncMock()
    notify = AsyncMock()
    monkeypatch.setattr(w, "_refresh_all_dashboards", refresh)
    monkeypatch.setattr(w, "_notify_subscribers", notify)
    monkeypatch.setattr(w, "_notify_capture", AsyncMock())
    return refresh, notify


async def _seed(turn: int, owners: dict[str, int], *, started_at=None) -> None:
    """Capitals of ``owners`` hold the given armies; every other tile,
    including the capitals of absent cults, is neutral and empty."""
    rows = []
    for tid in range(w.TILE_COUNT):
        cult = next((c for c, cap in w.CAPITAL_TILE.items() if cap == tid and c in owners), None)
        rows.append(Return3Tile(id=tid, controlling_cult=cult, army_count=owners.get(cult, 0)))
    await Return3Tile.bulk_create(rows)
    now = datetime.now(timezone.utc)
    await Return3GameState.create(
        id=1, phase="active", started_at=now, turn_order_csv=ORDER,
        current_turn_number=turn, turn_started_at=started_at or now,
    )


async def _seed_map(turn: int, owners_by_tile: dict[int, str], **state_kw) -> None:
    """Arbitrary ownership: listed tiles hold 5 armies, the rest are
    neutral and empty."""
    await Return3Tile.bulk_create([
        Return3Tile(
            id=tid,
            controlling_cult=owners_by_tile.get(tid),
            army_count=5 if tid in owners_by_tile else 0,
        )
        for tid in range(w.TILE_COUNT)
    ])
    now = datetime.now(timezone.utc)
    await Return3GameState.create(
        id=1, phase="active", started_at=now, turn_order_csv=ORDER,
        current_turn_number=turn, turn_started_at=now, **state_kw,
    )


def _split(**sizes: int) -> dict[int, str]:
    """Hand out tiles 0, 1, 2, … to cults in the given amounts."""
    out: dict[int, str] = {}
    tid = 0
    for cult, n in sizes.items():
        for _ in range(n):
            out[tid] = cult
            tid += 1
    return out


def test_dominance_target_schedule():
    assert w.DOMINANCE_DECAY_START_TURN % 4 == 0
    start = w.DOMINANCE_DECAY_START_TURN
    assert w._dominance_threshold(start - 4) == w.DOMINANCE_TILE_THRESHOLD
    assert w._dominance_threshold(start) == 15
    assert w._dominance_threshold(start + 4) == 15
    assert w._dominance_threshold(start + 8) == 14
    assert w._dominance_threshold(start + 24) == 12
    assert w._dominance_threshold(start + 4000) == 1
    # Displays read the boundary that ends the current loop.
    assert w._next_boundary(start - 4) == start
    assert w._next_boundary(start - 1) == start


async def test_lowered_target_wins_with_a_strict_lead(_isolated_db, discord_calls):
    # Boundary start+24 judges at 12. Turn start+23 is nazcult's (skipped),
    # so _roll_turn lands straight on it with no random action in between.
    turn = w.DOMINANCE_DECAY_START_TURN + 23
    await _seed_map(
        turn, _split(fishcult=12, deercult=7, wencult=6),
        dominance_leader_cult="fishcult", dominance_streak_loops=1,
    )
    state = await w._load_state()

    assert await w._roll_turn(MagicMock(), state)

    state = await w._load_state()
    assert state.phase == "ended"
    assert state.winner_cult == "fishcult"


async def test_tied_lead_does_not_count(_isolated_db, discord_calls):
    turn = w.DOMINANCE_DECAY_START_TURN + 23
    await _seed_map(
        turn, _split(fishcult=12, deercult=12, wencult=1),
        dominance_leader_cult="fishcult", dominance_streak_loops=1,
    )
    state = await w._load_state()

    assert not await w._roll_turn(MagicMock(), state)

    assert state.phase == "active"
    assert state.dominance_leader_cult is None
    assert state.dominance_streak_loops == 0


async def test_announce_endgame_posts_to_every_thread(_isolated_db, monkeypatch):
    await _seed(172, {"fishcult": 5, "deercult": 5, "wencult": 5})
    thread = MagicMock()
    thread.send = AsyncMock()
    monkeypatch.setattr(w, "_resolve_thread", AsyncMock(return_value=thread))
    feedback = AsyncMock()
    monkeypatch.setattr(w, "send_feedback", feedback)

    await w._manage_announce_endgame(MagicMock(), [])

    assert thread.send.await_count == len(w.ALL_CULTS)
    for call in thread.send.await_args_list:
        assert "endgame" in call.args[0]
        assert "loop 44" in call.args[0]
    assert "Failed" not in feedback.await_args.args[1]


async def test_eliminated_cult_turn_is_skipped(_isolated_db, discord_calls):
    _, notify = discord_calls
    # Turn index 122 is wencult's; nazcult (next) holds no tiles.
    await _seed(122, {"fishcult": 5, "deercult": 5, "wencult": 5})
    state = await w._load_state()

    await w._advance_one_turn(MagicMock(), state)

    state = await w._load_state()
    assert state.current_turn_number == 124
    assert w._current_cult(state) == "fishcult"
    notify.assert_awaited_once()
    assert notify.await_args.args[1] == "fishcult"
    # The skip crossed the loop boundary, so growth still ran.
    fish_cap = await Return3Tile.get(id=w.CAPITAL_TILE["fishcult"])
    assert fish_cap.army_count == 5 + w.LOOP_GROWTH


async def test_tick_passes_an_eliminated_cults_turn_at_once(_isolated_db, discord_calls):
    # nazcult is already active (turn index 123) with a fresh deadline.
    await _seed(123, {"fishcult": 5, "deercult": 5, "wencult": 5})

    await w.tick(MagicMock())

    state = await w._load_state()
    assert state.current_turn_number == 124
    assert w._current_cult(state) == "fishcult"


async def test_tick_waits_out_a_live_cults_turn(_isolated_db, discord_calls):
    await _seed(122, {"fishcult": 5, "deercult": 5, "wencult": 5})

    await w.tick(MagicMock())

    state = await w._load_state()
    assert state.current_turn_number == 122


async def test_last_cult_standing_wins_at_the_loop_boundary(_isolated_db, discord_calls):
    # fishcult's turn (index 120) and it's the only cult left: the three
    # skipped turns run to the boundary at 124, where elimination ends it.
    await _seed(120, {"fishcult": 5})
    state = await w._load_state()

    await w._advance_one_turn(MagicMock(), state)

    state = await w._load_state()
    assert state.phase == "ended"
    assert state.winner_cult == "fishcult"
    assert state.current_turn_number == 124

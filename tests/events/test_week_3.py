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

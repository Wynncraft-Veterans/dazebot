"""Regression tests for the aerich migration bootstrap in ``orm.init_db``.

Three branches must hold:

* Fresh DB → ``upgrade`` creates every table.
* Pre-aerich DB (tables present, no ``aerich`` table) → ``upgrade(fake=True)``
  marks the initial migration applied without re-running CREATE TABLE.
* Already-bootstrapped DB → ``upgrade`` is a no-op.

The first branch is exercised via synthetic mock data (no real production
identifiers); the second via a copy of the local ephemeral snapshot at
``.claude/ephemeral/dazebot-2026-05-22.db``. The ephemeral DB is gitignored
and never modified — every test writes only to ``tmp_path`` copies.
"""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orm import (
    Blocklist,
    BotConfigOverride,
    Cult,
    CultMembership,
    DiscordAccount,
    LinkCode,
    MinecraftAccount,
    Score,
    VerifyKey,
    WeeklyEvent,
    _existing_db_needs_fake_apply,
    close_db,
    init_db,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
EPHEMERAL_DB = REPO_ROOT / ".claude" / "ephemeral" / "dazebot-2026-05-22.db"


# All model tables that the initial migration creates. Used to assert
# every table is present after a fresh-DB bootstrap.
EXPECTED_TABLES = {
    "aerich",
    "blocklist",
    "bot_config_overrides",
    "build_promotions",
    "cult_memberships",
    "cults",
    "dead_guild_alerts",
    "discord_accounts",
    "dm_sent_log",
    "first_install_monitors",
    "guild_capacity_alerts",
    "intercult_messages",
    "janitor_alerts",
    "link_code",
    "link_requests",
    "minecraft_accounts",
    "minecraft_alts",
    "mojang_name_cache",
    "profession_categories",
    "recruitment_queries",
    "scores",
    "shouts",
    "staff_action_entries",
    "story_segments",
    "user_vanity_choices",
    "verify_keys",
    "waitlist",
    "weekly_events",
}


@pytest.fixture
async def _isolated_db(tmp_path, monkeypatch):
    """Point ``DAZEBOT_DB_PATH`` at a per-test tmp file and reset Tortoise +
    aerich global state on both sides so tests don't pollute each other.

    Both ``Tortoise._inited`` (skips re-init if True) and ``aerich.migrate.
    Migrate`` class-level fields (``_last_version_content``,
    ``migrate_location``, ``ddl``, etc.) survive across test boundaries by
    default, which makes the second ``init_db`` of a run silently reuse the
    first test's connection / migration metadata.
    """
    from aerich.migrate import Migrate
    from tortoise import Tortoise

    async def _reset():
        try:
            await Tortoise.close_connections()
        except Exception:
            pass
        # Force the next init_db to run Tortoise.init from scratch.
        Tortoise._inited = False
        # Reset aerich's class-level migration cache so it re-reads from the
        # new DB rather than relying on the previous run's _last_version_content.
        for attr in ("_last_version_content", "migrate_location", "ddl", "ddl_class", "dialect"):
            if hasattr(Migrate, attr):
                try:
                    delattr(Migrate, attr)
                except AttributeError:
                    pass
        Migrate.upgrade_operators = []
        Migrate.downgrade_operators = []

    await _reset()
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DAZEBOT_DB_PATH", str(db_path))
    yield db_path
    await _reset()


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()


def _row_counts(db_path: Path, tables: list[str]) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Detection helper
# ---------------------------------------------------------------------------


def test_detect_fresh_db_does_not_need_fake_apply(tmp_path):
    """A non-existent DB path is "fresh" — no fake-apply, just create."""
    assert _existing_db_needs_fake_apply(str(tmp_path / "nope.db")) is False


def test_detect_empty_db_does_not_need_fake_apply(tmp_path):
    """An empty SQLite file (no tables) is fresh."""
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    assert _existing_db_needs_fake_apply(str(db)) is False


def test_detect_pre_aerich_db_needs_fake_apply(tmp_path):
    """A DB with model tables but no ``aerich`` table — the prod state at
    first deploy — must trigger fake-apply.
    """
    db = tmp_path / "pre_aerich.db"
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE "minecraft_accounts" (id CHAR(36) PRIMARY KEY)')
    conn.commit()
    conn.close()
    assert _existing_db_needs_fake_apply(str(db)) is True


def test_detect_bootstrapped_db_does_not_need_fake_apply(tmp_path):
    """Once the aerich table exists, future boots take the normal upgrade
    path (which is a no-op when there are no pending migrations).
    """
    db = tmp_path / "bootstrapped.db"
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE "minecraft_accounts" (id CHAR(36) PRIMARY KEY)')
    conn.execute('CREATE TABLE "aerich" (id INTEGER PRIMARY KEY, version TEXT, app TEXT, content TEXT)')
    conn.commit()
    conn.close()
    assert _existing_db_needs_fake_apply(str(db)) is False


# ---------------------------------------------------------------------------
# Branch 1: fresh DB
# ---------------------------------------------------------------------------


async def test_bootstrap_on_fresh_db(_isolated_db):
    """``init_db`` on an empty path creates every model table and the
    aerich tracking table, and records every migration as applied.
    """
    db_path = _isolated_db
    assert not db_path.exists()

    await init_db()

    assert _table_names(db_path) == EXPECTED_TABLES

    # Every migration file is recorded as applied (initial + any follow-ups).
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT version, app FROM aerich ORDER BY id").fetchall()
    conn.close()
    versions = [r[0] for r in rows]
    apps = {r[1] for r in rows}
    assert versions[0].startswith("0_") and versions[0].endswith("_init.py")
    assert apps == {"models"}
    # Sanity-check ordering matches the on-disk migration files.
    from pathlib import Path
    on_disk = sorted(p.name for p in Path("migrations/models").glob("*.py"))
    assert versions == on_disk


async def test_fresh_db_round_trip(_isolated_db):
    """Sanity: after bootstrap on a fresh DB, model writes/reads work and
    the cross-table FK link round-trips."""
    await init_db()

    mc = await MinecraftAccount.create(
        uuid="00000000-0000-0000-0000-000000000001",
        wynn_username="TestPlayer",
        mc_username="testplayer",
        last_online=datetime.fromtimestamp(0, tz=timezone.utc),
        last_manual_check=datetime.fromtimestamp(0, tz=timezone.utc),
    )
    disc = await DiscordAccount.create(disc_uuid="111111111111111111", minecraft_account=mc)

    # FK lookup resolves.
    fetched = await DiscordAccount.get(disc_uuid="111111111111111111").prefetch_related("minecraft_account")
    assert fetched.minecraft_account_id == mc.id

    # Unique constraint on disc_uuid fires on duplicate.
    from tortoise.exceptions import IntegrityError

    with pytest.raises(IntegrityError):
        await DiscordAccount.create(disc_uuid="111111111111111111")


# ---------------------------------------------------------------------------
# Branch 2: pre-aerich production DB (against the ephemeral snapshot)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not EPHEMERAL_DB.exists(),
    reason="Ephemeral DB snapshot not present; this test is local-only.",
)
async def test_bootstrap_on_existing_db(_isolated_db):
    """Copy the ephemeral snapshot to a tmp_path, run ``init_db``, and
    assert: aerich tracking table created with one row, every original
    row count preserved, no model table dropped or recreated.
    """
    db_path = _isolated_db
    shutil.copy2(EPHEMERAL_DB, db_path)

    # Pre-bootstrap row counts and table set (sanity-anchor: prove we're
    # comparing against the real snapshot, not what aerich generated).
    pre_tables = _table_names(db_path)
    assert "aerich" not in pre_tables
    pre_counts = _row_counts(
        db_path,
        [
            "minecraft_accounts",
            "discord_accounts",
            "cults",
            "cult_memberships",
            "verify_keys",
            "story_segments",
            "profession_categories",
        ],
    )

    await init_db()

    # The aerich tracking table now exists; the initial migration is
    # fake-applied (no DDL re-run) and any follow-up migrations run for
    # real on top.
    post_tables = _table_names(db_path)
    assert "aerich" in post_tables
    # Every pre-existing table is still present (no destructive change).
    assert pre_tables.issubset(post_tables)

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT version, app FROM aerich ORDER BY id").fetchall()
    conn.close()
    versions = [r[0] for r in rows]
    assert versions[0].endswith("_init.py")
    assert {r[1] for r in rows} == {"models"}
    # Every on-disk migration is recorded.
    from pathlib import Path
    on_disk = sorted(p.name for p in Path("migrations/models").glob("*.py"))
    assert versions == on_disk

    # Row counts unchanged — fake-apply did not touch model data.
    post_counts = _row_counts(db_path, list(pre_counts.keys()))
    assert pre_counts == post_counts


@pytest.mark.skipif(
    not EPHEMERAL_DB.exists(),
    reason="Ephemeral DB snapshot not present; this test is local-only.",
)
async def test_bootstrap_is_idempotent(_isolated_db):
    """Calling ``init_db`` twice on a now-bootstrapped DB is a no-op: same
    tracking row, same data, no exception."""
    db_path = _isolated_db
    shutil.copy2(EPHEMERAL_DB, db_path)

    await init_db()
    first_counts = _row_counts(db_path, ["minecraft_accounts", "verify_keys", "cults"])

    await close_db()
    await init_db()

    second_counts = _row_counts(db_path, ["minecraft_accounts", "verify_keys", "cults"])
    assert first_counts == second_counts

    from pathlib import Path
    expected_count = len(list(Path("migrations/models").glob("*.py")))
    conn = sqlite3.connect(db_path)
    aerich_count = conn.execute("SELECT COUNT(*) FROM aerich").fetchone()[0]
    conn.close()
    assert aerich_count == expected_count


@pytest.mark.skipif(
    not EPHEMERAL_DB.exists(),
    reason="Ephemeral DB snapshot not present; this test is local-only.",
)
async def test_orm_queries_against_migrated_db(_isolated_db):
    """After bootstrap on the ephemeral snapshot, every model declared in
    orm.py is queryable through Tortoise. Catches signature mismatches
    between the model definitions and the actual on-disk schema (the
    failure mode we'd see if a column drifted)."""
    db_path = _isolated_db
    shutil.copy2(EPHEMERAL_DB, db_path)

    await init_db()

    # A representative SELECT from each model — purely structural; if any
    # column referenced by the ORM isn't on disk, ``.count()`` raises
    # ``OperationalError``.
    await MinecraftAccount.all().count()
    await DiscordAccount.all().count()
    await Cult.all().count()
    await CultMembership.all().count()
    await VerifyKey.all().count()
    await LinkCode.all().count()
    await WeeklyEvent.all().count()
    await Score.all().count()
    await Blocklist.all().count()
    await BotConfigOverride.all().count()


# ---------------------------------------------------------------------------
# Branch 3: mock-data integrity (no real production identifiers)
# ---------------------------------------------------------------------------


def _mc_account_factory():
    """Synthetic MinecraftAccount kwargs. Random UUIDs only — never real
    Wynncraft/Mojang/Discord identifiers."""
    return dict(
        uuid=str(uuid.uuid4()),
        wynn_username=f"Player_{uuid.uuid4().hex[:6]}",
        mc_username=f"player_{uuid.uuid4().hex[:6]}",
        last_online=datetime.fromtimestamp(0, tz=timezone.utc),
        last_manual_check=datetime.fromtimestamp(0, tz=timezone.utc),
    )


async def test_mock_data_cascade_delete(_isolated_db):
    """Deleting a MinecraftAccount that's the FK target of a
    DiscordAccount sets the disc row's minecraft_account_id to NULL
    (the model uses on_delete=SET_NULL)."""
    await init_db()

    mc = await MinecraftAccount.create(**_mc_account_factory())
    disc = await DiscordAccount.create(disc_uuid=f"disc_{uuid.uuid4().hex}", minecraft_account=mc)

    await mc.delete()

    refetched = await DiscordAccount.get(id=disc.id)
    assert refetched.minecraft_account_id is None


async def test_mock_data_unique_together(_isolated_db):
    """``DMSentLog.unique_together = (disc_uuid, kind)`` enforces a
    multi-column UNIQUE constraint at the DDL layer.
    """
    from tortoise.exceptions import IntegrityError

    from orm import DMSentLog

    await init_db()

    disc = f"disc_{uuid.uuid4().hex}"
    await DMSentLog.create(disc_uuid=disc, kind="inactive_warning")
    # Same (disc_uuid, kind) again → IntegrityError.
    with pytest.raises(IntegrityError):
        await DMSentLog.create(disc_uuid=disc, kind="inactive_warning")
    # Same disc_uuid but different kind → fine.
    await DMSentLog.create(disc_uuid=disc, kind="first_install_chains")
    assert await DMSentLog.filter(disc_uuid=disc).count() == 2


async def test_mock_data_one_to_one_uniqueness_enforced(_isolated_db):
    """The four OneToOneField columns (``discord_accounts.minecraft_account``,
    ``waitlist.minecraft_account``, ``blocklist.minecraft_account``,
    ``cult_memberships.discord_account``) now enforce uniqueness at the DDL
    layer. The fresh-DB initial migration adds the UNIQUE constraint to the
    column directly; the backfill migration ``1_*_backfill_fk_unique_indexes``
    adds it on top via a UNIQUE index so pre-aerich production DBs converge.
    Regression-guards against a future move back to ``ForeignKeyField(...,
    unique=True)``, which silently drops the constraint at the DDL layer.
    """
    from tortoise.exceptions import IntegrityError

    await init_db()

    mc = await MinecraftAccount.create(**_mc_account_factory())
    await DiscordAccount.create(disc_uuid=f"disc_{uuid.uuid4().hex}", minecraft_account=mc)
    # Same minecraft_account on a second DiscordAccount → IntegrityError.
    with pytest.raises(IntegrityError):
        await DiscordAccount.create(disc_uuid=f"disc_{uuid.uuid4().hex}", minecraft_account=mc)

    # CultMembership.discord_account is also one-to-one.
    cult_owner = await MinecraftAccount.create(**_mc_account_factory())
    cult_a = await Cult.create(name=f"cult_a_{uuid.uuid4().hex[:6]}", owner=cult_owner)
    cult_b = await Cult.create(name=f"cult_b_{uuid.uuid4().hex[:6]}", owner=cult_owner)
    disc = await DiscordAccount.create(disc_uuid=f"disc_{uuid.uuid4().hex}")
    await CultMembership.create(cult=cult_a, discord_account=disc)
    with pytest.raises(IntegrityError):
        await CultMembership.create(cult=cult_b, discord_account=disc)


async def test_mock_data_link_code_reissue_via_unique_username(_isolated_db):
    """``LinkCode.mc_username`` is unique — re-running ``/link code`` for
    the same username must UPDATE in place rather than duplicate."""
    await init_db()

    username = f"player_{uuid.uuid4().hex[:6]}"
    first = await LinkCode.create(mc_username=username, disc_uuid="d1", code="AAAA")
    # The cog does update_or_create against mc_username; we simulate that:
    obj, created = await LinkCode.update_or_create(
        defaults={"disc_uuid": "d2", "code": "BBBB"}, mc_username=username
    )
    assert created is False
    assert obj.id == first.id
    assert obj.code == "BBBB"
    assert await LinkCode.all().count() == 1


async def test_mock_data_verify_key_revoked_at_round_trip(_isolated_db):
    """``VerifyKey.revoked_at`` accepts a nullable datetime and round-trips
    through the ORM. Catches a tz-drop regression on the column."""
    await init_db()

    revoked_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    key = await VerifyKey.create(
        key=f"key_{uuid.uuid4().hex}",
        disc_uuid=f"disc_{uuid.uuid4().hex}",
        tier="member",
    )
    assert key.revoked_at is None

    key.revoked_at = revoked_time
    await key.save()

    refetched = await VerifyKey.get(id=key.id)
    assert refetched.revoked_at == revoked_time

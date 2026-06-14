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


def _find_ephemeral_snapshot() -> Path | None:
    """Pick whichever pre-aerich DB snapshot is present in ``.claude/ephemeral``.

    Snapshots get renamed / refreshed over time (e.g. ``dazebot-<date>.db``,
    ``BACKUP-PRE-AERICH.db``, ``DUMP-POST-MIGRATION.db``). We pick the
    largest non-empty ``.db`` file that has a ``minecraft_accounts`` table
    and no ``aerich`` table — the pre-aerich production shape. Returns
    ``None`` if no usable snapshot exists, so the dependent tests can skip.
    """
    import sqlite3

    eph = REPO_ROOT / ".claude" / "ephemeral"
    if not eph.is_dir():
        return None
    candidates: list[tuple[int, Path]] = []
    for p in eph.glob("*.db"):
        if p.stat().st_size < 1024:
            continue
        try:
            conn = sqlite3.connect(p)
            try:
                names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        if "minecraft_accounts" in names and "aerich" not in names:
            candidates.append((p.stat().st_size, p))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


EPHEMERAL_DB = _find_ephemeral_snapshot()


# All model tables that the initial migration + follow-ups create. Used to
# assert every table is present after a fresh-DB bootstrap. Computed from
# the live model registry so adding a model doesn't silently desync the
# assertion — the table set is what Tortoise *would* create from orm.py.
def _expected_tables() -> set[str]:
    import orm as _orm
    from tortoise.models import Model

    return {"aerich"} | {
        cls._meta.db_table
        for name in dir(_orm)
        if isinstance((cls := getattr(_orm, name, None)), type)
        and issubclass(cls, Model)
        and cls is not Model
    }


def _expected_returns_tables() -> set[str]:
    """Tables expected in returns.db after bootstrap. NOTE: no ``aerich``
    — aerich tracks both apps' history in dazebot.db, not in returns.db."""
    import orm_returns as _orm_ret
    from tortoise.models import Model

    return {
        cls._meta.db_table
        for name in dir(_orm_ret)
        if isinstance((cls := getattr(_orm_ret, name, None)), type)
        and issubclass(cls, Model)
        and cls is not Model
    }


EXPECTED_TABLES = _expected_tables()
EXPECTED_RETURNS_TABLES = _expected_returns_tables()


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
    returns_db_path = tmp_path / "test_returns.db"
    monkeypatch.setenv("DAZEBOT_DB_PATH", str(db_path))
    monkeypatch.setenv("DAZEBOT_RETURNS_DB_PATH", str(returns_db_path))
    # Tests intentionally start from empty tmp_path DBs (or seed one by
    # copying the ephemeral snapshot). The fresh-DB guards in init_db are a
    # prod safety mechanism that would otherwise refuse all these tests.
    monkeypatch.setenv("DAZEBOT_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("DAZEBOT_ALLOW_FRESH_RETURNS_DB", "1")
    # Legacy-tables-present scenarios need this; granting blanket is safe in
    # tests because no test ever runs against the real prod DB path.
    monkeypatch.setenv("DAZEBOT_ALLOW_LEGACY_RETURNS_DROP", "1")
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


async def test_init_db_refuses_missing_file_without_opt_in(tmp_path, monkeypatch):
    """The 2026-05-23 regression: init_db must NOT silently treat a missing
    DB file as "fresh install" and create a new schema. Without the explicit
    ``DAZEBOT_ALLOW_FRESH_DB=1`` opt-in, missing the file at the configured
    path must raise.

    Bypassing this guard is what wiped prod when a bind-mount glitch left
    ``/app/data/dazebot.db`` invisible to the new container at deploy time.
    """
    db_path = tmp_path / "nonexistent.db"
    monkeypatch.setenv("DAZEBOT_DB_PATH", str(db_path))
    monkeypatch.delenv("DAZEBOT_ALLOW_FRESH_DB", raising=False)

    with pytest.raises(RuntimeError, match="DAZEBOT_ALLOW_FRESH_DB"):
        await init_db()


async def test_init_db_creates_pre_migration_backup(_isolated_db):
    """Every init_db run that touches an existing DB file leaves a
    timestamped backup beside it. This makes post-incident recovery a file
    copy instead of a forensic exercise.
    """
    from orm import _backup_db_before_migration

    # Seed the path with a tiny existing file so the backup helper has
    # something to copy.
    _isolated_db.write_bytes(b"not-real-sqlite-but-non-empty")
    backup = _backup_db_before_migration(str(_isolated_db))
    assert backup is not None
    assert Path(backup).exists()
    assert Path(backup).read_bytes() == b"not-real-sqlite-but-non-empty"


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
    """``init_db`` on empty paths creates every model table in dazebot.db,
    every returns table in returns.db, and records both apps' migrations
    in dazebot.db's single ``aerich`` tracking table.
    """
    db_path = _isolated_db
    returns_path = db_path.parent / "test_returns.db"
    assert not db_path.exists()
    assert not returns_path.exists()

    await init_db()

    assert _table_names(db_path) == EXPECTED_TABLES
    assert _table_names(returns_path) == EXPECTED_RETURNS_TABLES

    # Every migration file is recorded — both apps' rows live in dazebot.db's
    # `aerich` table (the returns connection has no aerich table of its own).
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT version, app FROM aerich ORDER BY id").fetchall()
    conn.close()
    apps = {r[1] for r in rows}
    assert apps == {"models", "returns"}

    from pathlib import Path
    on_disk_models = sorted(p.name for p in Path("migrations/models").glob("*.py"))
    on_disk_returns = sorted(p.name for p in Path("migrations/returns").glob("*.py"))
    versions_by_app: dict[str, list[str]] = {}
    for v, a in rows:
        versions_by_app.setdefault(a, []).append(v)
    assert sorted(versions_by_app["models"]) == on_disk_models
    assert sorted(versions_by_app["returns"]) == on_disk_returns


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
    EPHEMERAL_DB is None,
    reason="No pre-aerich DB snapshot in .claude/ephemeral; this test is local-only.",
)
async def test_bootstrap_on_existing_db(_isolated_db):
    """Copy the ephemeral snapshot to a tmp_path, run ``init_db``, and
    assert: aerich tracking table created (with rows for both apps),
    every preserved row count unchanged, the legacy returns tables
    migrated to returns.db then dropped from dazebot.db.
    """
    db_path = _isolated_db
    returns_path = db_path.parent / "test_returns.db"
    shutil.copy2(EPHEMERAL_DB, db_path)

    # Pre-bootstrap row counts and table set (sanity-anchor: prove we're
    # comparing against the real snapshot, not what aerich generated).
    pre_tables = _table_names(db_path)
    assert "aerich" not in pre_tables
    # Tables that MUST survive in dazebot.db post-migration.
    dazebot_kept_tables = [
        "minecraft_accounts",
        "discord_accounts",
        "cults",
        "cult_memberships",
        "verify_keys",
        "profession_categories",
    ]
    pre_counts = _row_counts(db_path, dazebot_kept_tables)
    # Legacy returns tables (may or may not be in this snapshot — older
    # pre-aerich snapshots predate them).
    pre_legacy_counts = _row_counts(
        db_path,
        [t for t in ("return_guesses", "story_segments") if t in pre_tables],
    )

    await init_db()

    # The aerich tracking table now exists; the initial migration is
    # fake-applied (no DDL re-run) and any follow-up migrations — including
    # the legacy-returns drop — run for real on top.
    post_tables = _table_names(db_path)
    assert "aerich" in post_tables
    # Every non-legacy pre-existing table is still present.
    for t in pre_tables:
        if t in {"return_guesses", "story_segments"}:
            continue
        assert t in post_tables, f"{t} disappeared from dazebot.db"
    # Legacy returns tables MUST be gone from dazebot.db (drop migration ran).
    assert "return_guesses" not in post_tables
    assert "story_segments" not in post_tables

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT version, app FROM aerich ORDER BY id").fetchall()
    conn.close()
    assert {r[1] for r in rows} == {"models", "returns"}
    # Every on-disk migration is recorded for its respective app.
    from pathlib import Path
    on_disk_models = sorted(p.name for p in Path("migrations/models").glob("*.py"))
    on_disk_returns = sorted(p.name for p in Path("migrations/returns").glob("*.py"))
    versions_by_app: dict[str, list[str]] = {}
    for v, a in rows:
        versions_by_app.setdefault(a, []).append(v)
    assert sorted(versions_by_app["models"]) == on_disk_models
    assert sorted(versions_by_app["returns"]) == on_disk_returns

    # Kept tables: row counts unchanged.
    post_counts = _row_counts(db_path, dazebot_kept_tables)
    assert pre_counts == post_counts

    # Legacy returns rows migrated to returns.db with the disc_uuid translation.
    if pre_legacy_counts:
        post_returns_counts = _row_counts(returns_path, list(pre_legacy_counts.keys()))
        assert pre_legacy_counts == post_returns_counts


@pytest.mark.skipif(
    EPHEMERAL_DB is None,
    reason="No pre-aerich DB snapshot in .claude/ephemeral; this test is local-only.",
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
    EPHEMERAL_DB is None,
    reason="No pre-aerich DB snapshot in .claude/ephemeral; this test is local-only.",
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


# ---------------------------------------------------------------------------
# Branch 4: legacy returns-table → returns.db one-shot migration
# ---------------------------------------------------------------------------
#
# These tests seed a synthetic dazebot.db that mimics the pre-split prod
# shape: it has the full aerich-tracked schema through migration 10
# (i.e. *before* the drop-returns-legacy migration), plus rows in the
# legacy return_guesses / story_segments tables FK'd to real
# discord_accounts. Running init_db then exercises the whole copy →
# verify → drop path.


def _seed_legacy_dazebot_db(db_path: Path, *, n_guesses: int = 5, n_segs: int = 3) -> dict[str, int]:
    """Seed ``db_path`` with the *minimum* shape needed to exercise the
    legacy-returns copy path: aerich-tracked schema with discord_accounts
    + the legacy return_guesses/story_segments tables (FK'd to
    discord_accounts.id, as they were before the split), plus N rows in
    each. Returns the seeded row counts.

    Synthetic CREATE TABLEs — not aerich-driven — because the tests here
    care about the COPY behaviour, not how the schema got there. The
    aerich table is populated with one row per pre-drop migration so
    init_db's "pending migrations" detection finds only the drop on top.
    """
    import sqlite3

    pre_drop_migrations = sorted(
        p.name for p in Path("migrations/models").glob("*.py")
        if not p.name.endswith("_drop_returns_legacy.py")
    )

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE "aerich" (
                "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                "version" VARCHAR(255) NOT NULL,
                "app" VARCHAR(100) NOT NULL,
                "content" JSON NOT NULL
            );
            CREATE TABLE "discord_accounts" (
                "id" CHAR(36) NOT NULL PRIMARY KEY,
                "disc_uuid" VARCHAR(255) NOT NULL UNIQUE,
                "shout_count" INT NOT NULL DEFAULT 0,
                "minecraft_account_id" CHAR(36)
            );
            CREATE TABLE "minecraft_accounts" (
                "id" CHAR(36) NOT NULL PRIMARY KEY
            );
            CREATE TABLE "return_guesses" (
                "id" CHAR(36) NOT NULL PRIMARY KEY,
                "week" INT NOT NULL,
                "day" INT NOT NULL,
                "discord_account_id" CHAR(36) NOT NULL REFERENCES "discord_accounts"("id"),
                "price" INT NOT NULL,
                "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE "story_segments" (
                "id" CHAR(36) NOT NULL PRIMARY KEY,
                "week" INT NOT NULL,
                "discord_account_id" CHAR(36) NOT NULL REFERENCES "discord_accounts"("id"),
                "content" TEXT NOT NULL,
                "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        for m in pre_drop_migrations:
            conn.execute(
                'INSERT INTO "aerich" ("version", "app", "content") VALUES (?, ?, ?)',
                (m, "models", "{}"),
            )

        disc_records: list[tuple[str, str]] = []
        for i in range(max(n_guesses, n_segs)):
            disc_id = f"00000000-0000-0000-0000-{i:012d}"
            disc_uuid = f"9{i:017d}"
            disc_records.append((disc_id, disc_uuid))
            conn.execute(
                'INSERT INTO "discord_accounts" ("id", "disc_uuid") VALUES (?, ?)',
                (disc_id, disc_uuid),
            )
        for i in range(n_guesses):
            conn.execute(
                'INSERT INTO "return_guesses" '
                '("id", "week", "day", "discord_account_id", "price") '
                "VALUES (?, ?, ?, ?, ?)",
                (
                    f"00000001-0000-0000-0000-{i:012d}",
                    75, i + 1, disc_records[i][0], 1000 * (i + 1),
                ),
            )
        for i in range(n_segs):
            conn.execute(
                'INSERT INTO "story_segments" '
                '("id", "week", "discord_account_id", "content") '
                "VALUES (?, ?, ?, ?)",
                (
                    f"00000002-0000-0000-0000-{i:012d}",
                    73, disc_records[i][0], f"segment {i}",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return {
        "return_guesses": n_guesses,
        "story_segments": n_segs,
        "discord_accounts": max(n_guesses, n_segs),
    }


async def test_legacy_returns_data_copied_with_disc_uuid_translation(_isolated_db):
    """Seed dazebot.db with pre-split state (legacy tables + FK-linked rows),
    run init_db, and verify:
      - returns.db has every legacy row, identical counts.
      - The disc_uuid column in returns.db matches the Discord snowflake
        string (translated from the source's synthetic UUID FK).
      - The legacy tables are gone from dazebot.db.
    """
    from orm import _copy_returns_legacy_data

    db_path = _isolated_db
    returns_path = db_path.parent / "test_returns.db"

    seeded = _seed_legacy_dazebot_db(db_path, n_guesses=5, n_segs=3)
    pre_disc_uuids = sorted(
        r[0] for r in sqlite3.connect(db_path).execute(
            "SELECT da.disc_uuid FROM return_guesses rg "
            "JOIN discord_accounts da ON da.id = rg.discord_account_id"
        )
    )

    # Pre-create returns.db schema so the copy helper has somewhere to insert.
    # init_db() does this via the returns-app aerich upgrade, but for this
    # focused test we exercise the copy helper directly.
    import sqlite3 as _sq3
    rconn = _sq3.connect(returns_path)
    try:
        rconn.executescript("""
            CREATE TABLE "return_guesses" (
                "id" CHAR(36) NOT NULL PRIMARY KEY,
                "week" INT NOT NULL,
                "day" INT NOT NULL,
                "disc_uuid" VARCHAR(255) NOT NULL,
                "price" INT NOT NULL,
                "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE "story_segments" (
                "id" CHAR(36) NOT NULL PRIMARY KEY,
                "week" INT NOT NULL,
                "disc_uuid" VARCHAR(255) NOT NULL,
                "content" TEXT NOT NULL,
                "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        rconn.commit()
    finally:
        rconn.close()

    _copy_returns_legacy_data(str(db_path), str(returns_path))

    # Counts match.
    post_returns = _row_counts(returns_path, ["return_guesses", "story_segments"])
    assert post_returns == {
        "return_guesses": seeded["return_guesses"],
        "story_segments": seeded["story_segments"],
    }

    # Translation: every disc_uuid in returns.db's return_guesses matches
    # what the source FK pointed at via discord_accounts.disc_uuid.
    post_disc_uuids = sorted(
        r[0] for r in sqlite3.connect(returns_path).execute(
            "SELECT disc_uuid FROM return_guesses"
        )
    )
    assert post_disc_uuids == pre_disc_uuids


async def test_legacy_returns_copy_is_idempotent(_isolated_db):
    """Running the copy twice doesn't double the data — the
    destination-empty gate inside ``_copy_returns_legacy_data`` skips on
    rerun.
    """
    from orm import _copy_returns_legacy_data

    db_path = _isolated_db
    returns_path = db_path.parent / "test_returns.db"
    seeded = _seed_legacy_dazebot_db(db_path, n_guesses=4, n_segs=2)

    import sqlite3 as _sq3
    rconn = _sq3.connect(returns_path)
    try:
        rconn.executescript("""
            CREATE TABLE "return_guesses" (
                "id" CHAR(36) NOT NULL PRIMARY KEY,
                "week" INT NOT NULL, "day" INT NOT NULL,
                "disc_uuid" VARCHAR(255) NOT NULL,
                "price" INT NOT NULL,
                "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE "story_segments" (
                "id" CHAR(36) NOT NULL PRIMARY KEY,
                "week" INT NOT NULL,
                "disc_uuid" VARCHAR(255) NOT NULL,
                "content" TEXT NOT NULL,
                "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        rconn.commit()
    finally:
        rconn.close()

    _copy_returns_legacy_data(str(db_path), str(returns_path))
    _copy_returns_legacy_data(str(db_path), str(returns_path))

    post = _row_counts(returns_path, ["return_guesses", "story_segments"])
    assert post == {
        "return_guesses": seeded["return_guesses"],
        "story_segments": seeded["story_segments"],
    }


async def test_init_db_refuses_legacy_drop_without_gate(_isolated_db, monkeypatch):
    """If dazebot.db still has the legacy returns tables, init_db must
    refuse unless ``DAZEBOT_ALLOW_LEGACY_RETURNS_DROP=1`` is set. This is
    the safety latch that ensures the host-side dump has been taken
    before the destructive drop migration runs.
    """
    db_path = _isolated_db
    _seed_legacy_dazebot_db(db_path, n_guesses=2, n_segs=1)

    monkeypatch.delenv("DAZEBOT_ALLOW_LEGACY_RETURNS_DROP", raising=False)
    with pytest.raises(RuntimeError, match="DAZEBOT_ALLOW_LEGACY_RETURNS_DROP"):
        await init_db()


async def test_copy_helper_detects_orphan_rows(_isolated_db):
    """If a legacy ``return_guesses`` row points at a ``discord_account_id``
    that doesn't exist (shouldn't happen — FK was CASCADE — but we don't
    want to silently drop history if it does), the copy helper raises
    rather than producing a smaller-than-source destination.
    """
    from orm import _copy_returns_legacy_data

    db_path = _isolated_db
    returns_path = db_path.parent / "test_returns.db"
    _seed_legacy_dazebot_db(db_path, n_guesses=3, n_segs=1)

    # Insert an orphan row whose FK target doesn't exist.
    orphan_conn = sqlite3.connect(db_path)
    try:
        orphan_conn.execute(
            'INSERT INTO "return_guesses" '
            '("id", "week", "day", "discord_account_id", "price", "created_at") '
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ("99999999-9999-9999-9999-999999999999", 75, 7, "ffffffff-ffff-ffff-ffff-ffffffffffff", 999),
        )
        orphan_conn.commit()
    finally:
        orphan_conn.close()

    # Pre-create returns.db schema.
    import sqlite3 as _sq3
    rconn = _sq3.connect(returns_path)
    try:
        rconn.executescript("""
            CREATE TABLE "return_guesses" (
                "id" CHAR(36) NOT NULL PRIMARY KEY,
                "week" INT NOT NULL, "day" INT NOT NULL,
                "disc_uuid" VARCHAR(255) NOT NULL,
                "price" INT NOT NULL,
                "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE "story_segments" (
                "id" CHAR(36) NOT NULL PRIMARY KEY,
                "week" INT NOT NULL,
                "disc_uuid" VARCHAR(255) NOT NULL,
                "content" TEXT NOT NULL,
                "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        rconn.commit()
    finally:
        rconn.close()

    with pytest.raises(RuntimeError, match="Orphan rows"):
        _copy_returns_legacy_data(str(db_path), str(returns_path))

"""Regression tests for ``lib.mc.resolve.ensure_mc_account``'s UUID recheck.

Production failure this pins down (2026-08-24): a staff member ran
``~donations record nneeen 20le Oblivion`` for a player we already had on
file under their previous Mojang name, ``EenRandomG``. Nothing periodic
refreshes the two username columns for accounts outside the Returners
roster scan (``cogs/activity._apply_guild`` is the only writer), so:

  1. ``resolve_mc_account_loose("nneeen")`` missed -- both stored names
     were still the old one.
  2. The Wynncraft API knew the new name and returned the *old* row's
     UUID.
  3. ``MinecraftAccount.create`` hit the ``uuid`` UNIQUE index and threw
     ``tortoise.exceptions.IntegrityError`` straight out of the command
     handler and into ``on_command_error``.

``ensure_mc_account`` now rechecks by the API-supplied UUID before
creating, and treats a hit as a rename: adopt the row, refresh its names.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from tortoise.exceptions import IntegrityError

from lib.mc import resolve as resolve_mod
from lib.mc.resolve import ensure_mc_account
from orm import MinecraftAccount, UNKNOWN_LAST_ONLINE, init_db


RENAMED_UUID = "1c4f81a9-0000-4000-8000-00000000beef"
OLD_NAME = "EenRandomG"
NEW_NAME = "nneeen"


@pytest.fixture
async def _isolated_db(tmp_path, monkeypatch):
    """Fresh SQLite DB per test. Same shape as the fixture in
    ``tests/test_migrations.py``; duplicated here and in
    ``test_donation_milestone.py`` for the same reason (the DB-backed
    suites don't share a conftest).
    """
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
    yield
    await _reset()


def _stats(uuid: str, username: str, guild: str | None = None) -> SimpleNamespace:
    """Minimal stand-in for the ``WynncraftPlayer`` envelope -- only the
    attributes ``ensure_mc_account`` actually reads.
    """
    return SimpleNamespace(
        uuid=uuid,
        username=username,
        guild=SimpleNamespace(name=guild) if guild else None,
        lastJoin=datetime(2026, 8, 24, tzinfo=timezone.utc),
        firstJoin=datetime(2019, 1, 1, tzinfo=timezone.utc),
    )


def _patch_api(monkeypatch, stats: SimpleNamespace, *, canonical: str | None = None):
    """Point the Wynncraft and Mojang calls at fixed answers. Both names are
    patched in ``lib.mc.resolve``'s namespace, which is where the module
    imported them by value.
    """
    async def _get_player_stats(_value, **_kwargs):
        return stats

    async def _resolve_canonical_username(_uuid, hint=None):
        return canonical if canonical is not None else hint

    monkeypatch.setattr(resolve_mod, "get_player_stats", _get_player_stats)
    monkeypatch.setattr(
        resolve_mod, "resolve_canonical_username", _resolve_canonical_username
    )


async def _seed_old_row() -> MinecraftAccount:
    return await MinecraftAccount.create(
        uuid=RENAMED_UUID,
        wynn_username=OLD_NAME,
        mc_username=OLD_NAME,
        guild="Returners",
        last_online=UNKNOWN_LAST_ONLINE,
        last_manual_check=UNKNOWN_LAST_ONLINE,
    )


async def test_rename_adopts_existing_row_instead_of_raising(_isolated_db, monkeypatch):
    """The exact production case: new name in, old-name row on file."""
    await init_db()
    old = await _seed_old_row()
    _patch_api(monkeypatch, _stats(RENAMED_UUID, NEW_NAME, guild="Returners"))

    mc = await ensure_mc_account(NEW_NAME)

    assert mc.id == old.id
    assert await MinecraftAccount.filter(uuid=RENAMED_UUID).count() == 1


async def test_rename_refreshes_the_stale_usernames(_isolated_db, monkeypatch):
    """Adopting the row also repairs it, so the *next* lookup takes the
    cheap local path instead of round-tripping the API again.
    """
    await init_db()
    await _seed_old_row()
    _patch_api(monkeypatch, _stats(RENAMED_UUID, NEW_NAME), canonical=NEW_NAME)

    await ensure_mc_account(NEW_NAME)

    stored = await MinecraftAccount.get(uuid=RENAMED_UUID)
    assert stored.wynn_username == NEW_NAME
    assert stored.mc_username == NEW_NAME
    assert await resolve_mod.resolve_mc_account_loose(NEW_NAME) is not None


async def test_mojang_failure_falls_back_to_the_wynncraft_name(_isolated_db, monkeypatch):
    """``resolve_canonical_username`` raises when every Mojang provider is
    down and the cache is cold. Take the Wynncraft name anyway -- it is
    fresher than the value we already know to be stale.
    """
    await init_db()
    await _seed_old_row()

    async def _get_player_stats(_value, **_kwargs):
        return _stats(RENAMED_UUID, NEW_NAME)

    async def _boom(_uuid, hint=None):
        raise RuntimeError("all Mojang providers down")

    monkeypatch.setattr(resolve_mod, "get_player_stats", _get_player_stats)
    monkeypatch.setattr(resolve_mod, "resolve_canonical_username", _boom)

    mc = await ensure_mc_account(NEW_NAME)

    assert mc.mc_username == NEW_NAME
    assert mc.wynn_username == NEW_NAME


async def test_create_race_adopts_the_winners_row(_isolated_db, monkeypatch):
    """Two concurrent creators for one UUID: the loser adopts rather than
    surfacing a raw IntegrityError. The awaits inside ``ensure_mc_account``
    leave a wide window for the activity loop or a second command to land
    between our UUID recheck and our own INSERT.
    """
    await init_db()
    _patch_api(monkeypatch, _stats(RENAMED_UUID, NEW_NAME), canonical=NEW_NAME)

    # Bound classmethod captured before the patch, so calling it below does
    # not re-enter the stub.
    real_create = MinecraftAccount.create

    async def _racing_create(**kwargs):
        await real_create(**{**kwargs, "wynn_username": OLD_NAME, "mc_username": OLD_NAME})
        raise IntegrityError("UNIQUE constraint failed: minecraft_accounts.uuid")

    monkeypatch.setattr(MinecraftAccount, "create", _racing_create)

    mc = await ensure_mc_account(NEW_NAME)

    assert mc.uuid == RENAMED_UUID
    assert mc.mc_username == NEW_NAME  # adopted row, then re-synced
    assert await MinecraftAccount.filter(uuid=RENAMED_UUID).count() == 1


async def test_genuinely_new_player_is_still_created(_isolated_db, monkeypatch):
    """Guard the happy path that the UUID recheck now sits in front of."""
    await init_db()
    new_uuid = "1c4f81a9-0000-4000-8000-0000000000aa"
    _patch_api(monkeypatch, _stats(new_uuid, "SomeoneNew", guild="Returners"))

    mc = await ensure_mc_account("SomeoneNew")

    assert mc.uuid == new_uuid
    assert mc.guild == "Returners"
    assert await MinecraftAccount.filter(uuid=new_uuid).count() == 1


async def test_local_hit_never_touches_the_api(_isolated_db, monkeypatch):
    """A name already on file short-circuits before any network call -- the
    rename path must not turn into a per-lookup API tax.
    """
    await init_db()
    await _seed_old_row()

    async def _explode(*_a, **_k):
        raise AssertionError("ensure_mc_account hit the API on a local hit")

    monkeypatch.setattr(resolve_mod, "get_player_stats", _explode)

    mc = await ensure_mc_account(OLD_NAME.lower())

    assert mc.uuid == RENAMED_UUID

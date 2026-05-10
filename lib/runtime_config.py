"""Runtime overrides for ``Config`` attributes set via the ``/config`` admin
command. See ``../.claude/membership_spec.md`` \u00a73.

Persisted overrides live in the ``bot_config_overrides`` table and are loaded
into the live ``Config`` object on bot startup. Updates flow through
``set_override``/``clear_override`` which both mutate ``CurrConfig`` *and*
persist the change.

Only flat scalar attributes on the top-level ``Config`` class are exposed; the
nested ``VanityRolesConfig`` / ``AnniConfig`` / ``DocumentationConfig`` classes
and ``@property`` accessors are intentionally skipped to avoid surprises.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Iterable

from config import CurrConfig

logger = logging.getLogger("dazebot.runtime_config")


async def _checkpoint_wal() -> None:
    """Force a SQLite WAL checkpoint so writes hit the main DB file.

    Without this, container restarts via SIGKILL (which discord.py does not
    trap on SIGTERM) can leave override writes stuck in the -wal sidecar
    that doesn't always replay cleanly across container recreates.
    """
    try:
        from tortoise import Tortoise

        conn = Tortoise.get_connection("default")
        await conn.execute_query("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception as e:
        logger.warning(f"WAL checkpoint failed: {e}")


_SUPPORTED_TYPES = (int, float, bool, str)


def _is_overridable(name: str, value: Any) -> bool:
    if name.startswith("_"):
        return False
    if name.isupper() is False:
        # Only ALL_CAPS module-level constants are user-facing config.
        return False
    if isinstance(value, type):
        return False
    if callable(value):
        return False
    return isinstance(value, _SUPPORTED_TYPES) or isinstance(value, timedelta)


def list_keys() -> list[str]:
    keys: list[str] = []
    for name in dir(CurrConfig):
        try:
            val = getattr(CurrConfig, name)
        except Exception:
            continue
        if _is_overridable(name, val):
            keys.append(name)
    return sorted(keys)


def get_value(key: str) -> Any:
    return getattr(CurrConfig, key)


def _coerce(key: str, raw: str) -> Any:
    current = getattr(CurrConfig, key)
    if isinstance(current, bool):
        # bool must be checked BEFORE int (bool is an int subclass)
        if raw.lower() in ("true", "1", "yes", "y", "on"):
            return True
        if raw.lower() in ("false", "0", "no", "n", "off"):
            return False
        raise ValueError(f"Cannot parse {raw!r} as bool")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, timedelta):
        return timedelta(seconds=float(raw))
    return raw  # str passthrough


def _serialize(value: Any) -> str:
    if isinstance(value, timedelta):
        return json.dumps({"__timedelta__": value.total_seconds()})
    return json.dumps(value)


def _deserialize(blob: str) -> Any:
    obj = json.loads(blob)
    if isinstance(obj, dict) and "__timedelta__" in obj:
        return timedelta(seconds=float(obj["__timedelta__"]))
    return obj


async def load_overrides() -> None:
    """Apply persisted overrides to ``CurrConfig`` in-place."""
    from orm import BotConfigOverride  # local import to avoid circular dep

    overrides = await BotConfigOverride.all()
    logger.info(f"load_overrides: found {len(overrides)} persisted override row(s) in DB")
    applied = 0
    for row in overrides:
        if not hasattr(CurrConfig, row.key):
            logger.warning(f"Skipping unknown config override key: {row.key}")
            continue
        try:
            value = _deserialize(row.value_json)
        except Exception as e:
            logger.error(f"Failed to deserialize override {row.key!r}: {e}")
            continue
        setattr(CurrConfig, row.key, value)
        logger.info(f"load_overrides: applied {row.key} = {value!r}")
        applied += 1
    logger.info(f"load_overrides: applied {applied}/{len(overrides)} override(s)")


async def set_override(key: str, raw_value: str) -> Any:
    from orm import BotConfigOverride

    if not hasattr(CurrConfig, key):
        raise KeyError(f"Unknown config key: {key}")
    current = getattr(CurrConfig, key)
    if not _is_overridable(key, current):
        raise PermissionError(f"Config key {key} is not overridable at runtime")

    new_value = _coerce(key, raw_value)
    setattr(CurrConfig, key, new_value)
    await BotConfigOverride.update_or_create(
        key=key, defaults={"value_json": _serialize(new_value)}
    )
    await _checkpoint_wal()
    logger.info(f"Set runtime config override {key} = {new_value!r}")
    return new_value


async def clear_override(key: str) -> None:
    from orm import BotConfigOverride

    deleted = await BotConfigOverride.filter(key=key).delete()
    if deleted:
        await _checkpoint_wal()
        logger.info(f"Cleared runtime config override {key}")


def keys_filtered(prefix: str | None = None) -> Iterable[str]:
    keys = list_keys()
    if prefix:
        return [k for k in keys if k.lower().startswith(prefix.lower())]
    return keys

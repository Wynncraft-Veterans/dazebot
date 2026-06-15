# Runtime config (`/config` overrides)

Reference for [lib/runtime_config.py](../lib/runtime_config.py) and the `BotConfigOverride` ORM table — how the `/config` admin command lets staff change flat `Config` constants without redeploying.

## What's overridable

Only **flat scalar attributes** on the top-level `Config` class (`config.py`). Specifically, `_is_overridable` requires:
- ALL_CAPS name (skips `_private` and `mixedCase`)
- not a `type` or `callable`
- value is `int`, `float`, `bool`, `str`, or `timedelta`

Things that are deliberately NOT overridable:
- Nested config classes (`VanityRolesConfig`, `DocumentationConfig`)
- `@property` accessors
- `list[...]`, `dict[...]`, or any structured type

If you want a list-typed knob to be runtime-mutable, add a sibling scalar (e.g. `ALERTS_ENABLED: bool` rather than `ALERTS_CHANNELS: list[int]`) or build a dedicated cog command for it.

## Storage

Table: `bot_config_overrides`. Single row per overridden key:
```
key            value_json    updated_at
ROLE_MEMBER    "1234..."     2026-04-12 ...
DEBUG          true          2026-04-12 ...
```

Values are JSON-encoded so any of `int`/`bool`/`str` round-trips cleanly. `timedelta` uses a `{"__timedelta__": <seconds>}` envelope.

## Boot sequence

`bot.py:setup_hook` calls `runtime_config.load_overrides()` *after* `init_db()`. Each override `setattr`s the value onto `CurrConfig` in-place. Unknown keys are skipped with a warning (rather than crashing) — keeps deploys smooth when a column gets renamed.

## `/config` command flow

Defined in `cogs/membership/runtime_config_cog.py` under `commands.hybrid_group(name="config")`. Subcommands:

- `/config list [prefix]` — `keys_filtered(prefix)` to filter, then show key=value for each
- `/config get <key>` — `get_value(key)` — show current live value
- `/config set <key> <value>` — `set_override(key, raw_value)` — coerce, set, persist
- `/config clear <key>` — `clear_override(key)` — delete the row, **but does not revert** the in-memory value to the compiled default. The user must restart to get the compiled default back, or `/config set` it explicitly.

`set_override` calls `_coerce(key, raw_value)` which uses the *current* type to decide how to parse:

```python
isinstance(current, bool)      → "true"/"false"/"yes"/"no"/"on"/"off"/"1"/"0"
isinstance(current, int)       → int(raw)         # bool branch above MUST come first
isinstance(current, float)     → float(raw)
isinstance(current, timedelta) → timedelta(seconds=float(raw))
else                           → str (passthrough)
```

The `bool` branch must come before `int` because `bool` is a Python `int` subclass. Reordering them silently turns boolean knobs into integer ones.

## WAL checkpoint after writes

`set_override` and `clear_override` both call `_checkpoint_wal()` after the DB write. This issues `PRAGMA wal_checkpoint(TRUNCATE)` so the change is durable in the main DB file rather than only in `dazebot.db-wal`.

Why: discord.py doesn't trap SIGTERM, and Docker SIGKILLs after the grace period. WAL writes that haven't been checkpointed can fail to replay across container recreates. The `/config` write path is the one place where data loss is most user-visible (a staff member ran `/config set ROLE_MEMBER 12345`, the bot got restarted, and the change is gone), so we force a checkpoint after every override write.

If you add another path that writes critical metadata, copy the `_checkpoint_wal()` pattern.

## Don't

- **Don't read `Config.X` for hot-path values.** Use `CurrConfig.X` (which is a re-export with overrides applied). `Config.X` is the *compiled default* — the override-load step mutates `CurrConfig`, not `Config`.
- **Don't introduce a new value type without updating `_SUPPORTED_TYPES` and `_coerce`.** Otherwise the new key shows up in `list_keys()` (because it passes `isinstance(value, _SUPPORTED_TYPES)` for whatever subtype it derives from) but `set_override` raises on coercion.
- **Don't make `clear_override` "revert" the in-memory value.** That requires a reload from `Config` (the compiled defaults module) — possible but currently unimplemented. The current behaviour is "the override row is gone; in-memory value persists until next restart"; document this if a staff member is confused.

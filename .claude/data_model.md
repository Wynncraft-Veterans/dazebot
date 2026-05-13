# Data model

Reference for [orm.py](../orm.py) — every table, its purpose, and which subsystem owns it.

DB engine: SQLite at `/app/data/dazebot.db` (production) or `dev.db` at the repo root (local). WAL mode. **No migrations** — `Tortoise.generate_schemas(safe=True)` runs in `init_db()` and only ever emits `CREATE TABLE IF NOT EXISTS`. It does **not** alter existing tables. New tables work automatically; new columns require a one-shot manual `ALTER TABLE`. To remove a field, drop the table or wire in Aerich.

## Identity

| Table | Owner | Purpose |
|---|---|---|
| `MinecraftAccount` | `lib/mc/linking.py`, activity loop | UUID-keyed MC profile. Holds `wynn_username` (cached from API, stale-tolerant) + `mc_username` + `guild` + `last_online` + `first_join`. Also carries `last_seen_server` + `server_observed_at`, written by `cogs/server_watcher.py` to infer activity for privacy-hidden players (see [cogs.md](cogs.md)). |
| `DiscordAccount` | `lib/mc/linking.py` | `disc_uuid` + nullable FK to the *primary* `MinecraftAccount`. One row per Discord user. |
| `MinecraftAlt` | `cogs/moderation/admin.py` (`/link alt`) | Additional MC accounts beyond the primary. The primary lives on `DiscordAccount.minecraft_account`; the alts live here. |
| `MojangNameCache` | `lib/mc/mojang.py` | Persistent UUID → username cache. Refreshed after `MAX_AGE` to handle name changes. Distinct from the per-account `MinecraftAccount.mc_username` snapshot. |

## Membership state

| Table | Owner | Purpose |
|---|---|---|
| `Waitlist` | `cogs/membership/waitlist.py` (`/waitlist`) | In-game guild waitlist. FK to `MinecraftAccount`. |
| `Blocklist` | `cogs/membership/blocking.py` (`/block`) | Forced-to-Registered users. FK to `MinecraftAccount`, unique. Triggers `force_to_registered_only` on add. |

The role state itself (REGISTERED/HIATUS/MEMBER/HONOURARY/WAITLISTED) lives on Discord — these tables hold the data that *triggers* state transitions, not the state. See [role_state.md](role_state.md).

## Auth flows

| Table | Owner | Purpose |
|---|---|---|
| `LinkCode` | `lib/mc/linking.py` | One row per pending `/first_install` code. Key: `mc_username` (lowercased). Holds the 6-char code + target `disc_uuid`. Deleted on successful consumption. See [linking.md](linking.md). |
| `VerifyKey` | `lib/staff/verify_keys.py` | One row per Discord user with a vetsmod key. 43-char URL-safe base64 key + tier snapshot + `mc_uuid/mc_username` + `last_used_at` + `revoked_at`. See [verify_keys.md](verify_keys.md). |

## Customisation / vanity

| Table | Owner | Purpose |
|---|---|---|
| `UserVanityChoice` | `cogs/membership/vanity_roles.py` | Records a user's manual vanity-role pick. Auto-assignment by firstJoin must NOT override this. |

## Bot configuration

| Table | Owner | Purpose |
|---|---|---|
| `BotConfigOverride` | `lib/runtime_config.py` | JSON-encoded overrides for top-level `Config` constants. Loaded into `CurrConfig` on boot, mutated by the `/config` admin command. WAL is force-checkpointed after writes. |
| `FirstInstallMonitor` | `cogs/membership/join.py`, `lib/discord_utils/first_install_view.py` | Tracks which channel message hosts the persistent "Link my account" button so it survives bot restarts. |
| `DMSentLog` | `cogs/membership/membership_state.py` (inactivity loop) | Idempotency for one-shot DMs (e.g. inactive-member warnings). Prevents the activity loop from re-DMing on every tick. |

## Weekly events / scoring (legacy / cog-specific)

| Table | Owner | Purpose |
|---|---|---|
| `WeeklyEvent` | `cogs/events/weekly_event.py`, `cogs/events/returns/` | Weekly Returners event configuration. |
| `Score` | `cogs/events/weekly_event.py` | Per-user scoring for the weekly event. |

## Alerts (rare, edge-case-driven)

| Table | Owner | Purpose |
|---|---|---|
| `DeadGuildAlert` | `cogs/activity/activity.py` | Records that a "dead guild" alert was raised so we don't re-spam staff. |
| `GuildCapacityAlert` | `cogs/activity/activity.py` | Same shape, for capacity-based alerts. |
| `Shout` | (rare) | One-off staff shout broadcasts. |
| `LinkRequest` | (legacy) | Predates the `LinkCode` flow. Inspect before adding new code that uses it. |

## Cult subsystem

| Table | Owner | Purpose |
|---|---|---|
| `Cult` | `cogs/events/returns/week_0.py` | Returners "cults" (sub-groups). |
| `CultMembership` | `cogs/events/returns/week_0.py` | Membership of a Discord user in a cult. |

## Misc

| Table | Owner | Purpose |
|---|---|---|
| `ProfessionCategories` | (data ref) | Lookup for Wynncraft profession categories. Static-ish data. |

## Relationships at a glance

```
DiscordAccount ──primary──► MinecraftAccount ◄── alts ── MinecraftAlt
                            │
                            ├──► Waitlist
                            ├──► Blocklist (unique)
                            └──► (UUID lookup) MojangNameCache, ProfessionCategories
DiscordAccount ──disc_uuid─► VerifyKey (1:1, unique on disc_uuid)
                          ──► UserVanityChoice (1:1, unique on disc_uuid)
                          ──► DMSentLog (n: per dm-key)
                          ──► CultMembership ──► Cult
LinkCode (no FK; matched by mc_username + code at consume time)
```

## When to add a new table

`init_db()` calls `Tortoise.generate_schemas(safe=True)` which only emits `CREATE TABLE IF NOT EXISTS`. Existing tables are completely untouched — no `ALTER TABLE` is ever issued. So:

- **Adding a table?** Add the model, restart the bot, you're done.
- **Adding a column?** Tortoise will *not* add it for you. The model will reference a column that doesn't exist, every query that selects it will fail (`OperationalError: no such column`), and every `.save()` on that table will then fail with `IncompleteInstanceError` because Tortoise falls back to partial-loading. You MUST run `ALTER TABLE <table> ADD COLUMN <name> <type>;` on every DB (production + every dev DB) before deploying the model change. Use the canonical type Tortoise would have generated — inspect via `Tortoise.get_connection('default').schema_generator(conn).get_create_schema_sql(safe=True)`.
- **Renaming a column?** Doesn't work via `generate_schemas`. Either drop the table or write a one-shot migration script. Aerich is in the dependency tree but not wired up.
- **Removing a column?** Same — either ignore it (the column lingers) or do a manual schema bump.

Production safety: The `dazebot.db-wal` sidecar can hold uncommitted writes if the container exits via SIGKILL. `runtime_config.set_override` issues a `PRAGMA wal_checkpoint(TRUNCATE)` after every write to mitigate this. Other write-heavy cogs *should* but don't always do the same; on a restart-during-write you may see a small recent change "rollback".

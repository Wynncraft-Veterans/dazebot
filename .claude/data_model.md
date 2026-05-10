# Data model

Reference for [orm.py](../orm.py) — every table, its purpose, and which subsystem owns it.

DB engine: SQLite at `/app/data/dazebot.db` (production) or `dev.db` at the repo root (local). WAL mode. **No migrations** — `Tortoise.generate_schemas()` runs in `init_db()` and only ever *adds* tables/columns. To remove a field, drop the table or wire in Aerich.

## Identity

| Table | Owner | Purpose |
|---|---|---|
| `MinecraftAccount` | `lib/linking.py`, activity loop | UUID-keyed MC profile. Holds `wynn_username` (cached from API, stale-tolerant) + `mc_username` + `guild` + `last_online` + `first_join`. |
| `DiscordAccount` | `lib/linking.py` | `disc_uuid` + nullable FK to the *primary* `MinecraftAccount`. One row per Discord user. |
| `MinecraftAlt` | `cogs/management.py` (`/add`) | Additional MC accounts beyond the primary. The primary lives on `DiscordAccount.minecraft_account`; the alts live here. |
| `MojangNameCache` | `lib/mc.py` | Persistent UUID → username cache. Refreshed after `MAX_AGE` to handle name changes. Distinct from the per-account `MinecraftAccount.mc_username` snapshot. |

## Membership state

| Table | Owner | Purpose |
|---|---|---|
| `Waitlist` | `cogs/management.py` (`/waitlist`) | In-game guild waitlist. FK to `MinecraftAccount`. |
| `Blocklist` | `cogs/management.py` (`/block`) | Forced-to-Registered users. FK to `MinecraftAccount`, unique. Triggers `force_to_registered_only` on add. |

The role state itself (REGISTERED/HIATUS/MEMBER/HONOURARY/WAITLISTED) lives on Discord — these tables hold the data that *triggers* state transitions, not the state. See [role_state.md](role_state.md).

## Auth flows

| Table | Owner | Purpose |
|---|---|---|
| `LinkCode` | `lib/linking.py` | One row per pending `/first_install` code. Key: `mc_username` (lowercased). Holds the 6-char code + target `disc_uuid`. Deleted on successful consumption. See [linking.md](linking.md). |
| `VerifyKey` | `lib/verify_keys.py` | One row per Discord user with a vetsmod key. 43-char URL-safe base64 key + tier snapshot + `mc_uuid/mc_username` + `last_used_at` + `revoked_at`. See [verify_keys.md](verify_keys.md). |

## Customisation / vanity

| Table | Owner | Purpose |
|---|---|---|
| `UserVanityChoice` | `cogs/vanity_roles.py` | Records a user's manual vanity-role pick. Auto-assignment by firstJoin must NOT override this. |

## Bot configuration

| Table | Owner | Purpose |
|---|---|---|
| `BotConfigOverride` | `lib/runtime_config.py` | JSON-encoded overrides for top-level `Config` constants. Loaded into `CurrConfig` on boot, mutated by the `/config` admin command. WAL is force-checkpointed after writes. |
| `FirstInstallMonitor` | `cogs/join.py`, `lib/first_install_view.py` | Tracks which channel message hosts the persistent "Link my account" button so it survives bot restarts. |
| `DMSentLog` | `lib/lib.py` | Idempotency for one-shot DMs (e.g. inactive-member warnings). Prevents the activity loop from re-DMing on every tick. |

## Weekly events / scoring (legacy / cog-specific)

| Table | Owner | Purpose |
|---|---|---|
| `WeeklyEvent` | `cogs/weekly_event.py`, `cogs/returns/` | Weekly Returners event configuration. |
| `Score` | `cogs/weekly_event.py` | Per-user scoring for the weekly event. |

## Alerts (rare, edge-case-driven)

| Table | Owner | Purpose |
|---|---|---|
| `DeadGuildAlert` | `cogs/management.py` | Records that a "dead guild" alert was raised so we don't re-spam staff. |
| `GuildCapacityAlert` | `cogs/management.py` | Same shape, for capacity-based alerts. |
| `Shout` | (rare) | One-off staff shout broadcasts. |
| `LinkRequest` | (legacy) | Predates the `LinkCode` flow. Inspect before adding new code that uses it. |

## Cult subsystem

| Table | Owner | Purpose |
|---|---|---|
| `Cult` | `cogs/management.py` | Returners "cults" (sub-groups). |
| `CultMembership` | `cogs/management.py` | Membership of a Discord user in a cult. |

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

`init_db()` calls `Tortoise.generate_schemas(safe=True)` which only *adds* — existing tables are untouched. So:

- **Adding a column?** It works automatically on next boot. Old rows have `NULL` (or the field default).
- **Adding a table?** Add the model, restart the bot, you're done.
- **Renaming a column?** Doesn't work via `generate_schemas`. Either drop the table or write a one-shot migration script. Aerich is in the dependency tree but not wired up.
- **Removing a column?** Same — either ignore it (the column lingers) or do a manual schema bump.

Production safety: The `dazebot.db-wal` sidecar can hold uncommitted writes if the container exits via SIGKILL. `runtime_config.set_override` issues a `PRAGMA wal_checkpoint(TRUNCATE)` after every write to mitigate this. Other write-heavy cogs *should* but don't always do the same; on a restart-during-write you may see a small recent change "rollback".

# Data model

Reference for [orm.py](../orm.py) — every table, its purpose, and which subsystem owns it.

DB engine: SQLite at `/app/data/dazebot.db` (production) or `dev.db` at the repo root (local). WAL mode. Schema is managed by [aerich](https://github.com/tortoise/aerich) — migrations live in [migrations/models/](../migrations/models/) and are applied automatically at boot by `init_db()`. Adding/renaming/removing columns is supported via `uv run aerich migrate --name "..."`; see "When to add a new table" below.

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
| `IntercultMessage` | `cogs/events/returns/lib/views/intercult_view.py` | Append-only log of cross-cult messages sent via the pinned intercult button. The 24h-per-cult cooldown is derived from the most recent row per `sender_cult_name`. Stores names (not FKs) so a future cult rename leaves history pointing at the name-at-time. |
| `RecruitmentQuery` | `cogs/events/returns/lib/views/recruitment_view.py` | Append-only log of clicks on the pinned recruitment button (lists online players not in any cult). The 1h-per-cult cooldown is derived from the most recent row per `cult_name`. Same name-not-FK pattern as `IntercultMessage`. |

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

## Changing the schema

All changes — new tables, new columns, renames, removals — go through aerich:

1. Edit the model in [`orm.py`](../orm.py).
2. `uv run aerich migrate --name "describe_change"` — generates a new file under `migrations/models/`.
3. Inspect the generated file (`upgrade()` and `downgrade()` SQL); commit it.
4. Restart the bot; `init_db()` applies pending migrations automatically.

`init_db()` (in [`orm.py`](../orm.py)) handles three states:

- **Fresh DB** (file absent / no model tables): `aerich.Command.upgrade()` runs the initial migration's `CREATE TABLE`s normally.
- **Existing pre-aerich DB** (model tables present, no `aerich` table): `_create_aerich_tracking_table` provisions the tracking table, then `upgrade(fake=True)` records the initial migration as applied without re-running its DDL. This is the one-time bootstrap path on first deploy after this rollout.
- **Already-bootstrapped DB**: `upgrade()` applies any pending follow-up migrations; no-op when up-to-date.

The detection is read-only (`sqlite_master` query) and the fake-apply path is idempotent. If you want to inspect manually:

```bash
docker exec dazebot sqlite3 /app/data/dazebot.db 'SELECT version, app FROM aerich;'
```

Production safety: The `dazebot.db-wal` sidecar can hold uncommitted writes if the container exits via SIGKILL. `runtime_config.set_override` issues a `PRAGMA wal_checkpoint(TRUNCATE)` after every write to mitigate this. Other write-heavy cogs *should* but don't always do the same; on a restart-during-write you may see a small recent change "rollback".

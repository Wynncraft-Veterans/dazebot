# Data model

Reference for [orm.py](../orm.py) (dazebot.db tables) and
[orm_returns.py](../orm_returns.py) (returns.db tables) — every table, its
purpose, and which subsystem owns it.

DB engine: SQLite. Two physical files at `/app/data/` (production) or in
the repo root (local):

| File | Tortoise app | Migration dir | Models module |
|---|---|---|---|
| `dazebot.db` | `models` | `migrations/models/` | [`orm.py`](../orm.py) |
| `returns.db` | `returns` | `migrations/returns/` | [`orm_returns.py`](../orm_returns.py) |

Both DBs run WAL mode. Schema is managed by
[aerich](https://github.com/tortoise/aerich) — `init_db()` applies pending
migrations on both apps at every boot. Aerich tracks BOTH apps' history in
ONE `aerich` table that lives in dazebot.db's default connection (this is
the actual aerich behaviour — verified empirically; do not "fix" by adding
`aerich.models` to the returns app's models list). Adding/renaming/removing
columns is supported via `uv run aerich --app <models|returns> migrate
--name "..."`; see "When to add a new table" below.

## Why the two-DB split

The returns subsystem hosts ad-hoc, per-week ephemeral state (price guesses
for week 75, story fragments for week 73, future similar one-shots) that
has a fundamentally different lifecycle than the well-modelled dazebot
core. Keeping them in separate files means:

- A regression in a single `cogs/events/returns/week_N.py` can't corrupt
  the dazebot core schema.
- "Wipe last year of stale returns state" is a file-level (`rm returns.db`,
  let init_db re-create) rather than per-table operation.
- The host-side dump pipeline (`manage update dazebot` → pre-update
  `_dump_sqlite_pre_update`) backs both files up to
  `/opt/docker/backups/db-dumps/dazebot/` as `dazebot-<ts>.db` and
  `returns-<ts>.db` — independent restore points.

What lives in **returns.db** (`orm_returns.py`): `ReturnGuess` (week 75
price guesses), `StorySegment` (week 73 collaborative story). Both
reference Discord users via a plain `disc_uuid: CharField` — **not** a
FK, because SQLite has no cross-database FK enforcement. This mirrors the
existing pattern used by `IntercultMessage.sender_disc_uuid`,
`RecruitmentQuery.requester_disc_uuid`, etc. in orm.py.

What stays in **dazebot.db**: everything else, including the Returners
infrastructure that is NOT per-week ephemeral — `WeeklyEvent`, `Score`
(scoring scaffold every return uses), `Cult` and friends (week 0 / cults
are permanent), and `Apartment` (community feature, not week-scoped).

Restoring from a known-safe dump: `vets-deploy/scripts/one-off/restore-dazebot-db.sh`
for dazebot.db and `restore-returns-db.sh` for returns.db.

## 2026-06-14 one-shot: legacy returns-tables split

`return_guesses` and `story_segments` used to live in dazebot.db, FK'd to
`DiscordAccount`. The 2026-06-14 deploy (a) created returns.db with a
standalone schema (no FK; just an indexed `disc_uuid` CharField),
(b) ran a one-shot `_copy_returns_legacy_data` helper in `init_db` that
ATTACHes returns.db, INSERT...SELECT JOINs through `discord_accounts` to
translate `discord_account_id` (UUID PK) → `disc_uuid` (snowflake string),
and verifies row counts before commit, and (c) ran the
`11_*_drop_returns_legacy.py` migration to remove the legacy tables from
dazebot.db. The destructive drop is gated on `DAZEBOT_ALLOW_LEGACY_RETURNS_DROP=1`
which `manage update dazebot` only sets after a successful host-side dump
landed in `/opt/docker/backups/db-dumps/dazebot/`. Once every deployed
environment is past this one-shot, the gate variable + the
`_LEGACY_RETURNS_TABLES` constant in `orm.py` can be removed.

## Identity

| Table | Owner | Purpose |
|---|---|---|
| `MinecraftAccount` | `lib/mc/linking.py`, activity loop | UUID-keyed MC profile. Holds `wynn_username` (cached from API, stale-tolerant) + `mc_username` + `guild` + `last_online` + `first_join`. Also carries `last_seen_server` + `server_observed_at` (branch A baseline) + `last_stat_snapshot` (branch B baseline, JSON over every monotonic /v3/player counter) + `lastjoin_hidden_at` (drives server_watcher's poll scope), all written by `cogs/activity/server_watcher.py` / `cogs/activity/activity.py` to infer activity for privacy-hidden players (see [cogs.md](cogs.md)). Two further columns serve the hiatus-return DM only: `last_in_returners_at` (last tick we saw them on the Returners roster — the *presence* stamp behind the "have they been back since we DMed them?" gate) and `online_seen_at` (last tick we had positive evidence they were **online** — a hole in it longer than `HIATUS_RETURN_DM_LOGOUT_GAP_MINUTES` is the only evidence we get that someone logged out and back in). `online_seen_at` is *not* a substitute for `last_online`: that one is a monotonic claim about the player sourced from Wynncraft's `lastJoin`, this one is a claim about our own observations. See "**`last_online` sentinel — API-disabled players**" below for the epoch-sentinel semantics. |
| `DiscordAccount` | `lib/mc/linking.py` | `disc_uuid` + nullable FK to the *primary* `MinecraftAccount`. One row per Discord user. |
| `MinecraftAlt` | `cogs/moderation/admin.py` (`/link alt`) | Additional MC accounts beyond the primary. The primary lives on `DiscordAccount.minecraft_account`; the alts live here. |
| `MojangNameCache` | `lib/mc/mojang.py` | Persistent UUID → username cache. Refreshed after `MAX_AGE` to handle name changes. Distinct from the per-account `MinecraftAccount.mc_username` snapshot. |

### `last_online` sentinel — API-disabled players

`MinecraftAccount.last_online == UNKNOWN_LAST_ONLINE` (the Unix epoch, defined in [`orm.py`](../orm.py)) is the in-band sentinel for **"this Wynncraft player has their API/profile disabled"** (the WAPI returns `lastJoin=null`, so no real timestamp is knowable). `first_join` is also `None` for these accounts.

Canonical handling — don't invent new logic, follow this:

- Test with `orm.is_last_online_unknown(dt)` (true if `dt` is within 24h of the epoch). `orm.py` explicitly comments: **"never compare directly."**
- `/purgelist` ([`cogs/activity/activity.py`](../cogs/activity/activity.py)) segregates these rows into an "Unknown (API disabled)" bucket — never "inactive".
- [`cogs/activity/server_watcher.py`](../cogs/activity/server_watcher.py) (2-min loop) tracks them via two complementary signals — Wynn `server`-field world-change observation (branch A) and any strict increase across the `last_stat_snapshot` JSON of ~17 monotonic counters from the /v3/player envelope (branch B: `playtime`, `contentCompletion`, `wars`, `totalLevel`, `mobsKilled`, `chestsFound`, `worldEvents`, `lootruns`, `caves`, `completedQuests`, `dungeons.total`, `raids.total`, `raidStats.*`, `pvp.*`) — and bumps `last_online=now` when either fires. Scope: Returners + waitlisted (added 2026-05-16) accounts whose `lastJoin` was observed hidden at the last guild tick (`lastjoin_hidden_at` non-null) OR whose `last_online` is still the epoch sentinel. The `lastjoin_hidden_at` clause closes an asymmetry bug (fixed 2026-06-07): previously the watcher only polled the epoch-sentinel bucket, so an account bumped out by a one-shot detection (or a brief lastJoin un-hide caught by `_apply_guild`) would drop out of scope permanently — if they re-hid `lastJoin` afterward, their `last_online` aged silently until `/purgelist`'s freshness re-verify reclassified them into the "Unknown" bucket with no path back. `_apply_guild` now stamps `lastjoin_hidden_at` every tick that observes hidden state and NULLs it the instant `lastJoin` becomes visible.

**Bug class:** any `last_online__lt=cutoff` / `last_online < cutoff` filter without a matching `last_online__gt=UNKNOWN_LAST_ONLINE + timedelta(days=1)` lower bound will treat every API-disabled player as "inactive ~56 years" — silently purging or flagging them. This was the root cause of "waitlisted user vanishes after every restart" in `waitlist_cleanup` ([`cogs/membership/join.py`](../cogs/membership/join.py)). When debugging a dazebot vanish/inactive-flag bug, **check `last_online` for the epoch sentinel first**.

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

## Weekly events / scoring (dazebot.db)

| Table | Owner | Purpose |
|---|---|---|
| `WeeklyEvent` | `cogs/events/weekly_event.py`, `cogs/events/returns/` | Weekly Returners event configuration. Scaffold every return uses — permanent infrastructure, stays in dazebot.db. |
| `Score` | `cogs/events/weekly_event.py` | Per-user scoring for the weekly event. FK to `DiscordAccount`. Stays in dazebot.db. |

## Per-week ephemeral tables (returns.db)

These live in a **separate physical SQLite file** (`/app/data/returns.db`)
because per-week Returners state is ad-hoc and one-shot — see "Why the
two-DB split" above. Defined in [orm_returns.py](../orm_returns.py); the
`returns` Tortoise app is bound to the `returns` connection.

| Table | Owner | Purpose |
|---|---|---|
| `ReturnGuess` | `cogs/events/returns/week_75.py` | One user's emerald-price guess for a `/return 75` day-N slot. Unique on `(week, day, disc_uuid)`. `disc_uuid` is a plain `CharField` (no FK across DB files). |
| `StorySegment` | `cogs/events/returns/week_73.py` | One fragment of a `/return 73` collaborative story. The back-to-back guard reads the most recent row's `disc_uuid` to forbid the same author twice running. |

## Alerts (rare, edge-case-driven)

| Table | Owner | Purpose |
|---|---|---|
| `DeadGuildAlert` | `cogs/activity/activity.py` | Records that a "dead guild" alert was raised so we don't re-spam staff. |
| `GuildCapacityAlert` | `cogs/activity/activity.py` | Same shape, for capacity-based alerts. |
| `HiatusSpottedAlert` | `lib/mc/hiatus_alerts.py` | Per-UUID 24h cooldown marker for the "hiatus user is online" post in `#activity`. One row per post; the watchers query the most recent row per `uuid`. |
| `HiatusReturnNotice` | `lib/mc/hiatus_return_dm.py` | Per-**Discord-user** state for the "welcome back from your hiatus" DM: `last_sent_at` (the "since the last time this was sent" half of the repeat gate), `muted` (permanent opt-out, only `/alerts return_dm_clear` lifts it), and `snooze_armed`/`snoozed_at` (one-shot bypass of the repeat gate, consumed on the next login). Keyed by person, not MC uuid — one human with three alts is one recipient. |
| `Shout` | (rare) | One-off staff shout broadcasts. |
| `LinkRequest` | (legacy) | Predates the `LinkCode` flow. Inspect before adding new code that uses it. |

## Cult subsystem

| Table | Owner | Purpose |
|---|---|---|
| `Cult` | `cogs/events/returns/week_0.py` | Returners "cults" (sub-groups). |
| `CultMembership` | `cogs/events/returns/week_0.py` | Membership of a Discord user in a cult. |
| `IntercultMessage` | `cogs/events/returns/lib/views/intercult_view.py` | Append-only log of cross-cult messages sent via the pinned intercult button. The 24h-per-cult cooldown is derived from the most recent row per `sender_cult_name`. Stores names (not FKs) so a future cult rename leaves history pointing at the name-at-time. |
| `RecruitmentQuery` | `cogs/events/returns/lib/views/recruitment_view.py` | Append-only log of clicks on the pinned recruitment button (lists online players not in any cult). The 1h-per-cult cooldown is derived from the most recent row per `cult_name`. Same name-not-FK pattern as `IntercultMessage`. |

## Chore-Torn Palace (CTP)

| Table | Owner | Purpose |
|---|---|---|
| `CTPBoard` | `cogs/rewards/ctp/ctp.py` (`~ctp board`) | One row per reward board (DEV / TXT / ART / ...). `enum` + `board_number` + optional `role_id`. |
| `CTPBoardMembership` | `cogs/rewards/ctp/ctp.py` (`~ctp assign` / `~ctp revoke`) | Manual staff-assigned (`disc_account`, `board`) association. Unioned with role-derived membership by `_board_memberships`. Assign/revoke also grant/strip the board's `role_id` as a one-shot side effect — see [ctp.md](ctp.md) "Board membership". |
| `CTPPrize` | `cogs/rewards/ctp/ctp.py` (`~ctp prize ...`) | Editable prize catalog. Unique on `(category, enum_name)`. `disabled` hides without deleting; `duration_seconds=None` means one-time. |
| `CTPLedger` | `cogs/rewards/ctp/lib/balance.py` | Append-only point-movement log. Balance = `SUM(amount_delta)` per user; never store a balance column. `source` discriminates rendering (`reward` / `redeem` / `gift_sent` / `gift_received` / `glint_invest` / `admin_set`). Snapshots prize fields + `expires_at` so a prize edit / delete leaves history rendering intact. |
| `CTPGlintInvestment` | `cogs/rewards/ctp/lib/glints.py` | Cumulative-only `total_invested` per Discord user (1:1, never decrements). Separate from the ledger so the leaderboard is one SELECT; the matching negative ledger row (`source='glint_invest'`) is what debits balance. |

See [ctp.md](ctp.md) for the schema invariants, the overloaded `~ctp board` parsing, and the glints-eligibility rule (MEMBER / WAITLISTED / HONOURARY only on the leaderboard).

## Donations

| Table | Owner | Purpose |
|---|---|---|
| `Donation` | `cogs/rewards/donations/donations.py` (`~donations`) | One staff-recorded in-game donation to the guild. PK is auto-increment int so `~donations info <id>` is human-typeable. `value_emeralds` is the staff-assigned emerald-equivalent valuation (BigInt). `image_urls_json` is a JSON list of Discord CDN URLs (null when no attachments). FK to `MinecraftAccount` is `RESTRICT`: deleting an MC account must not orphan donation history. See [donations.md](donations.md) for the 5%-milestone rule and the glint-slots 7-8 wiring. |

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

1. Edit the model in [`orm.py`](../orm.py) (dazebot.db) or
   [`orm_returns.py`](../orm_returns.py) (returns.db).
2. `uv run aerich --app models migrate --name "describe_change"` for
   dazebot.db, or `uv run aerich --app returns migrate --name "..."` for
   returns.db — generates a new file under `migrations/<app>/`.
3. Inspect the generated file (`upgrade()` and `downgrade()` SQL); commit it.
4. Restart the bot; `init_db()` applies pending migrations on both apps automatically.

`init_db()` (in [`orm.py`](../orm.py)) handles three states:

- **Fresh DB** (file absent / no model tables): refused unless `DAZEBOT_ALLOW_FRESH_DB=1` is set in the environment. The flag is the operator stating "yes, I really mean a clean install" — without it a missing prod DB file aborts startup instead of silently re-creating empty tables (see §"2026-05-23 incident" below). With the flag, `aerich.Command.upgrade()` runs the initial migration's `CREATE TABLE`s normally.
- **Existing pre-aerich DB** (model tables present, no `aerich` table): back up the file to `{db_path}.pre-migration-{epoch}`, provision the tracking table directly, fake-apply ONLY the initial migration via `_fake_apply_initial_migration`, then run `upgrade(fake=False)` so any follow-up migrations execute for real. This is the one-time bootstrap path on first deploy after the aerich rollout.
- **Already-bootstrapped DB**: back up the file, then `upgrade()` applies any pending follow-up migrations; no-op when up-to-date.

The detection is read-only (`sqlite_master` query). The fake-apply path is idempotent. Inspect manually:

```bash
docker exec dazebot sqlite3 /app/data/dazebot.db 'SELECT version, app FROM aerich;'
```

Every schema-touching boot leaves `dazebot.db.pre-migration-<epoch>` and (if present) `dazebot.db.pre-migration-<epoch>-wal` siblings beside the live DB. They accumulate; rotate them by hand if disk is tight.

## 2026-05-23 incident — total prod data wipe on aerich rollout

What happened: the first deploy of the aerich-wired `init_db` started against a container whose `/app/data/dazebot.db` was momentarily unreadable (likely a bind-mount race during `docker compose up -d`, never definitively root-caused). The old `_existing_db_needs_fake_apply` swallowed the underlying `sqlite3.OperationalError` and returned `False`. `init_db` therefore took the "fresh DB" branch, the initial migration's `CREATE TABLE IF NOT EXISTS` ran (no-op on existing tables — except those tables had vanished from the bot's view), and the bot started up with an empty schema. The activity loop then began repopulating `minecraft_accounts` from the WAPI within seconds, masking the wipe with fake-looking activity.

Three changes prevent a repeat:

1. `_inspect_db_state` (new) — raises `RuntimeError` on `sqlite3.Error` instead of falling through to "fresh DB". A transient read failure now aborts the boot.
2. `init_db` requires `DAZEBOT_ALLOW_FRESH_DB=1` to take the fresh-install branch. Missing file → boot fails loudly; operator must explicitly opt in.
3. `_backup_db_before_migration` runs before any DDL, leaving a timestamped backup beside the live file. Recovery from a future incident is `cp` + restart instead of forensics.

Recovery script: [`vets-deploy/scripts/one-off/restore-dazebot-db.sh`](../../vets-deploy/scripts/one-off/restore-dazebot-db.sh). Takes a backup file as input, stops the stack, preserves the wiped DB at `dazebot.db.wiped-<timestamp>`, installs the backup, restarts, prints aerich state + row counts.

Production safety: The `dazebot.db-wal` sidecar can hold uncommitted writes if the container exits via SIGKILL. `runtime_config.set_override` issues a `PRAGMA wal_checkpoint(TRUNCATE)` after every write to mitigate this. Other write-heavy cogs *should* but don't always do the same; on a restart-during-write you may see a small recent change "rollback".

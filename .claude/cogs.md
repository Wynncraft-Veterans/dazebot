# Cogs reference

Quick map of [dazebot/cogs/](../cogs/). Detailed behaviour lives in the cog files themselves; this is for "where do I look for X" navigation.

Auto-discovery: `bot.py:_load_cogs` walks every `.py` file under `cogs/` (recursing into subfolders, ignoring `__*` and any directory in `COG_SKIP_DIRS` — currently just `returns/`). Drop a new file in any subfolder and it's loaded.

## `cogs/membership/` — identity, state, and onboarding

| Cog | Owns |
|---|---|
| [`join.py`](../cogs/membership/join.py) | Two periodic janitor loops only: `clear_old_requests` (5 min — drops `LinkRequest` rows whose MC or Discord side was since linked elsewhere) and `waitlist_cleanup` (1 min — refreshes stale waitlist entries against the Wynncraft API and drops entries now in a guild or inactive ≥9 days; it now also strips the `WAITLISTED` role via `apply_transition(INACTIVE_WAITLIST)` when it deletes a row, so the role never dangles without a DB row). It does **not** re-run `ensure_linked_baseline`; the baseline self-heal loop lives in [`cogs/maintenance/janitor.py`](../cogs/maintenance/janitor.py) reconciler (A), not here (see [role_state.md](role_state.md)). |
| [`vanity_roles.py`](../cogs/membership/vanity_roles.py) | Per-member year/date cosmetic role auto-assignment driven by Wynncraft `firstJoin`. |
| [`blocking.py`](../cogs/membership/blocking.py) | `/block`, `/unblock`. |
| [`waitlist.py`](../cogs/membership/waitlist.py) | `/waitlist add\|view\|remove\|force\|self\|leave`. The `WAITLISTED` role is kept in lock-step with the `Waitlist` DB table: `add`/`self` apply the role (and `add` reports success if the role was already present — DB+role re-synced), `remove`/`leave` strip it via `apply_transition(INACTIVE_WAITLIST)`. `force` reorders by rewriting one row's `created_at` (position = `created_at` ordering) and deliberately touches neither roles nor row existence. The Waitlist table is the source of truth; janitor reconciler (D) heals any residual divergence. |
| [`membership_state.py`](../cogs/membership/membership_state.py) | `/first_install`, `/script edit_welcome\|rename_cult`, `/force change\|check`, `/vanity set\|force`, `/honour`, `/unhonour`, `/list unlinked\|linked`, `/info`, and the periodic `inactivity_loop`. |
| [`runtime_config_cog.py`](../cogs/membership/runtime_config_cog.py) | `/config list\|get\|set\|reset` and `/alerts status\|mute\|unmute\|thresholds`. |

## `cogs/moderation/` — staff actions

| Cog | Owns |
|---|---|
| [`admin.py`](../cogs/moderation/admin.py) | `/admin sync\|reload\|load\|unload`, `/say`, `/embed`, `/shouts set`, and the full `/link` group (set, remove, check, alt, request, requests, approve, code, info). |
| [`staff_actions.py`](../cogs/moderation/staff_actions.py) | `/warnings` read-only display for caution/warning/eject records; admin override commands. Writes come from the in-game API (`/api/internal/staff-action`). |
| [`anni.py`](../cogs/moderation/anni.py) | Anniversary listener. |

## `cogs/activity/` — Wynncraft polling

| Cog | Owns |
|---|---|
| [`activity.py`](../cogs/activity/activity.py) | Periodic Returners-guild scan; fires `JOINED_VETS`/`BECAME_GUILDLESS`/`JOINED_OTHER_GUILD`/`INACTIVE_*` triggers via `lib/role_state.apply_transition`. Hosts `/purgelist` and the `/shout` group. `_check_guild` has a plausibility write-guard: if the Wynncraft guild response returns implausibly few members vs. what's stored (`GUILD_SCAN_MIN_PLAUSIBLE_*`), it suppresses **both** join- and leave-detection that tick — a truncated/degraded API response no longer mass-downgrades members. |
| [`server_watcher.py`](../cogs/activity/server_watcher.py) | 3-min poll of Returners members whose `lastJoin` is privacy-hidden. Reads the `/v3/player` `server` field; between-tick changes are treated as activity and bump `last_online`. |

## `cogs/maintenance/` — data-integrity janitor

| Cog | Owns |
|---|---|
| [`janitor.py`](../cogs/maintenance/janitor.py) | Periodic reconciliation loop (`JANITOR_INTERVAL_MINUTES`, default 6h) + staff `/janitor run`. Four reconcilers, run **(F)→(A)→(D)→(B)**: **(F)** blocklisted users' linked members forced REGISTERED-only (blocklist *is* positive info — spec §2b); **(A)** the Flame self-heal, *strictly* — only linked members with **no** membership state at all get the baseline **granted** (additive; never a demotion). Members with any valid primary role, **including manually-assigned HIATUS/HONOURARY**, are left untouched; genuine multi-primary conflicts are left for manual review. **(D)** WAITLISTED ↔ Waitlist-DB sync — the DB table is the source of truth: an orphaned role with no row is removed, a row whose member lacks the role gets it (runs after (A) so a just-healed stateless member can be waitlisted). **(B)** leftover `LinkCode` cleanup — unrecoverable rows deleted; for a linked user, a stale row (user already has a valid state) is just deleted *without touching roles*, only a *stateless* user triggers an additive baseline re-enforce. Log-only by default (`JANITOR_REPAIR_ENABLED=False`); flips to actually mutate at runtime. **Either way** it posts a throttled (`JANITOR_ALERT_DELTA`) summary embed to `JANITOR_ALERT_CHANNEL`, throttle bypassed when a tick actually repaired. `JANITOR_ENABLED`/interval are read at init/decoration → restart to change; `JANITOR_REPAIR_ENABLED` is the live switch. Throttle marker = `JanitorAlert` ORM table (mirror of `DeadGuildAlert`). |

`cogs/maintenance/` is a normal auto-loaded subfolder (not in `COG_SKIP_DIRS`); the empty `__init__.py` is the subpackage marker like every other `cogs/*` folder.

## `cogs/events/` — return weeks + scoring

| Cog | Owns |
|---|---|
| [`return_cmd.py`](../cogs/events/return_cmd.py) and [`returns/`](../cogs/events/returns/) | `/return <id>` dispatcher + per-week handlers. Each `week_N.py` decorates a function with `@register(N)` to register itself in `REGISTRY`. |
| [`weekly_event.py`](../cogs/events/weekly_event.py) | `/score` group and `/count` (forum reaction tallies). |

## `cogs/integrations/` — outside-of-Discord glue

| Cog | Owns |
|---|---|
| [`api_server.py`](../cogs/integrations/api_server.py) | Mounts the FastAPI app inside the bot's asyncio loop on `$DAZEBOT_PORT`. The actual routes live in [`api/main.py`](../api/main.py). |
| [`vetsmod.py`](../cogs/integrations/vetsmod.py) | `/vetsmod` (self-issue) + `/change remove key`/`/change rotate key` (staff). DMs the modrinth link + `/unlock <key>`. Falls back to `LINK_FALLBACK_CHANNEL` ping if DMs closed (`VetsmodFallbackView`). See [verify_keys.md](verify_keys.md). |

## `cogs/meta/` — utility commands

| Cog | Owns |
|---|---|
| [`documentation.py`](../cogs/meta/documentation.py) | `/docs ...`. |
| [`pointless.py`](../cogs/meta/pointless.py) | `/randomfact`. |
| [`utility.py`](../cogs/meta/utility.py) | `/findprofer` (profession matchmaking). |
| [`build_library.py`](../cogs/meta/build_library.py) | Build promotion thread-clone flow. |

## Persistent views

`bot.py:setup_hook` registers these explicitly so the buttons survive bot restarts (otherwise discord.py loses them on reconnect):

- `FirstInstallView` (`lib/discord_utils/first_install_view.py`) — the "Link my account" button in the welcome flow
- `LinkFallbackView` (`lib/discord_utils/first_install_view.py`) — public-channel fallback when DM is closed
- `VetsmodFallbackView` (`cogs/integrations/vetsmod.py`) — same idea for the `/vetsmod` flow
- `IntercultButtonView` (`cogs/events/returns/lib/views/intercult_view.py`) — pinned in each cult thread by `/script install_intercult`; click opens an ephemeral select → modal flow that sends one message to another cult's thread (mirrored back to the sender's thread). Rate-limited to one outbound message per cult per 24h via `IntercultMessage` rows.
- `RecruitmentButtonView` (`cogs/events/returns/lib/views/recruitment_view.py`) — pinned in each non-deercult cult thread by `/script install_recruitment` (and in deercult separately by `/script install_recruitment_deercult`). Click fetches the live online-players list (Wynncraft guild API merged with temporary-server's VetsMod-connected list), filters out anyone with a `CultMembership`, and replies ephemerally with the unaffiliated usernames. Rate-limited to one query per cult per 1h via `RecruitmentQuery` rows.

If you add a button that needs to survive a restart, register it in `setup_hook`. discord.py only re-attaches handlers for views explicitly added via `bot.add_view(view)`.

## Adding a cog

1. Pick the most appropriate subfolder (or add a new one if no existing one fits). Create `<subfolder>/<name>.py` with a `class Whatever(commands.Cog)` and an `async def setup(bot): await bot.add_cog(Whatever(bot))`.
2. That's it — auto-discovery picks it up. No registration anywhere else.
3. If your cog has slash commands, they'll be `await bot.tree.sync()`'d on next `setup_hook` (or via `/admin sync` — see `cogs/moderation/admin.py`).

If your cog has a persistent view, register it in `bot.py:setup_hook`. Otherwise the buttons silently break on every restart.

If the new file is a helper that should NOT be loaded as a cog (no `setup()`), either place its parent directory in `bot.COG_SKIP_DIRS` (currently just `returns`) or rely on the `NoEntryPointError` safety net which logs at DEBUG and moves on.

## Pitfalls

- **`ctx.reply(..., ephemeral=True)` only honors `ephemeral` for slash invocations.** For `@commands.hybrid_command` cogs, the prefix path (e.g. `~return 75`) silently ignores the flag and posts the reply *publicly* in the channel — including any data you assumed was private. This is privacy-critical for surfaces that carry user-only data (guesses, link codes, key material). Route those through a DM-or-ephemeral helper instead — `cogs/events/returns/week_75.py:_private` is the reference pattern: ephemeral when `ctx.interaction is not None`, DM the author and delete the invoking message otherwise, with a non-revealing channel fallback if DMs are closed. The same trap applies to `ctx.send(..., ephemeral=True)`.

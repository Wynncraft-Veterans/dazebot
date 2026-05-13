# Cogs reference

Quick map of [dazebot/cogs/](../cogs/). Detailed behaviour lives in the cog files themselves; this is for "where do I look for X" navigation.

Auto-discovery: `bot.py:_load_cogs` walks every `.py` file under `cogs/` (recursing into subfolders, ignoring `__*` and any directory in `COG_SKIP_DIRS` — currently just `returns/`). Drop a new file in any subfolder and it's loaded.

## `cogs/membership/` — identity, state, and onboarding

| Cog | Owns |
|---|---|
| [`join.py`](../cogs/membership/join.py) | Periodic janitor that re-runs `ensure_linked_baseline` for every linked user (catches stale states from prior failures). |
| [`vanity_roles.py`](../cogs/membership/vanity_roles.py) | Per-member year/date cosmetic role auto-assignment driven by Wynncraft `firstJoin`. |
| [`blocking.py`](../cogs/membership/blocking.py) | `/block`, `/unblock`. |
| [`waitlist.py`](../cogs/membership/waitlist.py) | `/waitlist add\|view\|remove\|self\|leave`. |
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
| [`activity.py`](../cogs/activity/activity.py) | Periodic Returners-guild scan; fires `JOINED_VETS`/`BECAME_GUILDLESS`/`JOINED_OTHER_GUILD`/`INACTIVE_*` triggers via `lib/role_state.apply_transition`. Hosts `/purgelist` and the `/shout` group. |
| [`server_watcher.py`](../cogs/activity/server_watcher.py) | 3-min poll of Returners members whose `lastJoin` is privacy-hidden. Reads the `/v3/player` `server` field; between-tick changes are treated as activity and bump `last_online`. |

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

If you add a button that needs to survive a restart, register it in `setup_hook`. discord.py only re-attaches handlers for views explicitly added via `bot.add_view(view)`.

## Adding a cog

1. Pick the most appropriate subfolder (or add a new one if no existing one fits). Create `<subfolder>/<name>.py` with a `class Whatever(commands.Cog)` and an `async def setup(bot): await bot.add_cog(Whatever(bot))`.
2. That's it — auto-discovery picks it up. No registration anywhere else.
3. If your cog has slash commands, they'll be `await bot.tree.sync()`'d on next `setup_hook` (or via `/admin sync` — see `cogs/moderation/admin.py`).

If your cog has a persistent view, register it in `bot.py:setup_hook`. Otherwise the buttons silently break on every restart.

If the new file is a helper that should NOT be loaded as a cog (no `setup()`), either place its parent directory in `bot.COG_SKIP_DIRS` (currently just `returns`) or rely on the `NoEntryPointError` safety net which logs at DEBUG and moves on.

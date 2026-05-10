# Cogs reference

Quick map of [dazebot/cogs/](../cogs/). Detailed behaviour lives in the cog files themselves; this is for "where do I look for X" navigation.

Auto-discovery: `bot.py:_load_cogs` globs every `.py` in `cogs/` (ignoring `__*`). Drop a new file in and it's loaded.

## Identity / membership

| Cog | Owns |
|---|---|
| [`join.py`](../cogs/join.py) | Persistent first-install link button view. Periodic janitor that re-runs `ensure_linked_baseline` for every linked user (catches stale states from prior failures). |
| [`activity.py`](../cogs/activity.py) | Periodic guild-membership check via Wynncraft API. Fires `JOINED_VETS`/`BECAME_GUILDLESS`/`JOINED_OTHER_GUILD`/`INACTIVE_*` triggers via `lib/role_state.apply_transition`. |
| [`management.py`](../cogs/management.py) | Big one. `/block`, `/unblock`, `/honour`, `/unhonour`, `/force change`, `/check`, `/list (linked\|unlinked)`, `/info`, `/waitlist {add,view,remove,self,leave}`, `/config {list,get,set,clear}`, `/add` (alts), `/cult`, `/script`, `/edit_welcome`. Most staff-facing slash commands live here. |
| [`vanity_roles.py`](../cogs/vanity_roles.py) | Self-assignable year/date roles — `/vanity set`, `/vanity force` (staff). |
| [`vetsmod.py`](../cogs/vetsmod.py) | `/vetsmod`, `/vetsmod rotate`, `/vetsmod revoke @user`. DMs the modrinth link + `/unlock <key>`. Falls back to `LINK_FALLBACK_CHANNEL` ping if DMs are closed (`VetsmodFallbackView`). See [verify_keys.md](verify_keys.md). |

## Comms / chat

| Cog | Owns |
|---|---|
| [`api_server.py`](../cogs/api_server.py) | Mounts the FastAPI app inside the bot's asyncio loop on `$DAZEBOT_PORT`. The actual routes live in [`api/main.py`](../api/main.py). |
| [`api.py`](../cogs/api.py) | Empty stub — `joindate` was merged into `/info`. Don't add new commands here; use `management.py` or a new dedicated cog. |
| [`return_cmd.py`](../cogs/return_cmd.py), [`returns/`](../cogs/returns/) | `/return` command + per-week return cogs. |
| [`pointless.py`](../cogs/pointless.py) | Trivia / fun commands. |
| [`utility.py`](../cogs/utility.py) | Misc utility commands. |
| [`weekly_event.py`](../cogs/weekly_event.py) | Weekly event scheduling (drives `WeeklyEvent` + `Score` tables). |

## Other

| Cog | Owns |
|---|---|
| [`admin.py`](../cogs/admin.py) | Owner/admin commands. Reload, sync, etc. |
| [`anni.py`](../cogs/anni.py) | Annihilation timer. |
| [`documentation.py`](../cogs/documentation.py) | `/help`-style commands. |

## Persistent views

`bot.py:setup_hook` registers these explicitly so the buttons survive bot restarts (otherwise discord.py loses them on reconnect):

- `FirstInstallView` (`lib/first_install_view.py`) — the "Link my account" button in the welcome flow
- `LinkFallbackView` (`lib/first_install_view.py`) — public-channel fallback when DM is closed
- `VetsmodFallbackView` (`cogs/vetsmod.py`) — same idea for the `/vetsmod` flow

If you add a button that needs to survive a restart, register it in `setup_hook`. discord.py only re-attaches handlers for views explicitly added via `bot.add_view(view)`.

## Adding a cog

1. Create `cogs/<name>.py` with a `class Whatever(commands.Cog)` and an `async def setup(bot): await bot.add_cog(Whatever(bot))`.
2. That's it — auto-discovery picks it up. No registration anywhere else.
3. If your cog has slash commands, they'll be `await bot.tree.sync()`'d on next `setup_hook` (or via `/sync` — check `admin.py`).

If your cog has a persistent view, register it in `bot.py:setup_hook`. Otherwise the buttons silently break on every restart.

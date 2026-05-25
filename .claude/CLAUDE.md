# dazebot

In-house Discord bot for the Wynncraft "Returners" veterans community. Responsibilities, in rough size order:

- Discord identity + role state for every member (REGISTERED / HIATUS / MEMBER / HONOURARY / WAITLISTED — see [membership_spec.md §6](membership_spec.md#6--role-automation-behaviour) for the transition table).
- Account linking between Discord and Minecraft via the auth-stack chat-line flow.
- The guild waitlist, vanity roles, "honourary" role lifecycle, blocklist, and per-week return commands.
- Wynncraft-API-driven activity tracking (DMing inactive members, dropping waitlist entries after Y days, etc.).
- Issuing + introspecting vetsmod `/unlock` keys (see [auth.md](auth.md)).
- Various one-shots: trivia, annihilation timer, weekly event scheduling, supporter detection.

> The Python package name in `pyproject.toml` is `nazbot` for historical reasons, but the bot itself is **dazebot**. *nazbot* is the separate Discord bot in `../temporary-server` that handles the in-game ↔ Discord chat bridge — don't conflate the two.

## Discord bots in this workspace — command prefixes

dazebot's prefix-command leader is **`~`** (set via the `PREFIX` env var). The vets ecosystem runs four Discord bots, each with its own prefix. The table is duplicated across each bot's repo to keep the mapping discoverable from any vantage point:

| Bot | Repo | Prefix |
|-----|------|--------|
| **dazebot** | `dazebot` (this repo) | `~` |
| nazbot | `../temporary-server` | `!` |
| fishbot | `../vets-anni` | `\` |
| dynobot | (third-party, no repo) | `?` |

Slash commands are unprefixed. The prefix only applies to text/message commands.

> Canonical membership requirements: [membership_spec.md](membership_spec.md). Subsystem references: [role-state machine](role_state.md), [auth flows](auth.md) ([VerifyKey](verify_keys.md), [linking](linking.md)), [runtime config](runtime_config.md), [data model](data_model.md), [cogs map](cogs.md). [`MEMORY.md`](MEMORY.md) is the index.

## Key facts

- **Repo layout:** flat — repo root is `dazebot/`, with source files (`bot.py`, `cogs/`, `lib/`, `api/`, `orm.py`, etc.) sitting directly inside.
- **Stack:** Python 3.13, discord.py, tortoise-orm (SQLite), FastAPI (sidecar HTTP for sibling services)
- **DB:** SQLite at `/app/data/dazebot.db` in production (mounted via `./data:/app/data`), `dev.db` at the repo root locally
- **Public surface:** none — Discord-only on the public side; HTTP sidecar bound to `127.0.0.1:${DAZEBOT_PORT}` and reachable on the internal `verify` Docker network
- **Repo on GitHub:** `Wynncraft-Veterans/dazebot` (private)

## Related repos (same workspace)

- `../temporary-server` — Calls our `POST /api/auth/introspect` to validate vetsmod `/unlock` keys.
- `../vetsmod` — The Fabric mod whose users get keys via our `/vetsmod` slash command.
- `../auth-stack` — The PicoLimbo fork that POSTs every chat line to our `GET /api/auth/{uuid}/{msg}` for the link-code consumption flow (a *separate* auth path from the vetsmod `/unlock` key flow — same FastAPI app, different endpoint, different purpose).
- `../vets-deploy` — Where the bot runs in production (compose stack at `stacks/dazebot/`).

## Architecture

```
dazebot/                 (repo root)
  bot.py              Bot subclass + setup_hook (init_db, load runtime overrides, register persistent views, load cogs)
  config.py           Compiled defaults (CurrConfig), role/channel IDs, blocklist channel, etc.
  orm.py              Tortoise models — see "Data model" below
  api/main.py         FastAPI app:
                        GET  /health
                        GET  /api/auth/{uuid}/{msg}        — picolimbo link-code probe
                        POST /api/auth/introspect          — vetsmod key introspection (X-Introspect-Secret gated)
                        POST /api/internal/rank-alert      — temp-server-forwarded guild rank-change alerts (BAN/KICK)
  cogs/
    activity.py        Periodic guild check + role enforcement
    admin.py           Owner/admin commands
    anni.py            Annihilation timer
    api.py             (empty stub — joindate merged into /info)
    api_server.py      Mounts the FastAPI app inside the bot's asyncio loop on $DAZEBOT_PORT
    documentation.py   /help-style commands
    join.py            Guild-join handlers
    management.py      Block/unblock, force, vanity, honour, list, info, waitlist, config, alerts
    pointless.py       Trivia / fun commands
    return_cmd.py      /return command
    returns/           Per-week return cogs
    utility.py         Misc utility commands
    vanity_roles.py    Self-assignable year/date roles
    vetsmod.py         /vetsmod (self-issue), /change remove|rotate key (staff), DM-or-fallback view
    weekly_event.py    Weekly event scheduling
  lib/
    rank_alerts.py     post_rank_alert + WAPI verify-and-tag for the rank-alert HTTP endpoint
    auth.py            is_admin, is_staff, is_operator, is_registered, is_guild predicates
    converters.py      CaseInsensitiveMember
    discord_paginated_embed.py  Pagination helper
    first_install_view.py       /first_install link button + DM-or-fallback view (LinkFallbackView)
    lib.py             Misc shared
    linking.py         get_or_issue_code (link codes), try_consume_code, dm_or_log
    mc.py              Mojang username resolution
    role_state.py      State/Trigger/transition machine, ensure_linked_baseline, force_to_registered_only
    runtime_config.py  /config admin command — persisted overrides on top of compiled defaults
    vanity_roles.py    Date→role-id resolver
    verify_keys.py     VerifyKey helpers: get_or_issue_key, rotate_key, revoke_key, introspect, resolve_tier
    wynn.py            Wynncraft helpers
    wynn_api/          Tiny REST client for api.wynncraft.com (player, guild, requestor)
```

## Data model (selected)

| Table | Purpose |
|-------|---------|
| `MinecraftAccount` | UUID-keyed MC profile + cached guild + last-online |
| `DiscordAccount` | disc_uuid + (nullable) primary `MinecraftAccount` link |
| `MinecraftAlt` | Additional MC accounts beyond the primary |
| `LinkCode` | One row per pending /first_install code |
| `VerifyKey` | One row per Discord user with a vetsmod key. 43-char URL-safe base64 bearer token, mc_uuid + mc_username + tier snapshot, last_used_at, revoked_at. See [auth.md](auth.md). |
| `Waitlist` | In-game guild waitlist |
| `Blocklist` | Forced-to-Registered users |
| `UserVanityChoice` | Self-chosen vanity year/date roles |
| `BotConfigOverride` | /config persistence layer |
| `FirstInstallMonitor` | Tracks which message hosts the link button |
| `DMSentLog` | Idempotency for one-shot DMs |
| `MojangNameCache` | UUID→username cache |

## Auth (one of several responsibilities)

Two unrelated flows live under `/api/auth/` on dazebot's FastAPI sidecar — link-code consumption from PicoLimbo and `/unlock`-key introspection from temporary-server. They share a URL prefix and nothing else. **Detail in [auth.md](auth.md).**

The Discord-side commands are in [`cogs/integrations/vetsmod.py`](../cogs/integrations/vetsmod.py) and the `/first_install` view in [`lib/discord_utils/first_install_view.py`](../lib/discord_utils/first_install_view.py).

## Required env vars (production)

| Var | Purpose |
|-----|---------|
| `TOKEN` | Discord bot token. |
| `WAPI_TOKENS` | Wynncraft API token(s) for private guild stats. |
| `DAZEBOT_PORT` | FastAPI bind port inside the container. Bot crashes on boot if missing. |
| `PREFIX` | Discord prefix-command leader. |
| `DAZEBOT_INTROSPECT_SECRET` | See [auth.md](auth.md). Required for the introspection endpoint to function. |

Optional: `MC_PUBLIC_HOST` (default `verify.wynnvets.org`), `DEBUG`, `DAZEBOT_DB_PATH`.

## Cog loading

`bot.py:_load_cogs` globs every `.py` in `cogs/` (ignoring `__*`). Cogs are auto-discovered — drop a new file in and it's loaded. Persistent views (FirstInstallView, LinkFallbackView, VetsmodFallbackView) are explicitly registered in `setup_hook` so buttons survive restarts.

## Schema migrations

Managed by [aerich](https://github.com/tortoise/aerich). Migration files live in [`migrations/models/`](../migrations/models/). `init_db()` applies pending migrations at boot — pre-aerich production DBs are auto-detected and the initial migration is fake-applied (recorded as applied without re-running DDL). Fresh-DB creation is **refused** unless `DAZEBOT_ALLOW_FRESH_DB=1` is set (operator opt-in only — required because a missing prod file caused a total data wipe on 2026-05-23; see [data_model.md](data_model.md) §"2026-05-23 incident"). Every schema-touching boot leaves a `dazebot.db.pre-migration-<epoch>` backup beside the live file. New change: `uv run aerich migrate --name "describe"` then commit the generated file.

## Building / deploying

- Local: `uv run python -m bot` (or `python bot.py`) from the repo root.
- Production: `cd /opt/docker/dazebot/src && git pull && manage update dazebot`. See `../vets-deploy/.claude/CLAUDE.md` and the runbook at `../vetsmod/.claude/ephemeral/auth_deployment_instructions.md`.

## Things to know

- **Schema migrations via aerich.** `init_db()` auto-applies pending migrations on boot. See [data_model.md](data_model.md) for the workflow and the pre-aerich-DB bootstrap path.
- **Persistent views must be re-registered every boot** in `setup_hook` or buttons silently break on every restart. See [cogs.md](cogs.md).
- **The membership-state machine is the only safe path to mutate state-roles.** Don't call `member.add_roles(REGISTERED, …)` directly; go through [role_state.md](role_state.md).
- **Auth-side gotchas** (fail-closed introspection, picolimbo trust model, SQLite single-writer constraint) are in [auth.md](auth.md).

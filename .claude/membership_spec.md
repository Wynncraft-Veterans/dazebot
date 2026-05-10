# Membership spec

Long-term reference for dazebot's membership model: which Discord roles encode which states, what causes them to change automatically, what staff-facing commands override the automation, and what constraints the upstream Wynncraft / Mojang APIs impose. The implementation lives across [`role_state.md`](role_state.md), [`linking.md`](linking.md), [`runtime_config.md`](runtime_config.md), and the cogs catalogued in [`cogs.md`](cogs.md); this doc describes the rules they jointly enforce.

> Section numbers below are anchors used by code comments (`see membership_spec.md §6`). They originated in an earlier file called `instructions1.md`; old references using that name should be updated when encountered, but the section numbers themselves are stable.

---

## §1 — First install

The bot ships with a `/first-install <message ID>` admin command used **once per Discord guild** to bootstrap the link-code monitor. Once it has been run, it should not be run again — re-running it would re-monitor the same message and is not a tested or expected operation.

What it does:

- Begins monitoring the specified post for `:chains:` reactions, indefinitely. Whenever an unregistered user reacts, the bot DMs them an account-link code plus instructions on how to connect to the picolimbo server to consume it. The monitor survives bot restarts via the `FirstInstallMonitor` table (see [`data_model.md`](data_model.md)).
- Wipes pre-existing membership state-roles (legacy IDs) and vanity roles in that guild, on the assumption that the install is the cutover from a pre-bot world to bot-managed roles.

The cutover behaviour is tied to the bootstrap; it is not a general-purpose "reset" path.

## §2 — Manual override commands

Staff and admin commands provided by [`cogs/management.py`](../cogs/management.py). See also [`cogs.md`](cogs.md) for the full cog map.

**Permission model.** The "staff" role ID gates the staff-tagged commands below; admin-tagged commands additionally require Discord's server-administrator permission. Both role IDs and the permission requirement are sourced from `config.py` and runtime-overridable via `/config` ([§3](#3--runtime-configuration)).

| Command | Audience | Behaviour |
|---|---|---|
| `/block <user>` | staff | Adds the linked MC account to the `Blocklist` table (see [§2b note](#blocklist-invariant)) and forces the user to Registered, stripping any of MEMBER/HIATUS/HONOURARY/WAITLISTED. If the user is currently in the in-game guild, posts an alert to the configured staff channel. |
| `/unblock <user>` | staff | Removes the `Blocklist` row. Does not re-grant any roles — the user becomes eligible for the normal automation again from whatever state they end up in. |
| `/force <user> <transition>` | staff | Manually applies a transition that's not driven by the automation. Currently exposes only `Registered → Hiatus` (used when a user retroactively asks to be marked as a returning ex-member). New options must not conflict with the automation table in [§6](#6--role-automation). |
| `/register <user> <username>` | staff | Force-couples a Discord user to a Minecraft username without going through the link-code flow. Useful when picolimbo is unreachable or the user can't get in-game. |
| `/add <user> <username>` | staff | Adds an alt to a user's existing primary link. The primary link still lives on `DiscordAccount.minecraft_account`; this writes a `MinecraftAlt` row. |
| `/honour <user>` | admin | Adds Honourary; removes Registered/Hiatus/Member. Honourary is mutually exclusive with the linked-account baseline (see [§2g](#vanity-choice-persistence)). |
| `/unhonour <user>` | admin | Removes Honourary; adds Registered. The automation will then re-promote the user to MEMBER on the next activity tick if they're in Returners. |
| `/vanity <year-or-date>` | user | User self-assigns a vanity role corresponding to their original join era. Available even when the user's `firstJoin` is null (see [§7](#7--wynncraft-api-privacy)). The "<1.0/2013" role is reserved for staff assignment and cannot be self-picked. |
| `/vanity <user> <year-or-date>` | staff | Force-assigns a vanity role on someone else's behalf, including the staff-only "<1.0/2013" role. |
| `/list unlinked` | staff | Lists in-game Returners members who don't have a Discord link yet. Used to chase down stragglers after a recruitment push. |
| `/username <user>` | staff | Lists all Minecraft accounts linked to the Discord user (primary + alts). |
| `/waitlist add <user> [username]` | staff | Adds the user to the waitlist (`Waitlist` table + WAITLISTED role). Username is optional if the user is already linked; required otherwise (the command will issue a force-link). |

### Blocklist invariant

Users on the `Blocklist` table are *always* held at Registered. Even if a downstream automation event (e.g. the user joins Returners in-game) would normally promote them, the linked-account baseline enforcement leaves them at Registered. The blocklist is the only state that overrides §6 automation.

### Vanity-choice persistence

A user-chosen vanity role (via `/vanity <year-or-date>`) is recorded in the `UserVanityChoice` table and **must not be overridden** by `firstJoin`-based automatic assignment. The table acts as a mute marker: as long as a row exists for that `disc_uuid`, the automatic vanity assignment treats the role as user-managed and leaves it alone. Staff `/vanity <user> <year-or-date>` writes the table with `chosen_by_staff=True` so it's still considered user-respecting on subsequent automation runs.

## §3 — Runtime configuration

Anything that's configurable via constants in `config.py` is also overridable at runtime via the `/config` admin command, persisted in the `BotConfigOverride` table, and applied on every bot boot. This is a hard invariant: a config knob that's *only* in `config.py` is a bug — it forces a redeploy for what should be a slash-command tweak.

See [`runtime_config.md`](runtime_config.md) for the type-coercion rules and which kinds of values are overridable.

## §4 — Stale Wynncraft usernames

Wynncraft's guild API can return an extremely old username for a player — the name they had when they joined the guild, not their current Mojang name. Since `Wynncraft-Veterans/v3/guild/Returners` was extended to include `legacyName`, the canonical handling is:

- `username` is the **current** Minecraft username. Treat as canonical for display.
- `legacyName`, when present, is an **older** username that may still be returned by other Wynncraft endpoints. Use it as an additional candidate when reconciling a guild API row to a local record. Don't display it.
- For sort/dedup operations across the guild API, prefer matching by `uuid`. Fall back to `(username, legacyName)` only when the join key is name-based.

The `name_candidates()` helper on the guild model encodes this.

## §5 — Mojang lookups

Mojang's name servers have stricter rate limits than other parts of the Wynncraft ecosystem. Username/UUID lookups follow this preference order, all of them backed by the persistent `MojangNameCache` table (UUID ↔ name + timestamp):

1. **Preferred:** ashcon — `https://api.ashcon.app/mojang/v2/user/{uuid_or_name}`. Generally fine, no documented strict limits.
2. **Fallback:** `https://mcuuid.net/`. Known lenient rate limits.
3. **Last resort:** `https://api.minecraftservices.com/minecraft/profile/lookup`. Mojang official; high limit on paper but heavy abuse-throttling in practice.

The cache absorbs repeated lookups; upstream is only hit on cache miss or after the entry's TTL expires. New code adding a lookup should reuse the cache rather than calling any of the three upstreams directly.

## §6 — Role automation

The five state-roles (`REGISTERED`, `HIATUS`, `MEMBER`, `HONOURARY`, `WAITLISTED`) and how they change in response to which event. Driven by the activity loop in [`cogs/activity.py`](../cogs/activity.py) and the `/waitlist` command. The implementation is the transition table in [`role_state.md`](role_state.md) (in `lib/role_state.py:compute_transition`).

| Current state                         | Added to VETS waitlist | Joined VETS                            | Became guildless              | Inactive (`X` days member, `Y` days waitlist) | Joined another guild                       |
|---------------------------------------|------------------------|----------------------------------------|-------------------------------|------------------------------------------------|---------------------------------------------|
| **Registered** — never been a member  | + Waitlisted           | − Registered, + Member                 | —                             | —                                              | —                                           |
| **Hiatus** — ex-member, guildless     | + Waitlisted           | − Hiatus, + Member                     | error (already guildless)     | —                                              | − Hiatus, + Registered                      |
| **Member** — currently in VETS        | error                  | error                                  | − Member, + Hiatus            | DM warning at `X` days; no role change         | − Member, + Registered                      |
| **Honourary**                         | + Waitlisted           | − Honourary, + Member                  | —                             | —                                              | —                                           |
| **Registered + Waitlisted**           | error                  | − Waitlisted, − Registered, + Member   | —                             | At `Y` days: − Waitlisted                      | − Waitlisted                                |
| **Hiatus + Waitlisted**               | error                  | − Waitlisted, − Hiatus, + Member       | error                         | At `Y` days: − Waitlisted                      | − Waitlisted, − Hiatus, + Registered        |
| **Honourary + Waitlisted**            | error                  | − Waitlisted, − Honourary, + Member    | —                             | At `Y` days: − Waitlisted                      | − Waitlisted, − Member, + Registered        |

`X` (member inactivity warning) and `Y` (waitlist drop) are runtime-overridable via `/config` ([§3](#3--runtime-configuration)).

"Error state" entries in the table are the trigger/state combinations that should never occur under normal operation — they indicate either an upstream data inconsistency or a bug. The transition function returns a structured `TransitionResult.error` for these so the cog can surface them to staff rather than silently misapplying a delta.

## §7 — Wynncraft API privacy

Per <https://docs.wynncraft.com/privacy>, Wynncraft players can opt out of exposing certain fields. The API may return `null` for any of the following — code consuming these endpoints must be `Optional`-tolerant:

- `joinDate` / `firstJoin`
- `lastJoin` / `lastSeen` / `online`
- `playtime`
- `characters` (may be empty/null)
- guild membership data on the player endpoint

System properties dazebot enforces:

- **Inactivity is unobservable for opted-out users.** A `null` `lastJoin`/`lastSeen` does not trigger inactivity actions. Treat as "unknown / opted-out", leave roles alone.
- **Vanity roles need a verifiable join date.** A `null` `firstJoin` causes the automatic vanity assignment to be a no-op (it doesn't grant *or strip* a role). The `/vanity` self-assign surfaces this as "unable to verify your join date — ask staff to /vanity force you", at which point staff can use the staff form ([§2](#2--manual-override-commands)) which doesn't depend on the API.
- **Roster reconciliation is UUID-keyed, not date-keyed.** Guild member dedup and roster comparisons join on `uuid`; date fields are never assumed to exist.
- **Newly-null fields generate a one-time warning per UUID.** This trips a log entry the first time we see a field that used to be populated come back null, so a privacy-API change on Wynncraft's side surfaces in operations rather than silently breaking automation.

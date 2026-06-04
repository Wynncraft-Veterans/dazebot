# Memory Index

## Big picture
- [CLAUDE.md](CLAUDE.md) — repo overview, related repos, architecture map, env vars
- [Membership spec](membership_spec.md) — canonical requirements for first-install, manual-override commands, runtime config, automated role transitions, and Wynncraft-API privacy handling. Code refers to this as "membership_spec.md §N" (was historically "instructions1.md §N").

## Subsystem reference
- [Role-state machine](role_state.md) — `lib/role_state.py`. The five state-roles, transition table (implementation of [membership_spec §6](membership_spec.md#6--role-automation-behaviour)), `ensure_linked_baseline` invariant, and "go through this, not `member.add_roles`".
- [Runtime config (`/config`)](runtime_config.md) — `BotConfigOverride` table, what's overridable, type coercion, WAL checkpoint after writes.
- [Data model](data_model.md) — every ORM table by subsystem, no-migrations gotchas, relationship diagram.
- [Cogs reference](cogs.md) — what each `cogs/*.py` owns, persistent-view registration, how to add a cog.

## Auth (one of several responsibilities)
- [Auth flows](auth.md) — the two `/api/auth/...` flows that share a URL prefix and nothing else; tier resolution; auth-related env vars; cross-repo trust model.
- [VerifyKey / vetsmod auth](verify_keys.md) — bearer-key issuance, rotation, revocation, introspection contract.
- [Account linking](linking.md) — `LinkCode` flow over auth-stack chat forwarding. Code shape, primary + fallback matching, the "keep row on enforcement failure" rule.

## Features

- [Chore-Torn Palace](ctp.md) — `cogs/rewards/`. Append-only points ledger, prize catalog, glint leaderboard. Schema invariants (no mutable balance column, prize snapshots), permission tiers, `~ctp board` overloaded syntax, MEMBER/WAITLIST/HONOURARY-only glints filter.

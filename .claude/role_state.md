# Role-state machine

A precise reference for [lib/role_state.py](../lib/role_state.py) — the source of truth for which Discord role a member should hold, and the only safe way to mutate those roles.

## Why this exists

Five Discord roles encode a member's relationship to the VETS guild:

| Role constant | Discord role |
|---|---|
| `CurrConfig.ROLE_REGISTERED` | a Discord-linked user, but **not** an in-game member |
| `CurrConfig.ROLE_HIATUS` | an ex-member who has left the in-game guild but might return |
| `CurrConfig.ROLE_MEMBER` | currently in the in-game Returners guild |
| `CurrConfig.ROLE_HONOURARY` | granted manually by staff; orthogonal to MEMBER |
| `CurrConfig.ROLE_WAITLISTED` | wants in but hasn't joined yet (additive flag, not mutually exclusive) |

The transition table for these is defined in [`membership_spec.md` §6](membership_spec.md#6--role-automation-behaviour) and implemented as pure functions in this module. **Don't add or remove these roles directly via `member.add_roles()`** anywhere else in the code — go through `apply_transition`, `force_to_registered_only`, or `ensure_linked_baseline`.

## State enum

```python
class State(enum.Flag):
    NONE = 0
    REGISTERED = enum.auto()
    HIATUS = enum.auto()
    MEMBER = enum.auto()
    HONOURARY = enum.auto()
    WAITLISTED = enum.auto()
```

It's a `Flag` because `WAITLISTED` is additive — a member can be `REGISTERED|WAITLISTED` or `HIATUS|WAITLISTED`. The other four are *intended* to be mutually exclusive among themselves, but the machine handles cleanup of stale combinations rather than relying on that.

`state_of(member)` reads the member's current Discord roles and returns the corresponding `State`. It's the only function that touches the live Discord API for *reads*; everything else operates on the resulting `State` value.

## Transitions (`compute_transition`)

Pure function. Given a current `State` and a `Trigger`, returns a `TransitionResult` with `to_add`, `to_remove`, `error`, and optional `side_effects`. Mirrors [`membership_spec.md` §6](membership_spec.md#6--role-automation-behaviour) verbatim — read that table when in doubt.

| `Trigger` | What fires it |
|---|---|
| `ADDED_TO_WAITLIST` | `/waitlist add` slash command |
| `JOINED_VETS` | `cogs/activity.py` periodic check sees the user joined Returners in-game |
| `BECAME_GUILDLESS` | activity check sees a MEMBER left their guild (not necessarily Returners) |
| `INACTIVE_MEMBER` | activity check sees no in-game login for *X* days (DM warning only, no role change) |
| `INACTIVE_WAITLIST` | activity check sees no Discord activity for *Y* days (drops WAITLISTED) |
| `JOINED_OTHER_GUILD` | activity check sees they joined a guild that isn't Returners |

`TransitionResult.is_error` flags states where the trigger doesn't make sense (e.g. `ADDED_TO_WAITLIST` when already waitlisted, or when currently a `MEMBER`); the cog surfaces these to staff. `is_noop` flags transitions where there's nothing to change.

## Application

Three async helpers actually mutate Discord:

### `apply_transition(member, trigger, *, reason=None)`
The normal path. Calls `state_of`, then `compute_transition`, then performs the role delta. Returns the `TransitionResult` so callers can surface errors.

### `force_to_registered_only(member, *, reason=None)`
Used by `/block` ([`membership_spec.md` §2b](membership_spec.md#2--manual-overrides-commands)). Strips MEMBER/HIATUS/HONOURARY/WAITLISTED, ensures REGISTERED. Bypasses the transition table — this is the "nuclear option" for blocklisted users.

### `ensure_linked_baseline(member, *, in_returners, in_other_guild=False, blocked=False, reason=None)`
The **invariant-enforcing** helper. A linked Discord user must hold a *valid* primary membership state, never none. It is **preservation-based**: it strips only primary roles that are *invalid* for the given situation and grants a baseline only when no valid one remains — a manually-assigned HIATUS/HONOURARY is **kept** when still valid (re-running enforcement must never silently demote a legitimate manual tag). Validity (exactly one of `in_returners`/`in_other_guild` may be True; both False = guildless):

- `blocked` → REGISTERED only (authoritative, [membership_spec §2b](membership_spec.md#2--manual-overrides-commands)).
- `in_returners` → MEMBER only (authoritative — they're in the guild; matches `JOINED_VETS`).
- `in_other_guild` → valid {REGISTERED, HONOURARY}; HIATUS stripped (**in a guild → never Hiatus**), stale MEMBER stripped; HONOURARY preserved (matches `JOINED_OTHER_GUILD`).
- guildless → valid {REGISTERED, HIATUS, HONOURARY}; only a stale MEMBER stripped — a manually-parked HIATUS is preserved.

Baseline (granted only when no valid primary remains) = MEMBER for `in_returners`, else REGISTERED — the additive "Flame" no-state self-heal. Called from three event-driven places **and** re-run periodically by the data-integrity janitor:
- `lib/mc/linking.py:try_consume_code` (via `_enforce_linked_baseline_for`) after a successful link-code consumption
- `cogs/moderation/admin.py:link_set` — the `/link set` staff command, run on both new links *and* the already-linked branch (so it doubles as a role re-sync / repair)
- `cogs/membership/waitlist.py:waitlist_add` — before the `ADDED_TO_WAITLIST` transition, so a user linked via that command's own auto-`/register` path has a baseline state-role to transition from
- `cogs/maintenance/janitor.py` reconciler (A) — every janitor interval, but **only for linked members holding *no* membership state at all** (the true Flame class). It *grants* the baseline (additive); it never strips/demotes a member who already has a valid primary role, so manually-assigned HIATUS/HONOURARY are preserved (genuine multi-primary conflicts are left for manual review). Reconciler (B) applies the same stateless-only rule before any re-enforce. Gated by `JANITOR_REPAIR_ENABLED` but detects+alerts even when repair is off.

`cogs/activity/activity.py` does **not** call this — its loops drive `MEMBER ↔ REGISTERED` through the transition table (`apply_transition`), not through `ensure_linked_baseline`.

Idempotent: a member already holding a valid primary and no invalid ones is a no-op (no API calls). Safe to retry.

`WAITLISTED` is preserved — it's an additive flag managed by the waitlist commands.
`HONOURARY`/`HIATUS` are **preserved when valid** (guildless, or HONOURARY in another guild). They are only cleared when authoritatively superseded: `blocked` → REGISTERED-only; `in_returners` → MEMBER; and HIATUS specifically is cleared when the player is in *any* guild (Returners → MEMBER, other guild → REGISTERED). This intentionally diverges from the old "always clears HIATUS/HONOURARY" behavior, which silently demoted legitimately manual tags on every re-link/janitor pass — and is closer to the [membership_spec §6](membership_spec.md#6--role-automation-behaviour) transition table (JOINED_OTHER_GUILD keeps HONOURARY; HIATUS is reached only via BECAME_GUILDLESS).

If `to_add_ids` is non-empty but the role lookup fails (`guild.get_role(rid)` returns `None`, usually because the role ID in `CurrConfig` is wrong), the function aborts with a loud error log rather than silently leaving the member in a partial state.

## Things that bite

- **Role IDs come from `CurrConfig`, which is mutable.** `/config` can change `ROLE_MEMBER` etc. at runtime. The machine reads them via lambdas (`_STATE_ROLE_MAP`), so a misconfiguration shows up as `ensure_linked_baseline` aborting rather than being silently wrong.
- **`fetch_member` is required for cache-cold lookups.** Member resolution for role/DB reconciliation goes through the shared `lib/role_state.resolve_guild_member(bot, disc_uuid)` (cached `get_member` → REST `guild.fetch_member` fallback), used by the waitlist commands, `join.py:waitlist_cleanup`, and the janitor (`_resolve_member` delegates to it). `linking._enforce_linked_baseline_for` has its own equivalent fallback. Don't drop the REST fallback — it's the difference between a freshly-linked user getting their role and the link silently dissolving.
- **WAITLISTED is DB-authoritative — keep the role and the `Waitlist` table in lock-step.** WAITLISTED is *not* a free-standing manual tag like HIATUS/HONOURARY; it mirrors a `Waitlist` row. Every path that adds/removes a `Waitlist` row must also fire the matching transition (`ADDED_TO_WAITLIST` / `INACTIVE_WAITLIST`): `/waitlist add\|self` add it, `/waitlist remove\|leave` and `join.py:waitlist_cleanup` strip it. A raw `Waitlist.delete()` without the transition was the historical bug that left orphaned WAITLISTED roles (and `/waitlist add` then erroring "already waitlisted"). `/waitlist add` is now idempotent (role already present + DB row ensured = success), and janitor reconciler (D) reconciles any residual divergence both directions.
- **The baseline self-heal loop lives in `cogs/maintenance/janitor.py`, not `join.py`.** If `ensure_linked_baseline` enforcement fails during link-code consumption, the `LinkCode` row is kept; janitor reconciler (B) re-attempts it on each interval (default 6h) and reconciler (A) independently re-checks linked members, so a linked-but-**stateless** user is auto-corrected within one interval **when `JANITOR_REPAIR_ENABLED` is on** (it defaults off — log-only — but still detects + alerts to `JANITOR_ALERT_CHANNEL`). Note: the janitor only *grants* a baseline to members with **no** state at all — it never demotes HIATUS/HONOURARY or any valid primary role (that would require positive info like a guild change, which the activity loop handles). A staff `/link set`, `/janitor run`, or the player re-sending the code still recover it immediately. When triaging "linked but no role": check whether repair is enabled and read the janitor alert channel. (`join.py` itself only has `clear_old_requests` + `waitlist_cleanup`.)
- **Nothing scans a guild that no Returners member is in, so HIATUS goes stale silently.** `cogs/activity/activity.py` walks the Returners roster plus whatever *other* guilds turn up in the weekly stats refresh of current Returners members. A HIATUS user (guildless ex-member) who joins some third guild appears in neither, so `JOINED_OTHER_GUILD` never fires and `MinecraftAccount.guild` stays `None` forever — they keep the HIATUS role and the "in a guild → never Hiatus" invariant above is violated with no path back. Any consumer that gates on "is this person still guildless" must therefore re-read the live value with `lib/mc/resolve.refresh_mc_guild` rather than trusting the column. Measured 2026-07-27 by replaying wynnpool's `POST /guild/event` join/leave log against `hiatus_spotted_alerts`: 16 of 93 hiatus alerts (17%) fired while the player was in a guild — 10 in another guild, and **6 while in Returners**, i.e. the pre-existing `guild == "Returners"` guard failing on its own terms because the column was stale. The live re-read, not the widened comparison, is what actually closes this. Janitor reconciler **(H)** sweeps the same invariant on the 6h interval so a stale HIATUS is corrected without waiting for the player to log in — note it is the one exception to "the janitor never demotes a valid primary role", justified because a live guild reading *is* the positive information that rule asks for. `lib/mc/hiatus_alerts.maybe_alert_hiatus` does exactly that (stored-guild fast path first, so the API call only happens for accounts we *believe* to be guildless, and only after the 24h cooldown has passed) and then self-heals the role via the trigger the player is actually owed.
- **Firing a trigger from an MC uuid goes through `fire_trigger_for_mc_uuids(bot, uuids, trigger, *, reason)`.** The automation observes guild membership per *Minecraft* account but acts on *Discord* roles, so every caller needs the same uuid → link (primary **and** `MinecraftAlt`) → member → `apply_transition` walk. It lives in `lib/role_state.py`; `activity.py:_fire_role_transitions_for_uuids` is a thin string-keyed wrapper over it. Missing members are skipped and per-member Discord errors are logged without aborting the batch; `compute_transition` no-ops on inapplicable states, so an over-broad uuid set is safe.
- **Activity-loop triggers vs. `ensure_linked_baseline` overlap.** Both can fire `MEMBER ↔ REGISTERED` transitions. The activity loop uses the transition table (which generates `BECAME_GUILDLESS` → HIATUS); `ensure_linked_baseline` skips the table and goes directly to MEMBER or REGISTERED. They're consistent because `ensure_linked_baseline` only runs when triggered by a *linking* event, not by a guild-state change.

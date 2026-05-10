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

### `ensure_linked_baseline(member, *, in_returners, blocked, reason=None)`
The **invariant-enforcing** helper. Any linked Discord user must hold either MEMBER or REGISTERED — never both, never neither, never HIATUS or HONOURARY as their primary state. Called from:
- `lib/linking.py:try_consume_code` after a successful link
- `cogs/join.py` periodic janitor (catches stale states from prior failures)
- `cogs/activity.py` whenever guild membership changes

Idempotent: if the member already has the correct primary role and no conflicting ones, it's a no-op (no API calls). Safe to retry.

`WAITLISTED` is preserved — it's an additive flag managed by the waitlist commands.
`HONOURARY` is *intentionally cleared* — it's mutually exclusive with the linked-account baseline; staff can re-grant via `/honour`.

If `to_add_ids` is non-empty but the role lookup fails (`guild.get_role(rid)` returns `None`, usually because the role ID in `CurrConfig` is wrong), the function aborts with a loud error log rather than silently leaving the member in a partial state.

## Things that bite

- **Role IDs come from `CurrConfig`, which is mutable.** `/config` can change `ROLE_MEMBER` etc. at runtime. The machine reads them via lambdas (`_STATE_ROLE_MAP`), so a misconfiguration shows up as `ensure_linked_baseline` aborting rather than being silently wrong.
- **`fetch_member` is required for cache-cold lookups.** `cogs/join.py`'s periodic janitor and `linking._enforce_linked_baseline_for` both fall back to a REST `fetch_member` after a `get_member` cache miss. Don't drop that fallback — it's the difference between a user getting their role and the link silently dissolving.
- **Activity-loop triggers vs. `ensure_linked_baseline` overlap.** Both can fire `MEMBER ↔ REGISTERED` transitions. The activity loop uses the transition table (which generates `BECAME_GUILDLESS` → HIATUS); `ensure_linked_baseline` skips the table and goes directly to MEMBER or REGISTERED. They're consistent because `ensure_linked_baseline` only runs when triggered by a *linking* event, not by a guild-state change.

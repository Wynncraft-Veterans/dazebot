# VerifyKey: bearer-key auth for vetsmod

Reference for [lib/verify_keys.py](../lib/verify_keys.py) and the `VerifyKey` ORM table — the bearer-token auth flow that vetsmod uses to authenticate WS connections to `temporary-server`.

For the high-level cross-repo wiring, see [`../CLAUDE.md`](CLAUDE.md) §"Two related auth flows on the same FastAPI app". This doc is the dazebot-internal reference.

## Token shape

`secrets.token_urlsafe(32)` → 43 ASCII chars from `[A-Za-z0-9_-]`. 32 bytes (256 bits) of entropy. Fits inside Minecraft's 256-char chat-line limit even after `/unlock ` (8 chars) is prepended.

The token is the *only* secret on the row. `mc_uuid`, `mc_username`, and `tier` are snapshot values, used as a fallback if introspection can't reach Discord; they don't gate access on their own.

## Lifecycle

```
issued ──► used (last_used_at bumped on every introspection) ──┬─► rotated (key replaced)
                                                                ├─► revoked (revoked_at set; row preserved)
                                                                └─► re-issued (revoked row reused, revoked_at cleared)
```

### `get_or_issue_key(member)` — the `/vetsmod` slash handler
- Re-running `/vetsmod` returns the same key (analogous to `LinkCode` reuse).
- Tier is **always re-resolved** on call. So a user whose role changed between issuance and re-run sees the updated tier in the DM, even if the key itself didn't change.
- A revoked row is treated as absent — the row is rotated in place (key replaced, `revoked_at` cleared) so we don't collide on `disc_uuid`'s unique constraint.

### `rotate_key(member)` — the `/change rotate key <target>` staff handler
- Forces a new key. Old vetsmod installs holding the previous key fail introspection on next reconnect (worst case 60s — temporary-server's LRU cache TTL).
- If the user has no row yet, falls through to `get_or_issue_key`.
- Self-rotation by users was removed when the staff `/change` group was introduced — users now ask a moderator if they suspect their key has leaked. The cog refuses to rotate for a user who has left every shared guild (no live `Member` to refresh tier off, and they can't `/vetsmod` to pick up the new key anyway — staff should `/change remove key` instead).

### `revoke_key(disc_uuid, *, reason=None)` — the `/change remove key <target>` staff handler
- Sets `revoked_at = now()`. Row is **kept**, not deleted, so a future `/vetsmod` shows "your key was revoked on …" instead of silently re-issuing.
- Returns `True` only if a row was found and was previously un-revoked. Re-revoking returns `False`.
- The cog also DMs the affected user when staff invoke this (with the optional `reason` argument), so accidental revocations don't leave users wondering why their mod stopped working.

## Tier resolution (`resolve_tier(member)`)

Order of precedence — first match wins:

1. Linked MC account is in the `Blocklist` → `other` (with `blocked=True`)
2. `HONOURARY` Discord role → `honourary`
3. `MEMBER` Discord role (in-game Returners member) → `member`
4. `WAITLISTED` Discord role → `waitlist`
5. `Waitlist` row exists for the linked MC account → `waitlist`
6. None of the above → `other`

Returns a `ResolvedTier(tier, mc_uuid, mc_username, blocked)`.

`tier_to_ws_protocol(tier)` maps to the existing v1 WS register-frame `tier`/`ws_tier` value:
- `member` → `guild`
- `waitlist` → `waitlist`
- `honourary` → `honourary`
- `other` → `None` (no chat-channel access)

The protocol predates this auth flow and only knows the three string values; `other` deliberately has no WS tier.

## Introspection (`introspect(bot, key)`) — the contract with temporary-server

Endpoint: `POST /api/auth/introspect` (defined in [api/main.py](../api/main.py)). Header: `X-Introspect-Secret: $DAZEBOT_INTROSPECT_SECRET`. Body: `{"key": "<43 chars>"}`.

Returns an `IntrospectionResult`:

```json
{
  "valid": true,
  "disc_uuid": "...",
  "mc_uuid": "...",
  "mc_username": "...",
  "tier": "member|waitlist|honourary|other",
  "ws_tier": "guild|waitlist|honourary|null"
}
```

Or `{"valid": false, "reason": "..."}` for unknown / revoked / empty keys.

Behaviour worth knowing:

- **Tier is re-resolved on every call** against the live Discord member. Promote a user to MEMBER and they pick it up on the next WS reconnect (or whenever temporary-server's 60s LRU cache expires) — no `/change rotate key` needed.
- **`last_used_at` is bumped on every successful introspection.** Useful for "delete keys not used in N months" cleanup.
- **The row's snapshot (`tier`, `mc_uuid`, `mc_username`) is updated** when the live re-resolution disagrees with what's stored. This keeps the snapshot fresh as a fallback.
- **The function never raises.** Internal errors (member not in cache, REST flake) fall back to the snapshot stored on the row. Exceptions are logged but turned into a successful (snapshot-based) response, so a transient Discord outage doesn't lock everyone out of vetsmod chat.
- **`_find_member` walks every guild the bot is in** and returns the first match. Single-guild deployments pay no cost; multi-guild ones get correct results regardless of which guild the role lives in.

## Failure modes seen in production

| Symptom | Root cause | Fix |
|---|---|---|
| `{"valid": false, "reason": "unknown key"}` for a key the user *just* got DM'd | DB rolled back / sqlite checkpoint never happened | re-run `/vetsmod`; check `dazebot.db-wal` is being checkpointed (see `runtime_config._checkpoint_wal`) |
| Tier shown in `/vetsmod` DM doesn't match what introspection returns | Cog read tier from the *cached snapshot*; introspection re-resolves live | by design; the live tier is the source of truth, the DM is a hint |
| 401 on every introspection | `DAZEBOT_INTROSPECT_SECRET` mismatch between dazebot's `.env` and temporary-server's `.env` | regenerate (`openssl rand -hex 32`) and paste into both, restart dazebot first |
| 503 on every introspection | `DAZEBOT_INTROSPECT_SECRET` env var missing entirely | set it; the endpoint is fail-closed |
| `valid: true` but `ws_tier: null` | user's tier is `other` (no role match, unlinked, or blocklisted) | expected behaviour — they can't send chat, see [`membership_spec.md` §6](membership_spec.md#6--role-automation-behaviour) |

## Don't

- **Don't return a key body to the WS in the introspection response.** It's not part of the contract and would let a compromised temporary-server log keys. The current response only echoes the key's *identity* (disc_uuid + mc_*).
- **Don't soft-delete revoked rows by setting `key = ""`.** The revocation marker is `revoked_at IS NOT NULL`. Empty-string keys would still match in introspection and pass the `if not key` guard depending on how the caller invokes it.
- **Don't add a `created_by` column expecting it to mean "the user who issued the key".** A staff `/change remove key <target>` revokes someone else's key — the row is always owned by the target's `disc_uuid`.

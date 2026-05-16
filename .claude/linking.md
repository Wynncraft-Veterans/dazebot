# Account linking (Discord ↔ Minecraft)

Reference for [lib/mc/linking.py](../lib/mc/linking.py) and the `LinkCode` ORM table — the Discord-↔-Minecraft account-linking flow that runs over the auth-stack PicoLimbo chat-line forwarding.

This is the **link-code** flow, distinct from the **vetsmod /unlock key** flow ([verify_keys.md](verify_keys.md)). Both reach `dazebot/api/main.py` under `/api/auth/...`, but they have nothing else in common.

## The flow

1. Discord user clicks the "Link my Minecraft account" button in `#welcome` (or runs `/link_code <mc_username>`). This is the [`first_install_view.py`](../lib/discord_utils/first_install_view.py) entry point.
2. `get_or_issue_code(disc_uuid, mc_username)` persists or reuses a `LinkCode` row keyed on `lower(mc_username)`.
3. Bot DMs the user the 6-char code + `verify.wynnvets.org` IP. (Or, if DMs are closed, posts to `LINK_FALLBACK_CHANNEL` with a "show me the code" button.)
4. User joins `verify.wynnvets.org:25565` (auth-stack PicoLimbo) and types the code in chat.
5. PicoLimbo `GET`s `/api/auth/{uuid}/{msg}` for **every** chat line. The handler in [api/main.py](../api/main.py) calls `try_consume_code(bot, mc_uuid, mc_username, msg)` for each.
6. On match: link the accounts, enforce the role baseline, delete the `LinkCode` row.

The `LinkCode` row never times out — the user can take a week to get back in-game and it still works. A different Discord user requesting the same `mc_username` overwrites the row (rotates the code), implicitly invalidating the prior one.

## Code shape

```python
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # excludes 0/O, 1/I/L
_CODE_LENGTH = 6
```

Visually-confusable characters are excluded so users typing the code in Minecraft don't trip on `0`/`O` or `1`/`I`. 30^6 ≈ 7.3 × 10^8 — plenty of entropy for a 6-char shared secret with a username scope.

## `try_consume_code` — the matching logic

Two paths, in order:

### Primary path: row matches by username AND message contains the code
The expected case. The user typed the code in their own message, while in-game as the username they registered for.

### Fallback path: code-uniqueness match
If the username doesn't match a pending row but the message contains a shaped 6-char token (`(?<![A-Z0-9])[CODE_ALPHABET]{6}(?![A-Z0-9])`), search all `LinkCode` rows for a code that exactly equals it.

- **Zero matches:** silently ignore (just normal chat).
- **Exactly one match:** accept, log a `WARN` so the username discrepancy shows up in logs. Common reasons:
  - Account got renamed since they typed the modal.
  - User typed a typo / wrong capitalisation in the modal.
  - User has an alt and joined PicoLimbo as the alt.
- **Multiple matches:** refuse — log `WARN` listing all candidates. The 6-char alphabet has plenty of collision room and we don't want to guess.

The code itself is the shared secret; `mc_username` is purely a scope hint that lets the primary path skip the regex search.

## After a successful match

1. **Refuse if MC already linked elsewhere.** If the `MinecraftAccount` is already linked to a different `DiscordAccount`, delete the `LinkCode` row and return failure ("ask them to /unlink first"). The DM goes back to whoever the row belonged to.
2. **Refuse if Discord already has a primary link.** Same idea, reverse direction.
3. **Create the `MinecraftAccount` if missing.** Pulls full stats from Wynncraft API (`get_player_full_stats`) so we have `wynn_username`, `guild`, `last_online`, `first_join` populated.
4. **Refresh `mc.guild` from the live API.** Best-effort. We do this *before* `ensure_linked_baseline` so a user who joined Returners between the activity-loop tick and this link gets MEMBER instead of REGISTERED.
5. **Call `ensure_linked_baseline(member, in_returners=…, blocked=…)`.** See [role_state.md](role_state.md). This grants MEMBER or REGISTERED and clears HIATUS/HONOURARY.
6. **Only then delete the `LinkCode` row.** Critical for retry safety.

### The "keep the row on enforcement failure" rule

`ensure_linked_baseline` can fail if:
- The Discord member isn't in any of the bot's guilds (e.g. they left between clicking the button and typing the code).
- The Discord cache is cold AND the REST `fetch_member` falls back fail (rate limit, transient HTTPException).

If `_enforce_linked_baseline_for` returns `False`, `try_consume_code` **keeps the `LinkCode` row** and logs an `ERROR`. The data-integrity janitor (`cogs/maintenance/janitor.py` reconciler B) now periodically re-attempts `_enforce_linked_baseline_for` for kept rows and applies this *same* delete/keep contract: delete when enforcement confirms success, delete when unrecoverable (the MC is linked to a different `DiscordAccount`, or the Discord user is in no guild at all), keep otherwise. Re-attempt cadence = the janitor interval (default 6h) and only mutates when `JANITOR_REPAIR_ENABLED` is on (log-only otherwise, but still alerts). A player code re-send (re-invokes `try_consume_code`) or a staff `/link set` / `/janitor run` still recover it immediately. So keeping the row prevents *premature* `LinkCode` deletion **and** is now eventually reconciled rather than relying solely on manual re-trigger.

Without this rule, a user could end up with the `DiscordAccount ↔ MinecraftAccount` link in the DB but **neither** the MEMBER nor REGISTERED role — the exact invariant the role-state machine exists to prevent.

## Helpers in this module

- **`get_or_issue_code(disc_uuid, mc_username)`** — `(LinkCode, is_new)`. Reuse if same disc+mc; rotate if different disc, same mc; issue if neither.
- **`dm_or_log(user, content, *, fallback_logger)`** — best-effort DM. Returns `True`/`False` so the caller can fall back to a public ping. Used by `first_install_view` and the `/vetsmod` cog.

## Don't

- **Don't shorten the code without expanding the alphabet.** The fallback "code uniqueness" path relies on collisions being rare enough that exactly-one matches are common.
- **Don't trust `mc_username` from the in-game side as the identity.** The Wynncraft username field of the chat is set by the player; the `mc_uuid` (URL parameter from PicoLimbo) is the actual identity. We use the username only for matching the original `LinkCode` row.
- **Don't add an "expiry" column to LinkCode.** The current behaviour — never expires, overwritten on re-issue — is intentional. It accommodates users who get their code on Monday and don't log into Minecraft until the weekend.
- **Don't combine this flow with the VerifyKey flow.** They share an HTTP prefix and nothing else. See `verify_keys.md`.

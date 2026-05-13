# Auth flows

Auth is *one* of dazebot's responsibilities — alongside membership/role state, the waitlist, vanity roles, supporter detection, weekly events, etc. This doc consolidates the parts of the codebase that exist to serve auth so the rest of the docs aren't dominated by it.

For the surrounding context see [CLAUDE.md](CLAUDE.md). For the in-detail mechanics see [verify_keys.md](verify_keys.md) and [linking.md](linking.md).

## Two flows on the same FastAPI app

Both flows hit dazebot's HTTP sidecar under `/api/auth/...`. They share that prefix and **nothing else** — different endpoints, different callers, different purposes.

### A. PicoLimbo link code (account *linking*)
- **Endpoint:** `GET /api/auth/{uuid}/{msg}` (defined in [api/main.py](../api/main.py))
- **Caller:** PicoLimbo (auth-stack), forwards every in-game chat line on `verify.wynnvets.org`
- **Purpose:** Look for a 6-char `LinkCode` in the message; if found, couple the MC UUID to the Discord user. See [linking.md](linking.md).
- **Issued via:** the `/first_install` button ([lib/discord_utils/first_install_view.py](../lib/discord_utils/first_install_view.py)) — DMs the user a code.
- **Auth on the endpoint:** none. It's only reachable on the `verify` docker network and on `127.0.0.1:${DAZEBOT_PORT}` — *don't re-expose publicly*.

### B. Vetsmod `/unlock` key introspection (chat *authentication*)
- **Endpoint:** `POST /api/auth/introspect`
- **Caller:** temporary-server, on every WS `auth` frame (60s LRU cache, serve-stale-on-error).
- **Purpose:** Validate a 43-char bearer key; return `{valid, disc_uuid, mc_uuid, mc_username, tier, ws_tier, reason}`. See [verify_keys.md](verify_keys.md).
- **Issued via:** the `/vetsmod` slash command ([cogs/integrations/vetsmod.py](../cogs/integrations/vetsmod.py)) — DMs the user the modrinth link + `/unlock <key>` body, with a `LINK_FALLBACK_CHANNEL` ping fallback for closed DMs.
- **Auth on the endpoint:** `X-Introspect-Secret` header must match `$DAZEBOT_INTROSPECT_SECRET`. Missing env → 503 (fail-closed); mismatch → 401.

## Tier resolution

`lib/staff/verify_keys.py:resolve_tier(member)` is the single source of truth for mapping Discord roles + DB state to a tier. Called both at `/vetsmod` issuance and on every introspection, so tier changes propagate without re-issuing keys (worst case 60s — temporary-server's cache TTL).

| Tier | Granted by | `ws_tier` (wire protocol) |
|---|---|---|
| `member` | MEMBER Discord role + linked MC account in Returners | `guild` |
| `waitlist` | WAITLISTED role or row in `Waitlist` table | `waitlist` |
| `honourary` | HONOURARY role | `honourary` |
| `other` | linked but no role match, OR blocklisted | `null` (no chat-channel access) |

The wire protocol predates this auth flow and only knows the three string tier values; `other` deliberately maps to `null` rather than a fourth tier.

See [verify_keys.md §"Tier resolution"](verify_keys.md) for the precedence order.

## Auth-related env vars

| Var | Purpose |
|---|---|
| `DAZEBOT_INTROSPECT_SECRET` | Shared secret for `POST /api/auth/introspect`. Must match temporary-server's `.env` of the same name. Generate once with `openssl rand -hex 32`. Missing → fail-closed 503 on every introspection. |
| `MC_PUBLIC_HOST` | Defaults to `verify.wynnvets.org`. Used in the `/first_install` DM that tells users where to connect for the link-code flow. |

The other env vars (`TOKEN`, `WAPI_TOKENS`, `DAZEBOT_PORT`, `PREFIX`) belong to the bot core, not to auth specifically — see [CLAUDE.md](CLAUDE.md).

## Things to know (auth-side)

- **Blind trust to picolimbo.** `/api/auth/{uuid}/{msg}` has no auth; it's gated by docker-network isolation. The chat line itself is the authentication — only someone who can send chat to PicoLimbo as a given UUID can submit codes for that UUID.
- **Introspection is fail-closed.** Missing `DAZEBOT_INTROSPECT_SECRET` → every request returns 503. Intentional — better to lock everyone out than serve auth with a missing secret.
- **Tortoise SQLite quirks.** Concurrent writes from a sibling container are not safe — that's why temporary-server validates via HTTP introspection rather than sharing the SQLite file.
- **Two flows, one prefix.** Don't add a third endpoint under `/api/auth/` without a clear name; the prefix is *historical*, not a category. New endpoints belong wherever makes semantic sense.

## Production runbook

For "auth flow broken in prod, what now" triage steps, see [`../../vets-deploy/.claude/runbook.md`](../../vets-deploy/.claude/runbook.md). The dazebot-internal failure modes are catalogued in [verify_keys.md §"Failure modes"](verify_keys.md).

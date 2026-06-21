# Chore-Torn Palace (CTP)

The CTP cog ([cogs/rewards/ctp/ctp.py](../cogs/rewards/ctp/ctp.py)) is the points-and-prizes system surfaced via the `~ctp` command group. It tracks per-user point balances, an editable prize catalog, time-bound redemptions, and a cumulative glint-investment leaderboard.

## Where to look

| Concern | File |
|---|---|
| Command surface (all ~22 subcommands) | [cogs/rewards/ctp/ctp.py](../cogs/rewards/ctp/ctp.py) |
| Append-only ledger + balance math | [cogs/rewards/ctp/lib/balance.py](../cogs/rewards/ctp/lib/balance.py) |
| Board CRUD + tasks.wynnvets.org URL formatting | [cogs/rewards/ctp/lib/boards.py](../cogs/rewards/ctp/lib/boards.py) |
| Prize catalog + redemption logic | [cogs/rewards/ctp/lib/prizes.py](../cogs/rewards/ctp/lib/prizes.py) |
| Glint invest / leaderboard / rank lookup | [cogs/rewards/ctp/lib/glints.py](../cogs/rewards/ctp/lib/glints.py) |
| Link-bonus eligibility + idempotent grant | [cogs/rewards/ctp/lib/link_bonus.py](../cogs/rewards/ctp/lib/link_bonus.py) |
| Link-bonus retroactive backfill + periodic reconciler | [cogs/rewards/ctp/link_bonus_reconciler.py](../cogs/rewards/ctp/link_bonus_reconciler.py) |
| vets-anni internal client (fishbot role-capability check) | [lib/integrations/anni_internal.py](../lib/integrations/anni_internal.py) |
| Yes/No confirmation view (cross-subsystem; reused by `~donations remove`) | [cogs/rewards/lib/confirm_view.py](../cogs/rewards/lib/confirm_view.py) |
| Embed builders and history-line formatters | [cogs/rewards/ctp/lib/formatting.py](../cogs/rewards/ctp/lib/formatting.py) |
| ORM tables | [orm.py](../orm.py) — `CTPBoard`, `CTPBoardMembership`, `CTPPrize`, `CTPLedger`, `CTPGlintInvestment` |
| Initial prize-catalog seed | [vets-deploy/scripts/one-off/seed-dazebot-ctp-catalog.sh](../../vets-deploy/scripts/one-off/seed-dazebot-ctp-catalog.sh) |

## Schema invariants

1. **The ledger is append-only.** Every point movement (reward, redeem, gift, glint invest, admin set) writes a new `CTPLedger` row. Balance is `SUM(amount_delta) WHERE discord_account = X`, never read from a stored column. Mirrors the `StaffActionEntry` pattern in [orm.py](../orm.py).

2. **Prize snapshots survive prize deletion.** `CTPLedger.prize_display_at_time` / `prize_category_at_time` / `expires_at` are captured at redemption time. Editing a prize's duration after the fact does not move existing redemption windows; deleting a prize via `~ctp prize remove` nulls the FK but leaves the snapshot intact, so `~ctp history` still renders the old row legibly.

3. **Glint investments are cumulative-only.** `CTPGlintInvestment.total_invested` can only ever increase (spec line 92). A bid writes BOTH a negative ledger row (`source='glint_invest'`, which debits the balance) AND an increment on the glint row. There is no un-invest command.

4. **Gifts are two rows.** `~ctp gift A → B` writes `(A, -n, source='gift_sent', counterparty=B)` and `(B, +n, source='gift_received', counterparty=A)`. Both rows surface in their respective owners' `~ctp history`; the receiver's row isn't derived from the sender's at render time.

5. **`(category, enum_name)` is the prize key.** Uppercased on insert. `~ctp redeem @user access chief` does a loose substring match within the category (so `chief` → `CHIEFS_CORNER`). `~ctp prize edit GIFT.NAZ ...` uses the exact dotted form.

## Permission tiers

Reused from [lib/auth.py](../lib/auth.py) verbatim — no new decorators.

| Tier in spec | Decorator | Subcommands |
|---|---|---|
| Registered | `@is_registered()` | `status`, `history`, `gift`, `prize info` |
| Guild | `@is_guild()` | `glints bids`, `glints bid` |
| Staff | `@is_staff()` | `balance`, `info`, `reward`, `redeem`, `access`, `assign`, `revoke` |
| Admin | `@is_admin()` | `board`, `prize add/edit/disable/enable/remove/disclaim`, `set` |

## Glints leaderboard rules

`~ctp glints bids` and the "are/are_not glinted" line in `~ctp status` consult **only** users currently holding MEMBER, WAITLISTED, or HONOURARY. HIATUS, REGISTERED, no-state, and members the bot can't see are silently dropped from both. The investment row stays — they pop back on when they re-enter an eligible state.

Visible cutoff: `glints.GLINT_VISIBLE_CUTOFF = 8`. Ranks 1–8 render under **Currently Glinted**, ranks 9+ under **Standby**. `is_glinted` (used by `~ctp status`) is true iff `rank <= 8 AND rank is not None`.

Note: HIATUS users are NOT blocked from running `~ctp glints bid` — they keep accruing investment so re-joining the guild puts them back in the right position. Only the visible leaderboard and the `is_glinted` flag filter.

### Tied bidders

When two or more eligible bidders share the same `total_invested`, the cutoff slot rotates among them on a deterministic wall-clock cadence (`glints.TIE_ROTATION_PERIOD_SECONDS = 3 * 3600`, boundaries at 00:00 / 03:00 / 06:00 ... UTC). The rotation index is `epoch_seconds // PERIOD`; each tied group is sorted by `member.id` and rotated by `index mod group_size`. This applies before the cutoff slice in `_ranked_eligible`, so `~ctp glints bids`, `~ctp status` `is_glinted`, and `/api/internal/glinted` all agree on who is currently glinted within a 5-min poll window of each boundary (vetsmod's `SupportersPoller` cadence).

The rotation only changes who is glinted when a tied group **straddles** the cutoff (e.g. 7 bidders tied at rank 1 with `GLINT_VISIBLE_CUTOFF = 8` — all 7 fit; but with the API cutoff at 6, one of the 7 rotates out each bucket). Tied groups entirely above the cutoff (everyone glinted) or entirely below (nobody glinted) rotate internally but the membership of the cutoff is unchanged.

`~ctp glints bids` appends a footer "*N bidders tied at rank K; the cutoff slot rotates among them every 3 h.*" when a tie straddles the display cutoff. `~ctp status` for a user inside a cutoff-straddling tie appends "(tied with M others; rotates every 3 h)" so the rotation doesn't read as a bug.

## Board membership (manual vs role-derived)

A user "is on" a board if **either** of:

1. They hold the board's `role_id` in Discord (the original mechanism). Boards with no `role_id` set are skipped on this side — we can't claim membership we can't verify.
2. A staff member has written a `CTPBoardMembership(disc, board)` row via `~ctp assign`.

`_board_memberships` in [cogs/rewards/ctp/ctp.py](../cogs/rewards/ctp/ctp.py) unions the two and dedupes. The list surfaces in `~ctp status` and `~ctp info <user>`; nothing else in the codebase consumes board membership today.

`~ctp assign <user> <ENUM>` and `~ctp revoke <user> <ENUM>` are staff-tier. They write/delete the DB row **and** grant/strip the board's `role_id` from the target as a one-shot side effect at command time. There is no drift sync — if the role is later removed by hand in Discord, the DB row stays, and vice versa. This is intentional: the DB row is the staff-driven "manual" assignment record; the Discord role is whatever role-management is happening in the server, which membership reads opportunistically. The one-shot grant just exists so staff doesn't have to do two clicks.

Failures of the role half (missing permissions, board has no `role_id`, role deleted from the guild) are reported back to the staff member in the reply but do NOT roll back the DB write — the assignment is recorded regardless. `actor_disc_uuid` on the row captures who did the assign for forensics; no UI surfaces it today.

## Active redemptions

`~ctp access` lists ledger rows where `source='redeem' AND expires_at > now AND expires_at IS NOT NULL`. One-time prizes (no `expires_at`) never appear. The bot **does not** grant or strip Discord roles / channel permissions on redeem or expiry — staff handles all access changes manually (in-game rooms or the boardroom channel / pink role). The list is a reminder of "who is currently inside their window."

## `~ctp board` syntax (overloaded)

Dispatched by [`boards.apply_board_command`](../cogs/rewards/ctp/lib/boards.py). Parsing rules:

| Form | Effect | Required state |
|---|---|---|
| `~ctp board ART 2 <snowflake>` | Create or replace ART with `board_number=2` and the given role. | — |
| `~ctp board ART 6` | Move ART to `board_number=6`. | ART must already exist. |
| `~ctp board ART <snowflake>` | Change ART's role only. | ART must already exist. |
| `~ctp board ART 0` | Delete ART. | ART must already exist. |

"Snowflake" = numeric string of length ≥ 17. Short numbers are board IDs.

## Prize-info image

`~ctp prize info` renders one embed per category. When `CurrConfig.CTP_PRIZE_ARTWORK_URL` is set, the **first** embed only gets `set_image(url=...)` so paginating to a later page doesn't re-show the artwork. Set / clear via the `/config` admin command (persisted in `BotConfigOverride`).

## Admin-only surfaces folded into existing commands

- **`~ctp prize info` is tier-aware.** Admins additionally see disabled rows (marked `(disabled)` with strikethrough on the cost/duration line); non-admins see the same filtered view as before. There is no separate `~ctp prize list` — the same command renders the admin view via `prizes_svc.all_visible(include_disabled=True)`.
- **`~ctp board list`** (admin) enumerates every `CTPBoard` row. The parent `~ctp board ENUM <args>` overload still routes to `boards.apply_board_command` via the group's `invoke_without_command=True`. Both the bare body and the `list` subcommand carry their own `@is_admin()` — with `invoke_without_command=True`, the parent's check isn't run on subcommand dispatch.

## Admin-defined categories

Prize categories are entirely admin-defined — there is no system-side allowlist. `~ctp prize add <Category> <ENUM> <cost> <duration> <display>` titlecases the category arg via `prizes.normalize_category` and stores the row verbatim; subsequent commands resolve the category case-insensitively. `~ctp prize info` pages render alphabetically by category, with the always-on "Glints" page appended at the end (the only system-side rendering hook). The seed script just (re)creates a specific set of admin-defined categories; promoting a category to "always present after reseat" means adding rows under it to the seed `ROWS` list.

## Bulk `<Category>.*` form

`~ctp prize disable`, `enable`, `remove`, and `disclaim` accept either the original `<Category>.<ENUM>` arg or the wildcard `<Category>.*`, which targets every prize in that (canonicalised) category — including disabled ones for `enable`/`remove`/`disclaim`. Resolution lives in `prizes_svc.resolve_targets`; the cog handlers share `_summarise_bulk` for the reply (single-target keeps the pre-wildcard one-line format; multi-target names the count and lists the enums). `prize edit` is intentionally NOT wildcard-capable — its `<field> <value>` syntax doesn't make sense applied to a heterogeneous set.

## Seeding

Boards are populated in-place by admins via `~ctp board <ENUM> <num> <role_id>` after deploy. `~ctp board list` (admin) enumerates them. The prize catalog has a reseat-from-source script: [seed-dazebot-ctp-catalog.sh](../../vets-deploy/scripts/one-off/seed-dazebot-ctp-catalog.sh). The script is **destructive** — it backs the full DB up to `/opt/docker/backups/db-dumps/dazebot/` (online-backup API, same shape as `manage update`'s pre-flight dump), then `DELETE`s `ctp_prizes` and reinserts the seed list verbatim. Use it to recover from drift; for incremental tweaks use `~ctp prize add` / `edit` instead. Set `NONINTERACTIVE=1` to skip the confirm prompt.

## Top-glinted export — `GET /api/internal/glinted`

Lives in [api/main.py](../api/main.py), gated by `DAZEBOT_INTROSPECT_SECRET` like the other `/api/internal/*` siblings. Returns the 8-slot list that drives temporary-server's `/v1/outbound/supporters` endpoint (which vetsmod's `SupportersPoller` consumes for the in-chat glint shimmer).

Shape:

```
{"slots": [{"mc_uuid": str, "mc_username": str} | null, x8]}
```

- Slots 1–5: top 5 entries of `glints.leaderboard(bot)` — already filtered to MEMBER / WAITLISTED / HONOURARY by `is_eligible_member`.
- Slot 6: top cumulative donation recipient among Returners observed online in the last `ONLINE_RECENCY_SECONDS` (180s; see [cogs/activity/activity.py](../cogs/activity/activity.py)), deduped against slots 1–5 and 7–8. Computed via `donations_svc.leaderboard_totals_for_mc_ids(currently_online_returners_mc_ids())`. **Unique fallthrough behaviour**: on dedup conflict or ineligibility, the loop walks down the online-donor ranking rather than yielding `null` — the intent is "recognise the top historical donor who's around right now", so a conflict should keep searching, not waste the slot. `null` only when no eligible online donor exists at all.
- Slots 7–8: two most recent distinct recipients with a qualifying donation milestone (≥5% of their cumulative-received total). Computed live via `donations_svc.donation_milestone_recipients(limit=2)` — see [donations.md](donations.md). Subject to the same MEMBER / WAITLISTED / HONOURARY eligibility filter as slots 1–5.
- Positions are **positional**. A user whose slot can't be filled (no linked `MinecraftAccount`, ineligible role, fewer than 5 CTP glinters, fewer than 2 milestone donors, or — for slot 6 only — no eligible online donor) produces a `null` at their slot. Lower-ranked candidates are NOT promoted into the gap. Slot 6 is the sole exception: it walks the online-donor ranking until it finds an eligible non-duplicate, since "next-best online donor" is more useful than "empty slot".

Don't add a Discord "Glinted" role — the rank is computed live from `CTPGlintInvestment + state_of(member)` and recomputing on every guild state change would be its own can of worms. The poller on the temporary-server side picks up changes on its 5-min cadence; if you need faster propagation, lower the cadence there rather than building a push channel.

## Link-bonus (1 point × 3 milestones, retroactive + ongoing)

Three identity milestones each award **1 CTP point** exactly once per user:

| Kind | Eligible iff | Source of truth |
|---|---|---|
| `mc_link` | `DiscordAccount.minecraft_account_id` is set | dazebot DB |
| `vetsmod` | non-revoked `VerifyKey` row exists | dazebot DB |
| `fishbot_role` | linked `mc_uuid` is in vets-anni's `RoleCapability` set | vets-anni `GET /api/internal/role-capability-uuids` |

Awards are written as plain `CTPLedger` rows with `source='link_bonus'` and the kind token in `comment` (`mc_link` / `vetsmod` / `fishbot_role`). The `(source, comment)` pair *is* the idempotency key — there is no separate flag column. A user can therefore never receive the same bonus twice, even across reconciler ticks.

[`cogs/rewards/ctp/link_bonus_reconciler.py`](../cogs/rewards/ctp/link_bonus_reconciler.py) runs once on `on_ready` (the retroactive backfill) and then on `LINK_BONUS_RECONCILER_HOURS` (default 6h). Newly-linked users pick up their bonuses on the next tick — no event hook in the linking path, so a missed event (restart, edge case) still self-heals.

vets-anni's [`/api/internal/role-capability-uuids`](../../vets-anni/app/web/routers/internal.py) is gated by `X-Introspect-Secret == DAZEBOT_INTROSPECT_SECRET` (the same shared secret already used in the dazebot→vets-anni direction). An unreachable vets-anni silently skips the `fishbot_role` kind for the tick; the other two kinds still run.

## Don't

- Don't store a mutable `balance` column on any table. Every "set" goes through a corrective ledger row.
- Don't auto-grant or auto-strip Discord access from redemptions. Staff drives this manually; the bot only tracks the timer.
- Don't add `~ctp uninvest` or a glint-refund path. The spec is explicit that investments are cumulative-only.
- Don't filter HIATUS users out of `~ctp glints bid` — they should keep accruing investment for when they rejoin.
- Don't bypass `boards.apply_board_command` for `~ctp board`. The overloaded parsing lives in one place by design.
- Don't add an event hook to the linking / `/vetsmod` / role-capability paths to "award immediately". The reconciler is intentionally the only writer — having one path keeps idempotency in one place and means a missed event still self-heals on the next tick.

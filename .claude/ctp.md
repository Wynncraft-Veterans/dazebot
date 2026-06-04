# Chore-Torn Palace (CTP)

The CTP cog ([cogs/rewards/ctp.py](../cogs/rewards/ctp.py)) is the points-and-prizes system surfaced via the `~ctp` command group. It tracks per-user point balances, an editable prize catalog, time-bound redemptions, and a cumulative glint-investment leaderboard.

## Where to look

| Concern | File |
|---|---|
| Command surface (all ~22 subcommands) | [cogs/rewards/ctp.py](../cogs/rewards/ctp.py) |
| Append-only ledger + balance math | [cogs/rewards/lib/balance.py](../cogs/rewards/lib/balance.py) |
| Board CRUD + tasks.wynnvets.org URL formatting | [cogs/rewards/lib/boards.py](../cogs/rewards/lib/boards.py) |
| Prize catalog + redemption logic | [cogs/rewards/lib/prizes.py](../cogs/rewards/lib/prizes.py) |
| Glint invest / leaderboard / rank lookup | [cogs/rewards/lib/glints.py](../cogs/rewards/lib/glints.py) |
| Yes/No confirmation view (gift, glint bid) | [cogs/rewards/lib/confirm_view.py](../cogs/rewards/lib/confirm_view.py) |
| Embed builders and history-line formatters | [cogs/rewards/lib/formatting.py](../cogs/rewards/lib/formatting.py) |
| ORM tables | [orm.py](../orm.py) — `CTPBoard`, `CTPPrize`, `CTPLedger`, `CTPGlintInvestment` |
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
| Staff | `@is_staff()` | `balance`, `info`, `reward`, `redeem`, `access` |
| Admin | `@is_admin()` | `board`, `prize add/edit/disable/enable/remove/disclaim`, `set` |

## Glints leaderboard rules

`~ctp glints bids` and the "are/are_not glinted" line in `~ctp status` consult **only** users currently holding MEMBER, WAITLISTED, or HONOURARY. HIATUS, REGISTERED, no-state, and members the bot can't see are silently dropped from both. The investment row stays — they pop back on when they re-enter an eligible state.

Visible cutoff: `glints.GLINT_VISIBLE_CUTOFF = 8`. Ranks 1–8 render under **Currently Glinted**, ranks 9+ under **Standby**. `is_glinted` (used by `~ctp status`) is true iff `rank <= 8 AND rank is not None`.

Note: HIATUS users are NOT blocked from running `~ctp glints bid` — they keep accruing investment so re-joining the guild puts them back in the right position. Only the visible leaderboard and the `is_glinted` flag filter.

## Active redemptions

`~ctp access` lists ledger rows where `source='redeem' AND expires_at > now AND expires_at IS NOT NULL`. One-time prizes (no `expires_at`) never appear. The bot **does not** grant or strip Discord roles / channel permissions on redeem or expiry — staff handles all access changes manually (in-game rooms or the boardroom channel / pink role). The list is a reminder of "who is currently inside their window."

## `~ctp board` syntax (overloaded)

Dispatched by [`boards.apply_board_command`](../cogs/rewards/lib/boards.py). Parsing rules:

| Form | Effect | Required state |
|---|---|---|
| `~ctp board ART 2 <snowflake>` | Create or replace ART with `board_number=2` and the given role. | — |
| `~ctp board ART 6` | Move ART to `board_number=6`. | ART must already exist. |
| `~ctp board ART <snowflake>` | Change ART's role only. | ART must already exist. |
| `~ctp board ART 0` | Delete ART. | ART must already exist. |

"Snowflake" = numeric string of length ≥ 17. Short numbers are board IDs.

## Prize-info image

`~ctp prize info` renders one embed per category. When `CurrConfig.CTP_PRIZE_ARTWORK_URL` is set, the **first** embed only gets `set_image(url=...)` so paginating to a later page doesn't re-show the artwork. Set / clear via the `/config` admin command (persisted in `BotConfigOverride`).

## Seeding

Boards are populated in-place by admins via `~ctp board <ENUM> <num> <role_id>` after deploy. The prize catalog ships with a one-off seed: [seed-dazebot-ctp-catalog.sh](../../vets-deploy/scripts/one-off/seed-dazebot-ctp-catalog.sh). The seed is merge-safe — re-running picks up newly added rows without touching existing ones (admins still use `~ctp prize edit` for changes).

## Future hook: top-glinted export

The spec calls out "Glinted" users (top 8 by glint investment) as something temporary-server may want to know about later (e.g. to flip an in-game visual). When that lands, expose a tiny internal endpoint on dazebot that returns `glints.leaderboard(bot)[0]` and have temporary-server poll it. Don't add a Discord "Glinted" role — the rank is computed live from `CTPGlintInvestment + state_of(member)` and recomputing on every guild state change would be its own can of worms.

## Don't

- Don't store a mutable `balance` column on any table. Every "set" goes through a corrective ledger row.
- Don't auto-grant or auto-strip Discord access from redemptions. Staff drives this manually; the bot only tracks the timer.
- Don't add `~ctp uninvest` or a glint-refund path. The spec is explicit that investments are cumulative-only.
- Don't filter HIATUS users out of `~ctp glints bid` — they should keep accruing investment for when they rejoin.
- Don't bypass `boards.apply_board_command` for `~ctp board`. The overloaded parsing lives in one place by design.

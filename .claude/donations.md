# Donations

The donations cog ([cogs/rewards/donations/donations.py](../cogs/rewards/donations/donations.py)) is the staff-only `~donations` command group for recording and surfacing in-game donations to the guild. Most donations are **items, not currency** — staff assign each one an emerald-equivalent valuation and (usually) attach one or more screenshots as evidence.

The cog also drives slots 7-8 of `GET /api/internal/glinted` via the 5%-of-cumulative milestone rule below.

## Where to look

| Concern | File |
|---|---|
| Command surface (`record` / `recent` / `leaderboard` / `highest` / `info` / `edit value` / `edit comment` / `edit remove`) | [cogs/rewards/donations/donations.py](../cogs/rewards/donations/donations.py) |
| DB layer (record, list, leaderboard, highest, edit, remove, milestone math) | [cogs/rewards/donations/lib/svc.py](../cogs/rewards/donations/lib/svc.py) |
| Embed / line builders + comment truncation | [cogs/rewards/donations/lib/format.py](../cogs/rewards/donations/lib/format.py) |
| Emerald-value parser + stx formatter (reusable, also useful outside donations) | [lib/emerald.py](../lib/emerald.py) |
| Recipient resolution: Discord mention/id OR MC username/UUID → `MinecraftAccount` | [lib/mc/resolve.py](../lib/mc/resolve.py) (`resolve_donation_recipient`) |
| Yes/No confirmation for destructive `edit remove` | [cogs/rewards/lib/confirm_view.py](../cogs/rewards/lib/confirm_view.py) — shared with CTP |
| ORM table | [orm.py](../orm.py) — `Donation` |
| Glint slots 7-8 wiring | [api/main.py](../api/main.py) `/api/internal/glinted`; see [ctp.md](ctp.md) "Top-glinted export" |

## Channel restriction

The entire `~donations` group is locked to channel `1336152747644551248` (hardcoded as `DONATIONS_CHANNEL_ID` in the cog). Each subcommand re-checks at the top via `_channel_ok`. Off-channel invocations receive a one-line redirect reply and the command returns without side effects.

## Permission tiers

| Tier | Decorator | Subcommands |
|---|---|---|
| Staff | `@is_staff()` | `record`, `recent`, `leaderboard`, `highest`, `info` |
| Admin | `@is_admin()` | `edit value`, `edit comment`, `edit remove` |

Spec called for `~donations edit` to be "admin only" — edits are destructive (irreversible value changes, hard deletes) so they sit a tier above the rest. `@is_admin()` matches Discord Administrator perm or bot OPERATOR; staff role alone isn't sufficient.

## Value format

Staff type any of these for the `value` argument (case-insensitive, whitespace tolerated, parsed by `lib.emerald.parse_emeralds`):

- Bare integer: `5436584` → 5,436,584 raw emeralds
- Bare integer + e: `5436584e` (same)
- Compound: `20stx47le18eb40e` → 5,436,584 raw emeralds
- Decimal on any unit: `20stx47.29le` (decimals are floored after multiplication)

Internally stored as raw emeralds in `Donation.value_emeralds` (BigInt). Always displayed as decimal stx via `format_emeralds_as_stx` — two decimal places, trailing zeros preserved (e.g. `20.74 stx`).

Wynncraft unit conventions:

| Unit | Raw emeralds | Notes |
|---|---|---|
| `e` | 1 | base unit |
| `eb` | 64 | emerald block |
| `le` | 4,096 | liquid emerald |
| `stx` | 262,144 | stack of liquid emeralds (64 × 4,096) |

## Schema invariants

1. **PK is auto-increment int, not UUID.** `~donations info <id>` and `~donations edit <id>` are typed by humans; ints are friendlier. This is a deliberate departure from CTP's UUID convention — don't change without a UX win.

2. **Recipient FK is `RESTRICT`.** Deleting a `MinecraftAccount` must not silently orphan donation history. If a rename causes confusion, fix the underlying account; don't delete it.

3. **Value is in raw emeralds.** Conversion happens at the parse boundary. Don't store stx or le in the column — the value-display path round-trips through `format_emeralds_as_stx`, but the value-input path round-trips through `parse_emeralds`. Both expect raw integer emeralds at the storage layer.

4. **Comments are nullable; empty becomes `None`.** Long comments may contain PUA codepoints inside backtick blocks (spec line 12: `󰀀󰄀…`). They round-trip through Python `str` slicing as single codepoints. Truncation on list views is plain `text[:60] + "…"` — backtick blocks may be cut mid-block; the `info` view always shows the full text.

5. **Image URLs are Discord CDN, captured at record time.** Stored as a JSON list in `image_urls_json` (null when no attachments). Discord re-signs the URLs when the source message is re-fetched, so they remain viewable as long as the source message survives. If staff clear out the donations channel, `~donations info <id>` will show broken images. Re-hosting to `i.wynnvets.org` is a possible follow-up but out of scope for v1.

6. **`record` captures attachments only on prefix invocations.** Slash invocations don't expose `ctx.message.attachments` the same way. Staff should use `~donations record ...` (prefix) when screenshots are attached. This is a documented v1 limitation.

## 5%-milestone rule (drives glint slots 7-8)

A donation **qualifies as a glint milestone** iff:

```
value_emeralds * 20 >= cumulative_received_total_for_recipient (including this donation)
```

— i.e. the donation is at least 5% of the recipient's cumulative-received total at the time it was recorded. The first donation a recipient ever gets always qualifies (100% of cumulative).

The two most recent *distinct* recipients with a qualifying milestone fill slots 7-8 of `GET /api/internal/glinted` (slot 7 = most recent, slot 8 = second most recent). Subject to the same MEMBER / WAITLISTED / HONOURARY eligibility filter as slots 1-5: an ineligible / unlinked recipient yields `null` rather than promoting a lower-ranked donor. See [ctp.md](ctp.md) for the broader endpoint docstring (slot 6 is a separate online-donor slot fed from `leaderboard_totals_for_mc_ids`, also in [svc.py](../cogs/rewards/donations/lib/svc.py)).

**Computed live** by [`donation_milestone_recipients`](../cogs/rewards/donations/lib/svc.py): walks all donations chronologically, tracks per-recipient cumulative, marks each donation's qualification, then ranks recipients by their latest qualifying timestamp. No separate "milestone events" table — donations are editable / removable, so a derived ledger would need invalidation. At expected volume (tens to low hundreds per week), the full walk on each endpoint hit is cheap.

Edits and removes propagate naturally: if an admin lowers a qualifying donation's value below the 5% threshold, the recipient drops out of slot 7-8 on the next 5-minute poller tick (temporary-server's `GlintedPoller`).

## Verification

- **Parser**: `uv run pytest tests/test_emerald.py -v` — covers every example from the spec plus parse-error edge cases.
- **Milestone math**: `uv run pytest tests/test_donation_milestone.py -v` — covers single donation qualifies, small follow-up doesn't, big later donation requalifies, remove reshuffles, `limit=2` deduplicates.
- **Filtered-totals math** (slot 6 source): `uv run pytest tests/test_donations_filtered_totals.py -v` — covers empty mc_ids returns `[]` (not all), single-recipient sum, desc ordering, non-matching mc_ids excluded.
- **Top eligible donors** (donor-pool endpoint source): `uv run pytest tests/test_top_eligible_donors.py -v` — covers empty donations, limit cap, eligibility filter, ordering, no-guild defensive behaviour.

## Donor-pool endpoint (`GET /api/internal/donor_candidates`)

Sibling to `/api/internal/glinted`, also gated by `DAZEBOT_INTROSPECT_SECRET`. Returns the top-20 cumulative donation recipients filtered to MEMBER / WAITLISTED / HONOURARY, ranked by total desc. No "currently online" filter — that happens in vetsmod against the live tab list (~1s latency vs. the 5-min poll cycle that drives `/api/internal/glinted`).

Shape:

```
{"donors": [{"mc_uuid": str, "mc_username": str}, ...]}
```

Computed by [`top_eligible_donors`](../cogs/rewards/donations/lib/svc.py) from the existing `leaderboard_totals()` + `is_eligible_member`. The cap is applied **after** eligibility, so a result of 20 entries is always 20 *eligible* recipients (not "first 20 raw, may shrink after filter"). The vetsmod side stores this as a ranked list and, on every game tick, picks the highest-ranked entry currently in the tab list to shimmer.

This co-exists with the `/api/internal/glinted` slot-6 logic during the rollout; once new vetsmod is widely deployed, slot 6 can be removed server-side.
- **End-to-end**: in channel `1336152747644551248`, run `~donations record <mc> 5436584 hello` (with an image), `~donations recent`, `~donations info 1`, `~donations edit 1 value 1stx`, `~donations edit 1 remove`. Hit `GET /api/internal/glinted` to confirm slots 7-8 populate after a qualifying donation.

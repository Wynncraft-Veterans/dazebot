# Command permissions

Single-source-of-truth audit of every slash/prefix command exposed by the
bot, with its permission gate. Update this file whenever a command is added,
removed, renamed, or has its tier changed.

The decorators live in [`lib/auth.py`](lib/auth.py). Cogs live under domain subfolders (`membership/`, `moderation/`, `events/`, `activity/`, `integrations/`, `meta/`).

## Tiers

From least to most privileged:

| Tier        | Definition |
|-------------|------------|
| `PUBLIC`    | Anyone in the server. No decorator. |
| `REGISTERED`| Has any membership-state role (Registered, Waitlisted, Honourary, Hiatus, or Member). |
| `GUILD`     | Has Waitlisted, Honourary, Hiatus, or Member. |
| `STAFF`     | Has the configured `STAFF_ROLE`. |
| `ADMIN`     | Has the Discord “Administrator” permission. |
| `OPERATOR`  | Listed in `CurrConfig.ADMINS` (bot owners / devs). |

Higher tiers always satisfy lower ones (an `OPERATOR` can run a `STAFF`
command, an `ADMIN` can run a `GUILD` command, and so on).

Special, non-hierarchical:

| Tier      | Definition |
|-----------|------------|
| `SHOUTER` | Has `manage_messages` permission, OR holds one of the regional alert roles (USA / Europe / Asia). Used by `/shout send` and the `/shout last`+`/shout board`+`/purgelist` `STAFF | SHOUTER` reads. |

## Slash command surface

### Bot maintenance — `cogs/moderation/admin.py`

| Command | Tier | Notes |
|---|---|---|
| `/admin sync` | OPERATOR | |
| `/admin reload <cog>` | OPERATOR | `ALL` reloads every cog |
| `/admin load <cog>` | OPERATOR | |
| `/admin unload <cog>` | OPERATOR | |
| `/say <msg>` | ADMIN | |
| `/embed [color] [title] <description>` | ADMIN | |
| `/shouts set <user> <count>` | OPERATOR | Forcefully overwrites `shout_count` |

### Linking — `cogs/moderation/admin.py` (`/link` group)

| Command | Tier | Notes |
|---|---|---|
| `/link set <user> <username_or_uuid>` | STAFF | Wynncraft-API fallback creates the MC row if missing |
| `/link remove <user>` | STAFF | |
| `/link check <user>` | STAFF | |
| `/link request <username_or_uuid>` | REGISTERED | Practically only useful for alts post-migration |
| `/link requests` | STAFF | List pending requests |
| `/link approve <id>` | STAFF | |
| `/link code <username>` | PUBLIC | DMs a one-time code; consumed by chatting it on the MC server |
| `/link info` | PUBLIC | Pretty embed explaining the linking flow |

### Membership — `cogs/membership/` (split from former `cogs/management.py`)

| Command | Tier | Notes |
|---|---|---|
| `/first_install [channel] [quote_message_id]` | OPERATOR | Posts the welcome embed + “Link Minecraft” button; wipes legacy/vanity roles |
| `/script edit_welcome <channel> <message_id>` | OPERATOR | Rewrites a previously-posted welcome message |
| `/script rename_cult` | OPERATOR | One-off: renames the `dazecult` DB row to `deercult` |
| `/block <target> [reason]` | STAFF | Single command (no subgroup) |
| `/unblock <target>` | STAFF | |
| `/force change <target> <transition>` | ADMIN | Currently only `registered → hiatus` |
| `/force check` | ADMIN | Triggers the periodic guild check immediately |
| `/add <username> <user>` | STAFF | Register an alt (or set primary if none) |
| `/honour <user>` | ADMIN | Sets Discord role + flips `is_honourary` (bridge access) on primary MC |
| `/unhonour <user>` | ADMIN | Reverses both |
| `/list unlinked` | STAFF | In-game members not linked to a Discord account |
| `/list linked` | STAFF | Every Discord↔MC link known to the bot |
| `/info <target>` | REGISTERED | Discord member OR MC name/UUID. Replaces `/username`, `/last_online`, `/joindate` |
| `/vanity set <year_or_date>` | PUBLIC | Self-assign |
| `/vanity force <user> <year_or_date>` | STAFF | |
| `/waitlist add <user> [username]` | STAFF | |
| `/waitlist view` | REGISTERED | |
| `/waitlist remove <username_or_uuid>` | STAFF | |
| `/waitlist self` | GUILD | Self-add (former `/join_guild`) |
| `/waitlist leave` | PUBLIC | Self-remove (former `/leave_waitlist`) |
| `/config list \| get \| set \| reset` | ADMIN | Runtime-config overrides |
| `/alerts status \| mute \| unmute \| thresholds` | ADMIN | Shortcut wrappers around `/config` for guild dead/full alerts |
| `/alerts hiatus_mute \| hiatus_unmute` | ADMIN | Master switch for hiatus-spotted detection — silences the `#activity` post **and** the return DM, and suspends the bulk `/v3/player` poll |
| `/alerts return_dm_on \| return_dm_off` | ADMIN | Just the "welcome back from your hiatus" DM; leaves the `#activity` post running |
| `/alerts return_dm_check \| return_dm_clear <user>` | ADMIN | Inspect / lift one user's return-DM mute or snooze |

### Activity / shouts — `cogs/activity/activity.py`

| Command | Tier | Notes |
|---|---|---|
| `/purgelist [days] [refresh]` | STAFF \| SHOUTER | Inactive Returners members |
| `/shout send` | SHOUTER | Records a shout you just did in-game |
| `/shout last` | STAFF \| SHOUTER | Three most recent shouts |
| `/shout board` | STAFF \| SHOUTER | Per-shouter shout-count leaderboard |

### Weekly event — `cogs/events/weekly_event.py`

| Command | Tier | Notes |
|---|---|---|
| `/score set <user> <week> <value>` | STAFF | |
| `/score add <user> <week> <value>` | STAFF | |
| `/score print <user> <week>` | PUBLIC | |
| `/score leaderboard <week>` | PUBLIC | |
| `/count <channel> [override_emoji]` | STAFF | Forum-channel reaction tally |

### Utility — `cogs/meta/utility.py`

| Command | Tier | Notes |
|---|---|---|
| `/findprofer [include_non_members]` | GUILD | Profession matchmaking |

### Documentation — `cogs/meta/documentation.py`

| Command | Tier | Notes |
|---|---|---|
| `/docs ...` | PUBLIC | |

### Miscellaneous — `cogs/meta/pointless.py`

| Command | Tier | Notes |
|---|---|---|
| `/randomfact` | PUBLIC | |

### Returns — `cogs/events/return_cmd.py` (+ `cogs/events/returns/` package)

| Command | Tier | Notes |
|---|---|---|
| `/return <id> [action] [cult] [owner] [target] [message] [flag]` | PUBLIC (per-action gates inside the handler) | Dispatches to the week-`id` handler in `cogs/events/returns/week_<id>.py`. Signature is intentionally expandable — append optional kwargs as features land. |
| `/return 0 join <cult>` | REGISTERED | Switch the caller's active cult (mutually exclusive teams). 6-month self-switch cooldown. |
| `/return 0 add <cult> <owner>` | ADMIN | Create a cult; `owner` is a Discord mention/id, MC username, or MC UUID. |
| `/return 0 list <cult>` | REGISTERED | Print figurehead + staff + members for a cult. |
| `/return 0 force <cult> target:<user>` | STAFF | Staff override: move a user to a cult, ignoring the self-switch cooldown. Figurehead exclusion still enforced. |
| `/return 0 announce message:<text>` | ADMIN | Broadcast `message` to every cult thread via the bot. User mentions only (no role/@everyone). |
| `/return 0 install_intercult` | ADMIN | Idempotently posts + pins the persistent intercult button in each cult thread. Click → select target cult → modal → one message per cult per 24h, mirrored back to the sender's thread. |
| `/joincult <cult>` | REGISTERED | Shortcut for `/return 0 join`. Slash autocomplete lists every existing cult. |
| `/return 73` | ROLE-GATED (`1407078065137254563` / `1407078148440592444` / `1407078577450520637`; ADMIN+ bypass) | Collaborative story. No args shows the most recent segment + how to add. |
| `/return 73 message:<text>` | same role gate | Append the next fragment (capped at 125 chars; any punctuation allowed anywhere; can't have authored either of the last two segments). Awards +1 week-73 `/score`; posts a non-ephemeral "added" message. |
| `/return 73 action:full` | ADMIN | Dump the entire week-73 story (paginated, ephemeral). |

## Background / listener-only cogs (no slash surface)

* `cogs/membership/join.py` — janitor tasks: `clear_old_requests`, `waitlist_cleanup`.
* `cogs/membership/vanity_roles.py`, `cogs/integrations/api_server.py` — listeners only.

## Notes & migration log (phase 7)

Renames / restructures applied in the latest sweep:

| Old | New |
|---|---|
| `/sync`, `/reload`, `/load`, `/unload` | `/admin sync`, `/admin reload`, `/admin load`, `/admin unload` |
| `/set_shout_count` | `/shouts set` |
| `/honourary set/remove` | folded into `/honour` / `/unhonour` (also flips `is_honourary` MC flag) |
| `/first_install`, `/script *` | tier raised: ADMIN → OPERATOR |
| `/say`, `/embed` | tier raised: OPERATOR → ADMIN |
| `/block add` / `/block remove` | flattened to `/block` (single command) + existing `/unblock` |
| `/force` | promoted to a group: `/force change` (was `/force`), `/force check` (was `/force_check`); tier raised STAFF → ADMIN |
| `/register` | removed; use `/link set` |
| `/username` | renamed to `/info` (REGISTERED) and merged with `/last_online` and `/joindate` |
| `/waitlist view` | tier lowered: STAFF → REGISTERED |
| `/request_link`, `/link_requests`, `/link_approve`, `/link_code`, `/linking` | moved into `/link` group as `request`, `requests`, `approve`, `code`, `info` |
| `/join_guild` | `/waitlist self` |
| `/leave_waitlist` | `/waitlist leave` |
| `/force_check` | `/force check` |
| `/purgelist` | gate added: STAFF \| SHOUTER |
| `/shouterboard` | `/shout board` |
| `/last_shout` | `/shout last` |
| `/shout` | `/shout send` (now lives under the `/shout` group) |
| `/last_online`, `/joindate` | merged into `/info` |
| `/score set`, `/score add` | tier lowered: OPERATOR → STAFF |
| `/count` | tier raised: PUBLIC → STAFF |
| `/findprofer` | tier raised: PUBLIC → GUILD |

### Constraint reminder

A Discord slash command cannot simultaneously be invokable AND have
subcommands. This means the bare `/link`, `/shout`, `/force`, `/admin`,
`/waitlist`, `/score`, `/list`, `/script`, `/vanity`, `/config`, `/alerts`,
and `/shouts` slash entries are not directly invokable as slash commands —
users must pick a subcommand. The group-level callbacks only fire in the
prefix-command path.

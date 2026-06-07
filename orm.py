from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from tortoise import fields
from tortoise.models import Model

from lib.util import ProfCategory
from lib.mc.wynn_api.player_models import CharacterProfessionsType

import uuid


def _resolve_db_path() -> str:
    """Resolve the dazebot SQLite path. Production mounts ``/app/data``; locally
    we fall back to ``dazebot.db`` in the working directory. ``DAZEBOT_DB_PATH``
    overrides both — set in tests to point at a tmp_path copy.
    """
    p = os.environ.get("DAZEBOT_DB_PATH")
    if p is None:
        p = "/app/data/dazebot.db" if os.path.isdir("/app/data") else "dazebot.db"
    return p


def _build_tortoise_config(db_path: str | None = None) -> dict:
    """Build a tortoise config dict pointing at ``db_path`` (or the resolved
    default). Built fresh per call so test monkeypatching of
    ``DAZEBOT_DB_PATH`` takes effect — the module-level ``TORTOISE_ORM``
    below is for aerich CLI use and is frozen at import time.
    """
    path = db_path if db_path is not None else _resolve_db_path()
    return {
        "connections": {"default": f"sqlite://{path}"},
        "apps": {
            "models": {
                "models": ["orm", "aerich.models"],
                "default_connection": "default",
            }
        },
    }


# Aerich's CLI resolves this dotted path via ``pyproject.toml [tool.aerich]``.
# Frozen at module import — runtime users (``init_db``) build their own via
# ``_build_tortoise_config`` so test-time env vars take effect.
TORTOISE_ORM = _build_tortoise_config()


# Sentinel value written to ``MinecraftAccount.last_online`` when the upstream
# Wynncraft API reports ``lastJoin = None``. This happens when the player has
# opted out via privacy settings (https://docs.wynncraft.com/privacy) OR when
# the account simply has no join data on record. The DB column is NOT NULL,
# so we use the Unix epoch as an in-band "unknown" marker. Use
# :func:`is_last_online_unknown` to test for it; never compare directly.
UNKNOWN_LAST_ONLINE: datetime = datetime.fromtimestamp(0, tz=timezone.utc)


def is_last_online_unknown(dt: datetime | None) -> bool:
    """Return True if ``dt`` is the "unknown / API-hidden" sentinel.

    ``None`` is treated as unknown for forward-compat with a future schema
    migration to a nullable column.
    """
    if dt is None:
        return True
    # Anything within 24h of the epoch is treated as the sentinel; avoids any
    # tz-roundtrip drift around 1970-01-01.
    return dt <= datetime.fromtimestamp(86400, tz=timezone.utc)


class MinecraftAccount(Model):
    id = fields.UUIDField(pk=True)

    uuid = fields.CharField(max_length=36, unique=True)
    guild: Optional[str] = fields.CharField(max_length=255, null=True, blank=True)  # type: ignore
    wynn_username = fields.CharField(max_length=255)
    mc_username = fields.CharField(max_length=255)
    last_online = fields.DatetimeField(use_tz=True)
    last_manual_check = fields.DatetimeField(use_tz=True)
    # Most recent observed value of the Wynncraft /v3/player `server` field
    # (e.g. ``EU37``). Privacy-hidden players who hide ``lastJoin`` typically
    # still expose ``server``; the watcher in ``cogs/server_watcher.py``
    # treats a between-tick change in this value as proof of activity and
    # bumps ``last_online``. NULL = never observed.
    last_seen_server: Optional[str] = fields.CharField(max_length=16, null=True, default=None)  # type: ignore
    server_observed_at: datetime | None = fields.DatetimeField(use_tz=True, null=True)  # type: ignore
    # Most recent guild-tick observation that ``lastJoin`` was privacy-hidden
    # for this account. Drives server_watcher's polling scope so accounts
    # whose ``last_online`` has been bumped out of the epoch sentinel are
    # still re-polled while their ``lastJoin`` remains hidden — closes the
    # asymmetry where a one-shot un-hide → re-hide cycle would silently age
    # the account into purgelist's "Unknown" bucket with no way back out.
    # NULL = ``lastJoin`` was visible at the last guild tick (or account
    # predates this signal). Cleared by ``_apply_guild`` the instant a
    # non-null ``lastJoin`` is seen.
    lastjoin_hidden_at: datetime | None = fields.DatetimeField(use_tz=True, null=True)  # type: ignore
    # Snapshot of every monotonic /v3/player counter the server_watcher
    # tracks for stat-delta activity inference. Keys are the field names
    # from the envelope (e.g. ``playtime``, ``contentCompletion``,
    # ``raidStats.damageDealt``, ``dungeons.total``). The watcher OR-folds
    # strict increases across every key — any one bump = activity signal.
    # JSON over per-counter columns because the set is large (~17) and
    # expected to grow as Wynncraft adds stats; we never query these in
    # SQL WHERE clauses, only read them per-tick in Python.
    last_stat_snapshot: dict | None = fields.JSONField(null=True, default=None)  # type: ignore
    first_join: datetime | None = fields.DatetimeField(use_tz=True, null=True)  # type: ignore
    token: Optional[str] = fields.CharField(max_length=6, null=True, default=None)  # type: ignore
    is_honourary = fields.BooleanField(default=False)

    discord_account: fields.ReverseRelation[DiscordAccount]
    waitlist: fields.ReverseRelation[Waitlist]

    class Meta:
        table = "minecraft_accounts"


class ProfessionCategories(Model):
    id = fields.UUIDField(pk=True)
    minecraft_account = fields.ForeignKeyField(
        "models.MinecraftAccount", related_name="profession_categories", on_delete=fields.CASCADE
    )
    prof_type = fields.CharEnumField(CharacterProfessionsType)

    category = fields.CharEnumField(ProfCategory)

    class Meta:
        table = "profession_categories"
        unique_together = (("minecraft_account", "prof_type"),)


class DiscordAccount(Model):
    id = fields.UUIDField(pk=True)
    # OneToOneField instead of ForeignKeyField(..., unique=True): the latter
    # accepts unique=True but silently drops it at the DDL layer, leaving
    # ``minecraft_account_id`` non-unique on disk. OneToOneField generates
    # the same FK plus an actual UNIQUE constraint.
    minecraft_account: fields.OneToOneNullableRelation[MinecraftAccount] = fields.OneToOneField(
        "models.MinecraftAccount", related_name="discord_account", null=True, on_delete=fields.SET_NULL
    )
    minecraft_account_id: uuid.UUID | None

    disc_uuid = fields.CharField(max_length=255, unique=True)
    shout_count = fields.IntField(default=0)

    class Meta:
        table = "discord_accounts"


class WeeklyEvent(Model):
    id = fields.UUIDField(pk=True)
    title = fields.CharField(max_length=255, null=True)
    week = fields.IntField(unique=True)

    class Meta:
        table = "weekly_events"


class Score(Model):
    id = fields.UUIDField(pk=True)
    event = fields.ForeignKeyField("models.WeeklyEvent", related_name="scores", on_delete=fields.CASCADE)
    discord_account = fields.ForeignKeyField("models.DiscordAccount", related_name="scores", on_delete=fields.CASCADE)
    score = fields.IntField()

    class Meta:
        table = "scores"
        unique_together = (("event", "discord_account"),)


class DeadGuildAlert(Model):
    id = fields.UUIDField(pk=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "dead_guild_alerts"


class GuildCapacityAlert(Model):
    id = fields.UUIDField(pk=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "guild_capacity_alerts"


class JanitorAlert(Model):
    """Restart-safe throttle marker for the data-integrity janitor's Discord
    summary (cogs/maintenance/janitor.py). One row per posted summary; the
    janitor reads the most recent ``created_at`` to decide whether the
    JANITOR_ALERT_DELTA window has elapsed. Mirrors DeadGuildAlert/
    GuildCapacityAlert exactly."""

    id = fields.UUIDField(pk=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "janitor_alerts"


class HiatusSpottedAlert(Model):
    """Restart-safe per-UUID cooldown marker for hiatus-spotted alerts.
    One row per posted alert; the watcher cogs query the most recent row
    per ``uuid`` to enforce a 24h cooldown between repeat alerts about
    the same player. Mirrors DeadGuildAlert/GuildCapacityAlert + a uuid
    column for the per-player keying."""

    id = fields.UUIDField(pk=True)
    uuid = fields.CharField(max_length=36, index=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "hiatus_spotted_alerts"


class Shout(Model):
    id = fields.UUIDField(pk=True)
    shouter = fields.ForeignKeyField("models.DiscordAccount", related_name="shouts", on_delete=fields.CASCADE)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "shouts"


def short_id():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


class LinkRequest(Model):
    id = fields.CharField(pk=True, max_length=4, default=short_id)
    minecraft_account: fields.ForeignKeyRelation[MinecraftAccount] = fields.ForeignKeyField(
        "models.MinecraftAccount", related_name="link_requests", on_delete=fields.CASCADE
    )
    discord_account: fields.ForeignKeyRelation[DiscordAccount] = fields.ForeignKeyField(
        "models.DiscordAccount", related_name="link_requests", on_delete=fields.CASCADE
    )
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "link_requests"
        unique_together = (("minecraft_account", "discord_account"),)


class Waitlist(Model):
    id = fields.UUIDField(pk=True)
    minecraft_account: fields.OneToOneRelation[MinecraftAccount] = fields.OneToOneField(
        "models.MinecraftAccount", related_name="waitlist", on_delete=fields.CASCADE
    )
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "waitlist"


class Blocklist(Model):
    """Users on this list are forced to the Registered role and can never become
    Honourary / Hiatus / Member / Waitlisted. See .claude/membership_spec.md §2b.
    """

    id = fields.UUIDField(pk=True)
    minecraft_account: fields.OneToOneRelation[MinecraftAccount] = fields.OneToOneField(
        "models.MinecraftAccount", related_name="blocklist", on_delete=fields.CASCADE
    )
    reason: Optional[str] = fields.CharField(max_length=500, null=True)  # type: ignore
    blocked_by_disc_uuid = fields.CharField(max_length=255, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "blocklist"


class StaffActionEntry(Model):
    """Audit row for a single caution / warning / eject event recorded by
    in-game guild staff against a Minecraft player. See
    ``../.claude/staff_actions.md`` for the full spec.

    Cumulative caution-point total for a target = SUM(points) over all
    rows for ``target_uuid``. Three caution points = one "warning point";
    two warning points (= 6 caution points) trips the auto-ban
    threshold. Decay is not implemented; entries persist forever.

    Read by ``GET /api/internal/staff-actions/{uuid}`` (consumed by
    vetsmod's ``/wv check``) and by the Discord ``~warnings`` command.
    Written by ``POST /api/internal/staff-action`` (forwarded by
    temporary-server when a staff member runs ``/caution``, ``/warn``,
    or ``/eject`` in the mod).

    The target's MinecraftAccount may or may not exist at write time --
    cautions can be issued against unlinked players. We key by raw
    ``target_uuid`` (no FK) so insertion never fails on a missing
    parent row.
    """

    id = fields.UUIDField(pk=True)

    target_uuid = fields.CharField(max_length=36, index=True)
    target_username_at_time = fields.CharField(max_length=64)

    actor_uuid = fields.CharField(max_length=36)
    actor_username_at_time = fields.CharField(max_length=64)

    # "caution" | "warning" | "eject"
    kind = fields.CharField(max_length=16)
    # 1 (caution), 3 (warning), 6 (eject). Stored explicitly so future
    # tweaks to the conversion ratio don't retroactively rewrite history.
    points = fields.IntField()
    # Formal warning text. Required for kind=warning/eject; null for plain
    # cautions. May also be set on a caution that crossed the warning
    # threshold (the staff member shaped the warning at /caution time).
    message: Optional[str] = fields.TextField(null=True)  # type: ignore

    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "staff_action_entries"


class MinecraftAlt(Model):
    """Additional Minecraft accounts attached to a Discord user beyond their
    primary linked one (used by /add). The 'primary' link still lives on
    DiscordAccount.minecraft_account_id; this table holds the others.
    """

    id = fields.UUIDField(pk=True)
    discord_account: fields.ForeignKeyRelation[DiscordAccount] = fields.ForeignKeyField(
        "models.DiscordAccount", related_name="alts", on_delete=fields.CASCADE
    )
    minecraft_account: fields.ForeignKeyRelation[MinecraftAccount] = fields.ForeignKeyField(
        "models.MinecraftAccount", related_name="alt_of", on_delete=fields.CASCADE
    )
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "minecraft_alts"
        unique_together = (("discord_account", "minecraft_account"),)


class UserVanityChoice(Model):
    """Records that a user has manually picked a vanity role via /vanity. The
    automatic firstJoin-based assignment must NOT override this.
    See .claude/membership_spec.md §2g.
    """

    id = fields.UUIDField(pk=True)
    disc_uuid = fields.CharField(max_length=255, unique=True)
    role_id = fields.CharField(max_length=255)
    chosen_by_staff = fields.BooleanField(default=False)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "user_vanity_choices"


class MojangNameCache(Model):
    """Persistent cache of Mojang uuid -> username lookups (.claude/membership_spec.md §5).
    Refresh after `MAX_AGE` to handle name changes.
    """

    uuid = fields.CharField(pk=True, max_length=36)
    username = fields.CharField(max_length=255)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "mojang_name_cache"


class BotConfigOverride(Model):
    """Persisted overrides for Config attributes set via /config admin command.
    See .claude/membership_spec.md §3.
    """

    key = fields.CharField(pk=True, max_length=255)
    # JSON-encoded value, so any of int / bool / list / etc. round-trips cleanly.
    value_json = fields.TextField()
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "bot_config_overrides"


class FirstInstallMonitor(Model):
    """Persisted record of the message being monitored for /first-install chains
    reactions. There may be exactly one active row at a time (enforced in
    application logic).
    """

    id = fields.UUIDField(pk=True)
    guild_id = fields.CharField(max_length=255)
    channel_id = fields.CharField(max_length=255)
    message_id = fields.CharField(max_length=255, unique=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "first_install_monitors"


class DMSentLog(Model):
    """Tracks one-shot DMs already sent (e.g. first-install chains-DM, member
    inactivity warning) so we don't spam the same user repeatedly.
    """

    id = fields.UUIDField(pk=True)
    disc_uuid = fields.CharField(max_length=255)
    kind = fields.CharField(max_length=64)
    sent_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "dm_sent_log"
        unique_together = (("disc_uuid", "kind"),)


class Cult(Model):
    """A `/return 0` cult: a mutually-exclusive team a Discord user can join.

    The owner ("figurehead") is a MinecraftAccount, identified by an
    in-game username or UUID at creation time. ``thread_id`` is the Discord
    private-thread the cult lives in — used by ``cogs.events.returns.week_0``
    for membership sync and by ``cogs.events.returns.lib.views.intercult_view``
    for cross-cult messaging. Nullable so the column can be added by the
    idempotent ALTER in :func:`init_db` without a default; the per-row
    backfill from the legacy
    ``cogs.events.returns.lib.cult_threads.CULT_THREADS`` map runs on
    startup (see ``Returns.cog_load``).
    """

    id = fields.UUIDField(pk=True)
    # Stored lowercased so unique=True doubles as a case-insensitive guard.
    name = fields.CharField(max_length=64, unique=True)
    owner: fields.ForeignKeyRelation[MinecraftAccount] = fields.ForeignKeyField(
        "models.MinecraftAccount", related_name="owned_cults", on_delete=fields.RESTRICT
    )
    thread_id: Optional[int] = fields.BigIntField(null=True)  # type: ignore
    created_at = fields.DatetimeField(auto_now_add=True)

    memberships: fields.ReverseRelation[CultMembership]

    class Meta:
        table = "cults"


class CultMembership(Model):
    """A Discord user's active cult. Mutually exclusive: at most one row per
    DiscordAccount, enforced by the UNIQUE constraint on the
    ``discord_account`` FK (via OneToOneField).
    """

    id = fields.UUIDField(pk=True)
    cult: fields.ForeignKeyRelation[Cult] = fields.ForeignKeyField(
        "models.Cult", related_name="memberships", on_delete=fields.CASCADE
    )
    discord_account: fields.OneToOneRelation[DiscordAccount] = fields.OneToOneField(
        "models.DiscordAccount", related_name="cult_membership", on_delete=fields.CASCADE
    )
    joined_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "cult_memberships"


class IntercultMessage(Model):
    """One outbound cross-cult message sent via the pinned intercult button.

    Append-only audit log; the 24h-per-cult cooldown is derived by querying
    the most recent row whose ``sender_cult_name`` matches. Cult names
    (not FKs) are stored so a future cult rename via ``/script rename_cult``
    leaves history pointing at the name-at-time without dangling FKs.
    """

    id = fields.UUIDField(pk=True)
    sender_cult_name = fields.CharField(max_length=64, index=True)
    target_cult_name = fields.CharField(max_length=64)
    sender_disc_uuid = fields.CharField(max_length=255)
    content = fields.TextField()
    sent_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "intercult_messages"


class RecruitmentQuery(Model):
    """One click of the pinned recruitment button.

    Append-only audit log; the per-cult cooldown is derived by querying the
    most recent row whose ``cult_name`` matches. Cult names (not FKs) are
    stored for the same rename-resilience reason as ``IntercultMessage``.
    """

    id = fields.UUIDField(pk=True)
    cult_name = fields.CharField(max_length=64, index=True)
    requester_disc_uuid = fields.CharField(max_length=255)
    queried_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "recruitment_queries"


class LinkCode(Model):
    """A persistent (until consumed) Minecraft<->Discord link code.

    Keyed by ``mc_username`` (lowercased) so re-running ``/link code`` for the
    same username re-DMs the same code, while a different discord user
    requesting the same username overwrites the row (invalidating the prior
    code).
    """

    id = fields.UUIDField(pk=True)
    mc_username = fields.CharField(max_length=64, unique=True)  # lowercased
    disc_uuid = fields.CharField(max_length=255)
    code = fields.CharField(max_length=16)  # 6-char alphanumeric, case-insensitive on compare
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "link_code"


class VerifyKey(Model):
    """A long-lived bearer token that authenticates a Discord user's vetsmod
    client to ``temporary-server`` (``api.wynnvets.org``).

    Issued by ``/vetsmod``. The user types ``/unlock <key>`` in their Minecraft
    client, vetsmod stores it, and every subsequent v1 WebSocket connection
    carries it in an ``auth`` frame. ``temporary-server`` validates by hitting
    dazebot's ``POST /api/auth/introspect``.

    One row per Discord user. Re-running ``/vetsmod`` returns the same key
    (analogous to ``LinkCode`` reuse). Staff can revoke via ``/change remove
    key`` (or rotate via ``/change rotate key``); revocation sets
    ``revoked_at`` and causes future introspections to fail.

    The ``tier`` and ``mc_uuid`` columns are a *snapshot at last refresh*. They
    are re-resolved against current Discord roles + linked MC account on every
    successful introspection, so a user who changes tier (waitlisted -> member,
    blocklisted, etc.) gets the right access without re-running ``/vetsmod``.
    """

    id = fields.UUIDField(pk=True)
    # The token itself. ``secrets.token_urlsafe(32)`` -> 43 char base64url.
    # Indexed for O(1) introspection lookup.
    key = fields.CharField(max_length=64, unique=True)
    # One key per Discord user; re-running /vetsmod returns the same row.
    disc_uuid = fields.CharField(max_length=255, unique=True)
    # Snapshot at last introspection. May be empty if the user is unlinked.
    mc_uuid: Optional[str] = fields.CharField(max_length=36, null=True)  # type: ignore
    mc_username: Optional[str] = fields.CharField(max_length=64, null=True)  # type: ignore
    tier = fields.CharField(max_length=32)
    created_at = fields.DatetimeField(auto_now_add=True)
    last_used_at: Optional[datetime] = fields.DatetimeField(null=True)  # type: ignore
    revoked_at: Optional[datetime] = fields.DatetimeField(null=True)  # type: ignore

    class Meta:
        table = "verify_keys"


class BuildPromotion(Model):
    """A workshop->library forum-thread promotion performed by /promote.

    Used by /demote to find the original workshop thread (so demotion can
    sync new comments into it and unlock it, instead of creating a fresh
    one). Rows are deleted on successful /demote. Stale rows (library
    thread manually deleted, workshop thread gone, etc.) are cleaned up
    on the next /promote of the same workshop thread.

    ``sync_complete_at`` marks when the last reposted message landed in
    the library thread. /demote uses it as the ``after=`` cutoff to find
    *genuinely new* library messages (everything before it is either the
    bot's metadata header or a webhook-impersonated repost of original
    workshop content).
    """

    id = fields.UUIDField(pk=True)
    library_thread_id = fields.CharField(max_length=64, unique=True)
    workshop_thread_id = fields.CharField(max_length=64, unique=True)
    promoted_by_disc_uuid = fields.CharField(max_length=255)
    promoted_at = fields.DatetimeField(auto_now_add=True)
    sync_complete_at = fields.DatetimeField(use_tz=True)

    class Meta:
        table = "build_promotions"


class ReturnGuess(Model):
    """One user's emerald-price guess for a ``/return 75`` day-N slot.

    Used by ``cogs/events/returns/week_75.py``: each accepted submission
    appends one row. Unique per (``week``, ``day``, ``discord_account``) —
    re-submission for the same day is rejected (the user must ask staff if
    they want their guess cleared).

    Keyed by ``week`` (not an FK to ``WeeklyEvent``) for the same reason
    ``StorySegment`` is: a future price-guessing week picks a new int with
    no schema change. ``day`` is 1-7. ``price`` is the guess in raw
    emeralds; the manage view re-renders it as stx/le/e.
    """

    id = fields.UUIDField(pk=True)
    week = fields.IntField(index=True)
    day = fields.IntField()
    discord_account: fields.ForeignKeyRelation[DiscordAccount] = fields.ForeignKeyField(
        "models.DiscordAccount", related_name="return_guesses", on_delete=fields.CASCADE
    )
    price = fields.IntField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "return_guesses"
        unique_together = (("week", "day", "discord_account"),)


class Apartment(Model):
    """One Returners apartment row. ``number`` is unique — ``~apartment create``
    refuses on collision. NULL ``owner_mc_username`` means VACANT. Each user is
    expected to own at most one apartment; this is enforced by the cog logic
    (``cogs/community/apartment.py``), not the schema.
    """

    id = fields.UUIDField(pk=True)
    number = fields.CharField(max_length=16, unique=True)
    owner_mc_username: Optional[str] = fields.CharField(max_length=64, null=True)  # type: ignore
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "apartments"


class StorySegment(Model):
    """One fragment of a collaborative ``/return`` story event.

    Used by ``cogs/events/returns/week_73.py``: each accepted ``/return 73``
    submission appends one row. The full story is reconstructed by ordering
    rows for a ``week`` by ``created_at`` and concatenating ``content``.

    Keyed by ``week`` (not an FK to WeeklyEvent) so a future story week just
    picks a new int — no schema change. Scoring still goes through the normal
    ``WeeklyEvent``/``Score`` tables. The back-to-back guard reads the most
    recent row's ``discord_account`` to forbid the same author twice running.
    """

    id = fields.UUIDField(pk=True)
    week = fields.IntField(index=True)
    discord_account: fields.ForeignKeyRelation[DiscordAccount] = fields.ForeignKeyField(
        "models.DiscordAccount", related_name="story_segments", on_delete=fields.CASCADE
    )
    content = fields.TextField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "story_segments"


# --- Chore-Torn Palace (CTP) ----------------------------------------------
# Points / rewards system surfaced via the ``~ctp`` cog group. See
# ``cogs/rewards/`` for the command layer and ``.claude/ctp.md`` for the
# schema invariants. The ledger is append-only: balance = SUM(amount_delta)
# over a user's CTPLedger rows, mirroring the StaffActionEntry pattern.


class CTPBoard(Model):
    """One reward board (DEV / TXT / ART / OPS / ...). ``enum`` is the short
    uppercase tag used everywhere in commands; ``board_number`` is the
    tasks.wynnvets.org board id (formatted into the URL on display);
    ``role_id`` is the optional Discord role that members of this board hold
    (used by admins to find eligible reward recipients out-of-band).
    """

    id = fields.UUIDField(pk=True)
    enum = fields.CharField(max_length=8, unique=True)
    board_number = fields.IntField()
    role_id: Optional[str] = fields.CharField(max_length=64, null=True)  # type: ignore
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "ctp_boards"


class CTPBoardMembership(Model):
    """A manual staff-assigned association between a Discord user and a CTP
    board, surfaced in ``~ctp status`` / ``~ctp info`` alongside the
    role-derived memberships. Carries no Discord side effect: assigning a
    user does NOT grant the board's ``role_id``, and revoking does NOT
    strip it. The two membership sources (role-held + manual) are unioned
    at read time by ``_board_memberships`` in ``cogs/rewards/ctp.py``.

    ``actor_disc_uuid`` is captured for forensic value (who assigned
    whom); no audit UI consumes it today.
    """

    id = fields.UUIDField(pk=True)
    discord_account: fields.ForeignKeyRelation[DiscordAccount] = fields.ForeignKeyField(
        "models.DiscordAccount", related_name="ctp_board_memberships", on_delete=fields.CASCADE
    )
    board: fields.ForeignKeyRelation[CTPBoard] = fields.ForeignKeyField(
        "models.CTPBoard", related_name="memberships", on_delete=fields.CASCADE
    )
    actor_disc_uuid = fields.CharField(max_length=255)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "ctp_board_memberships"
        unique_together = (("discord_account", "board"),)


class CTPPrize(Model):
    """An entry in the redeemable-prize catalog. ``(category, enum_name)``
    is the lookup key used in ``~ctp redeem`` / ``~ctp prize edit``.
    ``duration_seconds`` null = one-time / no expiry; a positive value is
    the active-access window. ``disabled`` (default False) hides the prize
    from ``~ctp prize info`` and refuses new redemptions, but ledger rows
    that snapshotted this prize survive unaffected.
    """

    id = fields.UUIDField(pk=True)
    category = fields.CharField(max_length=16)  # "Access" | "Change" | "Service" | "Gift"
    enum_name = fields.CharField(max_length=32)
    cost = fields.IntField()
    duration_seconds: Optional[int] = fields.IntField(null=True)  # type: ignore
    display = fields.CharField(max_length=255)
    disclaimer: Optional[str] = fields.TextField(null=True)  # type: ignore
    disabled = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "ctp_prizes"
        unique_together = (("category", "enum_name"),)


class CTPLedger(Model):
    """Append-only point ledger. Balance = SUM(amount_delta) per user.

    ``source`` discriminates rendering in ``~ctp history`` and is one of:
    ``reward`` | ``redeem`` | ``gift_sent`` | ``gift_received`` |
    ``glint_invest`` | ``admin_set``.

    For ``source='redeem'`` with a time-bound prize, ``expires_at`` is
    snapshotted at redemption (computed from prize.duration_seconds +
    created_at) so a later prize-duration edit doesn't retroactively move
    anyone's active window. ``prize_display_at_time`` /
    ``prize_category_at_time`` are also snapshotted so deleting or disabling
    a prize doesn't corrupt history. Gift creates two rows (sender -delta,
    receiver +delta) with matching counterparty_disc_uuids; glint
    investments leave a negative row here plus a positive increment on
    ``CTPGlintInvestment``.
    """

    id = fields.UUIDField(pk=True)
    discord_account: fields.ForeignKeyRelation[DiscordAccount] = fields.ForeignKeyField(
        "models.DiscordAccount", related_name="ctp_ledger", on_delete=fields.CASCADE
    )
    amount_delta = fields.IntField()
    source = fields.CharField(max_length=16, index=True)
    board: fields.ForeignKeyNullableRelation[CTPBoard] = fields.ForeignKeyField(
        "models.CTPBoard", related_name="ledger_entries", null=True, on_delete=fields.SET_NULL
    )
    task_number: Optional[int] = fields.IntField(null=True)  # type: ignore
    prize: fields.ForeignKeyNullableRelation[CTPPrize] = fields.ForeignKeyField(
        "models.CTPPrize", related_name="ledger_entries", null=True, on_delete=fields.SET_NULL
    )
    prize_display_at_time: Optional[str] = fields.CharField(max_length=255, null=True)  # type: ignore
    prize_category_at_time: Optional[str] = fields.CharField(max_length=16, null=True)  # type: ignore
    expires_at: Optional[datetime] = fields.DatetimeField(null=True)  # type: ignore
    counterparty_disc_uuid: Optional[str] = fields.CharField(max_length=255, null=True)  # type: ignore
    actor_disc_uuid = fields.CharField(max_length=255)
    comment: Optional[str] = fields.TextField(null=True)  # type: ignore
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "ctp_ledger"


class CTPGlintInvestment(Model):
    """Cumulative-only invested-points total per user. The spec is explicit:
    'this number can only ever increase' (chore_torn_palace.md line 92).
    Stored separately from the ledger so a single SELECT answers the
    leaderboard query; the matching negative ledger row
    (``source='glint_invest'``) is what debits the user's balance.
    """

    id = fields.UUIDField(pk=True)
    discord_account: fields.OneToOneRelation[DiscordAccount] = fields.OneToOneField(
        "models.DiscordAccount", related_name="ctp_glint_investment", on_delete=fields.CASCADE
    )
    total_invested = fields.IntField(default=0)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "ctp_glint_investments"


def _inspect_db_state(db_path: str) -> tuple[bool, set[str]]:
    """Return ``(file_exists, table_names)`` for the SQLite file at ``db_path``.

    Raises ``RuntimeError`` if the file exists but can't be opened (corruption,
    permissions, etc.) — we explicitly *don't* swallow that into "fresh DB",
    because doing so would silently re-create the schema and wipe data on a
    transient read failure. Caller decides what to do based on the returned
    tuple. On 2026-05-23 the prior, error-swallowing version of this function
    contributed to a total prod data wipe — see ``.claude/data_model.md``.
    """
    import sqlite3

    if not os.path.isfile(db_path):
        return False, set()
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            names = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise RuntimeError(
            f"Failed to inspect SQLite file at {db_path}: {e!r}. Refusing to "
            "continue — letting init_db proceed would treat this as a fresh "
            "DB and run all migrations from scratch, wiping any data the "
            "file actually contained."
        ) from e
    return True, names


def _existing_db_needs_fake_apply(db_path: str) -> bool:
    """Legacy boolean shim over :func:`_inspect_db_state` retained for tests
    that assert the per-DB-state decision directly. Production code in
    :func:`init_db` uses :func:`_inspect_db_state` so it can distinguish
    "missing file" from "empty file" from "pre-aerich DB" and refuse the
    silent-wipe path.
    """
    file_exists, names = _inspect_db_state(db_path)
    return file_exists and bool(names - {"aerich"}) and "aerich" not in names


def _create_aerich_tracking_table(db_path: str) -> None:
    """Create the ``aerich`` migration-tracking table directly via sqlite3.

    Only invoked on the pre-aerich-DB bootstrap path: the initial migration's
    ``upgrade()`` would create this table as a side effect, but we're about
    to ``upgrade(fake=True)`` which skips DDL — and ``Aerich.create`` then
    tries to INSERT into a table that doesn't exist. So we provision it
    once, by hand, with the same shape ``aerich.models.Aerich`` declares.

    Idempotent: ``CREATE TABLE IF NOT EXISTS``. Safe to call on any DB.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS "aerich" ('
            '"id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, '
            '"version" VARCHAR(255) NOT NULL, '
            '"app" VARCHAR(100) NOT NULL, '
            '"content" JSON NOT NULL)'
        )
        conn.commit()
    finally:
        conn.close()


def _backup_db_before_migration(db_path: str) -> str | None:
    """Copy the existing DB to a timestamped sibling before any migration
    runs. Returns the backup path (or ``None`` if there's nothing to back
    up). Caller logs the path so an operator can find it after a wipe.

    Added 2026-05-23 after a deploy that silently wiped the prod DB —
    schema-touching code now leaves a side-channel backup so recovery is a
    file copy rather than a forensic exercise.
    """
    import shutil
    import time

    if not os.path.isfile(db_path):
        return None
    backup_path = f"{db_path}.pre-migration-{int(time.time())}"
    shutil.copy2(db_path, backup_path)
    wal = db_path + "-wal"
    if os.path.isfile(wal):
        shutil.copy2(wal, backup_path + "-wal")
    return backup_path


# Names of every model table the bot expects (kept here, in orm.py, so
# init_db can sanity-check the on-disk schema without running model queries).
# Update this list whenever a table is added or removed.
_EXPECTED_MODEL_TABLES = frozenset({
    "apartments", "blocklist", "bot_config_overrides", "build_promotions",
    "ctp_board_memberships", "ctp_boards", "ctp_glint_investments",
    "ctp_ledger", "ctp_prizes",
    "cult_memberships", "cults", "dead_guild_alerts", "discord_accounts",
    "dm_sent_log", "first_install_monitors", "guild_capacity_alerts",
    "intercult_messages", "janitor_alerts", "link_code", "link_requests",
    "minecraft_accounts", "minecraft_alts", "mojang_name_cache",
    "profession_categories", "recruitment_queries", "return_guesses",
    "scores", "shouts", "staff_action_entries", "story_segments",
    "user_vanity_choices", "verify_keys", "waitlist", "weekly_events",
})


async def _fake_apply_initial_migration(app: str) -> None:
    """Record the ``0_*_init.py`` migration as applied without running its
    DDL. Used in the pre-aerich-DB bootstrap path so subsequent migrations
    (which DO need to run, because they add schema not in the legacy
    ``generate_schemas`` output) execute for real.

    We can't use ``aerich.Command.upgrade(fake=True)`` here because that
    forwards ``fake=True`` to *every* pending migration, including the
    backfill migrations we explicitly want applied. Instead, pre-fill the
    tracking row for the initial migration only; the subsequent normal
    ``upgrade(fake=False)`` will skip it (already-applied) and run the rest.
    """
    from aerich.migrate import Migrate
    from aerich.models import Aerich
    from aerich.utils import decompress_dict, get_models_describe, import_py_module

    for vm in sorted(Migrate.get_all_version_modules(), key=lambda v: v.name):
        if not vm.name.startswith("0_"):
            continue
        m = import_py_module(vm)
        model_state_str = getattr(m, "MODELS_STATE", None)
        models_state = (
            decompress_dict(model_state_str) if model_state_str else get_models_describe(app)
        )
        await Aerich.create(version=vm.name + ".py", app=app, content=models_state)
        return
    raise RuntimeError("No initial 0_*_init.py migration found in migrations/models/")


async def init_db():
    """Initialise Tortoise and apply pending aerich migrations.

    Branches:

    * **Empty/fresh DB** (file absent, OR exists with no model tables and
      no ``aerich`` table) — refused unless ``DAZEBOT_ALLOW_FRESH_DB=1`` is
      set. A missing file at the prod path almost always means a broken
      bind mount / lost data rather than legitimate first-time setup; the
      flag is the operator stating "yes, I really mean a clean install."
      With the flag set, ``upgrade`` creates everything from scratch.
    * **Existing pre-aerich DB** (model tables present, ``aerich`` table
      absent) — back up the file, provision the tracking table, fake-apply
      ONLY the initial migration, then ``upgrade`` to apply any follow-up
      migrations for real. This is where backfill migrations (like
      ``1_*_backfill_fk_unique_indexes.py``) actually take effect on prod.
    * **Already-bootstrapped DB** (model tables + ``aerich`` table present)
      — back up the file, run ``upgrade`` for any pending follow-up
      migrations; no-op when up-to-date.

    The pre-migration backup ends up at ``{db_path}.pre-migration-{epoch}``
    so any deploy that goes wrong can be reversed with a file copy.

    ``aerich.Command.init`` calls ``Tortoise.init`` internally (guarded by
    ``Tortoise._inited``), so the connection it opens is the same one the
    bot uses for queries afterwards.
    """
    import logging

    from aerich import Command

    logger = logging.getLogger("dazebot.orm")

    db_path = _resolve_db_path()
    file_exists, table_names = _inspect_db_state(db_path)
    has_aerich = "aerich" in table_names
    model_tables_present = bool(table_names & _EXPECTED_MODEL_TABLES)

    if not file_exists or (not model_tables_present and not has_aerich):
        if os.environ.get("DAZEBOT_ALLOW_FRESH_DB") != "1":
            raise RuntimeError(
                f"No existing dazebot DB at {db_path!r} "
                f"(file_exists={file_exists}, model_tables_present={model_tables_present}). "
                "Refusing to create a fresh DB and lose any data that may "
                "have been at this path. If this is genuinely first-time "
                "setup, set DAZEBOT_ALLOW_FRESH_DB=1 and restart."
            )
        logger.warning(
            "DAZEBOT_ALLOW_FRESH_DB=1 set; creating fresh DB at %s", db_path
        )
        needs_fake_apply = False
    elif model_tables_present and not has_aerich:
        needs_fake_apply = True
    else:
        needs_fake_apply = False

    backup_path = _backup_db_before_migration(db_path)
    if backup_path is not None:
        logger.info("Backed up pre-migration DB to %s", backup_path)

    if needs_fake_apply:
        _create_aerich_tracking_table(db_path)

    command = Command(tortoise_config=_build_tortoise_config(db_path), app="models", location="./migrations")
    await command.init()
    if needs_fake_apply:
        await _fake_apply_initial_migration(command.app)
    applied = await command.upgrade(fake=False)
    if applied:
        logger.info("Applied aerich migration(s): %s", applied)


async def close_db():
    from tortoise import Tortoise

    await Tortoise.close_connections()

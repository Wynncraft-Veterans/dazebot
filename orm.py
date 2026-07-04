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


def _resolve_returns_db_path() -> str:
    """Resolve the returns SQLite path. Mirrors :func:`_resolve_db_path`;
    ``DAZEBOT_RETURNS_DB_PATH`` overrides both — set in tests to a tmp file.

    Returners-only tables (``return_guesses``, ``story_segments``) live in
    this separate file rather than in dazebot.db. See ``orm_returns.py``.
    """
    p = os.environ.get("DAZEBOT_RETURNS_DB_PATH")
    if p is None:
        p = "/app/data/returns.db" if os.path.isdir("/app/data") else "returns.db"
    return p


def _build_tortoise_config(
    db_path: str | None = None, returns_db_path: str | None = None
) -> dict:
    """Build a tortoise config dict with two connections / two apps. Built
    fresh per call so test monkeypatching of ``DAZEBOT_DB_PATH`` and
    ``DAZEBOT_RETURNS_DB_PATH`` takes effect — the module-level
    ``TORTOISE_ORM`` below is for aerich CLI use and is frozen at import time.

    Aerich tracks BOTH apps' migration history in one ``aerich`` table in
    the ``default`` connection (it's registered with the first app only,
    not duplicated under ``returns``). Each app's *schema* still lands in
    its own connection's DB file — verified empirically; do not "fix" this
    by adding ``aerich.models`` to the returns app's models list.
    """
    path = db_path if db_path is not None else _resolve_db_path()
    returns_path = returns_db_path if returns_db_path is not None else _resolve_returns_db_path()
    return {
        "connections": {
            "default": f"sqlite://{path}",
            "returns": f"sqlite://{returns_path}",
        },
        "apps": {
            "models": {
                "models": ["orm", "aerich.models"],
                "default_connection": "default",
            },
            "returns": {
                "models": ["orm_returns"],
                "default_connection": "returns",
            },
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
    in-game guild staff against a Minecraft player.

    Live caution-point total for a target is *not* the simple SUM of
    ``points`` across their rows -- each individual point decays on the
    curve ``y(x) = 21 * (26/3)^((x-1)/4)`` days after the entry's
    ``created_at`` (the *x*-th live point being slower than the first).
    The total is computed by replaying the audit log; see
    ``lib/staff/staff_actions.py::_replay``. Three live points = formal
    warning DM; six = auto-ban. Expired rows stay in the table for the
    audit trail but contribute 0 to the live total.

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
    category = fields.CharField(max_length=16)  # admin-defined; titlecased on insert by `prizes.normalize_category`
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


class Donation(Model):
    """A single staff-recorded in-game donation to the guild.

    ``value_emeralds`` is the staff-assigned emerald-equivalent valuation,
    not necessarily liquid emerald currency: most donations are items,
    which is why ``image_urls_json`` is usually populated (screenshot
    evidence). Pure emerald-currency donations exist but are rare.

    PK is auto-increment ``IntField`` (not UUID) because ``~donations
    info <id>`` and ``~donations edit <id>`` are typed by humans —
    friendly trumps matching CTP's UUID convention.

    ``image_urls_json`` is a JSON list of Discord CDN URLs captured at
    record time. Discord re-signs URLs when the source message is
    re-fetched, so they remain viewable as long as the source message
    exists. Re-hosting to i.wynnvets.org is out of scope for v1.

    FK to ``MinecraftAccount`` uses ``RESTRICT`` — deleting an MC account
    must not silently orphan donation history.
    """

    id = fields.IntField(pk=True)
    recipient_mc: fields.ForeignKeyRelation[MinecraftAccount] = fields.ForeignKeyField(
        "models.MinecraftAccount", related_name="donations_received",
        on_delete=fields.RESTRICT,
    )
    value_emeralds = fields.BigIntField()
    comment: Optional[str] = fields.TextField(null=True)
    image_urls_json: Optional[str] = fields.TextField(null=True)
    recorder_disc_uuid = fields.CharField(max_length=255)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "donations"


class ShoppingRequest(Model):
    """A staff-tracked "we want to buy X" wishlist entry.

    Populated by ``~shopping request`` (staff). Closed automatically when
    ``qty_remaining`` reaches 0 via ``~shopping record`` fulfilments, or
    manually via ``~shopping close``. ``qty_remaining`` is denormalized
    on-row for hot-path reads (``~shopping list`` filters
    ``closed_at IS NULL``); the append-only ``ShoppingRequestAdjustment``
    log is the authoritative history of manual deltas.

    ``item_name_lower`` is the lookup key for ``~shopping record`` — kept
    alongside ``item_name`` to preserve display casing (which comes from
    Wynnventory's canonical form).
    """

    id = fields.IntField(pk=True)
    item_name = fields.CharField(max_length=255, index=True)
    item_name_lower = fields.CharField(max_length=255, index=True)
    unit_value_emeralds = fields.BigIntField()
    qty_initial = fields.IntField()
    qty_remaining = fields.IntField()
    requester_disc_uuid = fields.CharField(max_length=255)
    comment: Optional[str] = fields.TextField(null=True)  # type: ignore
    closed_at: Optional[datetime] = fields.DatetimeField(null=True)  # type: ignore
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "shopping_requests"


class ShoppingRequestAdjustment(Model):
    """Append-only log of manual qty deltas on a :class:`ShoppingRequest`.

    Every ``~shopping adjust`` and ``~shopping close`` inserts one row;
    nothing ever mutates or deletes them. The parent request's
    ``qty_remaining`` is bumped in the same transaction so hot reads stay
    a single-row lookup, but the true history is here.

    ``delta_qty`` is signed: ``+3`` bumps the wanted qty up, ``-2`` down.
    """

    id = fields.IntField(pk=True)
    request: fields.ForeignKeyRelation[ShoppingRequest] = fields.ForeignKeyField(
        "models.ShoppingRequest", related_name="adjustments",
        on_delete=fields.RESTRICT,
    )
    delta_qty = fields.IntField()
    reason: Optional[str] = fields.TextField(null=True)  # type: ignore
    actor_disc_uuid = fields.CharField(max_length=255)
    created_at = fields.DatetimeField(auto_now_add=True, index=True)

    class Meta:
        table = "shopping_request_adjustments"


class ShoppingDonation(Model):
    """A staff-recorded fulfilment against a :class:`ShoppingRequest`.

    Donations exceeding the parent request's ``qty_remaining`` are split:
    the fulfilling portion counts at 100% of ``unit_value_emeralds_at_time``,
    the excess counts at 20%. ``qty_at_full_value`` captures the split so
    the value math is transparent and re-derivable.

    Removes are soft: ``~shopping edit donation <id> void`` sets
    ``voided_at`` and restores ``qty_at_full_value`` to the parent
    request's ``qty_remaining`` (reopening the request if needed). All
    leaderboard/glint queries filter ``voided_at__isnull=True``. The row
    itself is preserved so the audit trail stays intact — this is a
    deliberate divergence from :class:`Donation` which supports hard
    delete via ``~donations edit remove``.

    FK to ``ShoppingRequest`` uses ``SET_NULL`` so admin-deleting a request
    (should that ever be supported) doesn't cascade-destroy donation
    history; ``unit_value_emeralds_at_time`` is snapshotted at insert so
    later edits to the parent request's unit value don't retroactively
    rewrite historical credit.
    """

    id = fields.IntField(pk=True)
    request: fields.ForeignKeyRelation[ShoppingRequest] = fields.ForeignKeyField(
        "models.ShoppingRequest", related_name="donations",
        null=True, on_delete=fields.SET_NULL,
    )
    recipient_mc: fields.ForeignKeyRelation[MinecraftAccount] = fields.ForeignKeyField(
        "models.MinecraftAccount", related_name="shopping_donations_received",
        on_delete=fields.RESTRICT,
    )
    qty = fields.IntField()
    unit_value_emeralds_at_time = fields.BigIntField()
    qty_at_full_value = fields.IntField()
    value_emeralds = fields.BigIntField(index=True)
    comment: Optional[str] = fields.TextField(null=True)  # type: ignore
    recorder_disc_uuid = fields.CharField(max_length=255)
    voided_at: Optional[datetime] = fields.DatetimeField(null=True, index=True)  # type: ignore
    voided_by_disc_uuid: Optional[str] = fields.CharField(max_length=255, null=True)  # type: ignore
    voided_reason: Optional[str] = fields.TextField(null=True)  # type: ignore
    created_at = fields.DatetimeField(auto_now_add=True, index=True)

    class Meta:
        table = "shopping_donations"


class BucketPull(Model):
    """A staff-awarded "pull" token in one of six prize buckets.

    Users redeem in-person with staff; this row is the bot's only record
    that they're owed a prize. Expiry is six months from award — pulls
    past expiry stop counting as "outstanding" but the row is preserved
    so ``~bucket info`` can still surface them for audits. Redemption
    sets ``redeemed_at`` / ``redeemed_by_disc_uuid`` rather than deleting
    the row, for the same reason.

    PK is auto-increment ``IntField`` (matches :class:`Donation`'s
    choice) so ``~bucket info`` can surface a short ``#42`` identifier
    that's friendlier to type than a UUID.
    """

    id = fields.IntField(pk=True)
    discord_account: fields.ForeignKeyRelation[DiscordAccount] = fields.ForeignKeyField(
        "models.DiscordAccount", related_name="bucket_pulls", on_delete=fields.CASCADE,
    )
    tier = fields.IntField(index=True)
    reason = fields.TextField()
    expires_at = fields.DatetimeField()
    redeemed_at: Optional[datetime] = fields.DatetimeField(null=True)  # type: ignore
    redeemed_by_disc_uuid: Optional[str] = fields.CharField(max_length=255, null=True)  # type: ignore
    actor_disc_uuid = fields.CharField(max_length=255)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "bucket_pulls"


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
    "apartments", "blocklist", "bot_config_overrides", "bucket_pulls",
    "build_promotions",
    "ctp_board_memberships", "ctp_boards", "ctp_glint_investments",
    "ctp_ledger", "ctp_prizes",
    "cult_memberships", "cults", "dead_guild_alerts", "discord_accounts",
    "dm_sent_log", "donations", "first_install_monitors", "guild_capacity_alerts",
    "intercult_messages", "janitor_alerts", "link_code", "link_requests",
    "minecraft_accounts", "minecraft_alts", "mojang_name_cache",
    "profession_categories", "recruitment_queries",
    "scores", "shopping_donations", "shopping_request_adjustments",
    "shopping_requests", "shouts", "staff_action_entries",
    "user_vanity_choices", "verify_keys", "waitlist", "weekly_events",
})

# Tables that USED to live in dazebot.db but have moved to returns.db. Their
# presence in dazebot.db means an old (pre-split) DB file — init_db will copy
# them to returns.db, then a follow-up migration drops them from here. Once
# the drop migration has run everywhere, this set can be removed.
_LEGACY_RETURNS_TABLES = frozenset({"return_guesses", "story_segments"})

# What the returns.db file should contain after a successful upgrade. The
# `aerich` tracking table lives in dazebot.db (default connection), not here.
_EXPECTED_RETURNS_TABLES = frozenset({"return_guesses", "story_segments"})


def _copy_returns_legacy_data(main_db_path: str, returns_db_path: str) -> None:
    """One-shot copy of legacy ``return_guesses`` and ``story_segments`` rows
    from the dazebot DB into the freshly-provisioned returns DB.

    Runs inside :func:`init_db` exactly once per deploy, gated on the legacy
    tables still being present in dazebot.db. Idempotent: if the destination
    rows are already populated, the copy is skipped with a warning.

    The translation is the consequential bit: the source schema FKs to
    ``discord_accounts.id`` (synthetic UUID PK); the destination schema in
    :mod:`orm_returns` stores the natural Discord snowflake ``disc_uuid``
    string directly (no cross-DB FK is enforceable in SQLite). The
    ``INSERT … SELECT`` joins through ``discord_accounts`` to perform that
    translation. We assert source count == join-result count before
    committing — a mismatch means an orphan row whose discord_account_id
    pointed at a row that no longer exists, and we'd rather fail loudly
    than silently drop history.

    All work happens in a single transaction on the main connection with the
    returns DB ATTACHed, so a crash mid-copy leaves both files untouched
    (SQLite's atomic-commit guarantee).
    """
    import logging
    import sqlite3

    log = logging.getLogger("dazebot.orm")
    conn = sqlite3.connect(main_db_path)
    try:
        conn.execute(f"ATTACH DATABASE '{returns_db_path}' AS returns_db")

        src_rg = conn.execute("SELECT COUNT(*) FROM return_guesses").fetchone()[0]
        src_ss = conn.execute("SELECT COUNT(*) FROM story_segments").fetchone()[0]
        dst_rg = conn.execute("SELECT COUNT(*) FROM returns_db.return_guesses").fetchone()[0]
        dst_ss = conn.execute("SELECT COUNT(*) FROM returns_db.story_segments").fetchone()[0]

        if dst_rg > 0 or dst_ss > 0:
            log.warning(
                "returns.db already populated (rg=%s ss=%s); skipping legacy copy",
                dst_rg, dst_ss,
            )
            return

        joined_rg = conn.execute(
            "SELECT COUNT(*) FROM return_guesses rg "
            "JOIN discord_accounts da ON da.id = rg.discord_account_id"
        ).fetchone()[0]
        joined_ss = conn.execute(
            "SELECT COUNT(*) FROM story_segments ss "
            "JOIN discord_accounts da ON da.id = ss.discord_account_id"
        ).fetchone()[0]
        if joined_rg != src_rg or joined_ss != src_ss:
            raise RuntimeError(
                f"Orphan rows detected in legacy returns tables: "
                f"return_guesses src={src_rg} after-join={joined_rg}, "
                f"story_segments src={src_ss} after-join={joined_ss}. "
                "Refusing to copy a partial set — investigate before retrying."
            )

        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT INTO returns_db.return_guesses "
                "(id, week, day, disc_uuid, price, created_at) "
                "SELECT rg.id, rg.week, rg.day, da.disc_uuid, rg.price, rg.created_at "
                "FROM return_guesses rg "
                "JOIN discord_accounts da ON da.id = rg.discord_account_id"
            )
            conn.execute(
                "INSERT INTO returns_db.story_segments "
                "(id, week, disc_uuid, content, created_at) "
                "SELECT ss.id, ss.week, da.disc_uuid, ss.content, ss.created_at "
                "FROM story_segments ss "
                "JOIN discord_accounts da ON da.id = ss.discord_account_id"
            )
            new_rg = conn.execute(
                "SELECT COUNT(*) FROM returns_db.return_guesses"
            ).fetchone()[0]
            new_ss = conn.execute(
                "SELECT COUNT(*) FROM returns_db.story_segments"
            ).fetchone()[0]
            if new_rg != src_rg or new_ss != src_ss:
                conn.execute("ROLLBACK")
                raise RuntimeError(
                    f"returns.db copy count mismatch: "
                    f"rg src={src_rg} dst={new_rg}, ss src={src_ss} dst={new_ss}. "
                    "Rolled back."
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        log.info(
            "Copied legacy returns data to %s: %s return_guesses, %s story_segments",
            returns_db_path, new_rg, new_ss,
        )
    finally:
        conn.close()


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
    """Initialise Tortoise and apply pending aerich migrations across BOTH
    the dazebot and returns SQLite databases.

    Branches (dazebot.db side, unchanged from the single-DB era):

    * **Empty/fresh DB** (file absent, OR exists with no model tables and
      no ``aerich`` table) — refused unless ``DAZEBOT_ALLOW_FRESH_DB=1`` is
      set. A missing file at the prod path almost always means a broken
      bind mount / lost data rather than legitimate first-time setup; the
      flag is the operator stating "yes, I really mean a clean install."
    * **Existing pre-aerich DB** (model tables present, ``aerich`` table
      absent) — back up the file, provision the tracking table, fake-apply
      ONLY the initial migration, then ``upgrade`` to apply any follow-up
      migrations for real.
    * **Already-bootstrapped DB** — back up the file, run ``upgrade`` for
      any pending follow-up migrations; no-op when up-to-date.

    Returns-DB side:

    * **Missing returns.db** is fine IF the legacy ``return_guesses``/
      ``story_segments`` tables are still in dazebot.db (this deploy will
      copy them across). Missing AND no legacy → require explicit
      ``DAZEBOT_ALLOW_FRESH_RETURNS_DB=1``.
    * **Legacy split gate** (``DAZEBOT_ALLOW_LEGACY_RETURNS_DROP=1``) — when
      dazebot.db still holds the legacy tables, the boot is about to
      (a) populate returns.db and (b) let the models-app drop migration
      nuke them from dazebot.db. The gate env var ensures the operator
      saw the pre-update dump land in ``/opt/docker/backups/db-dumps/dazebot/``
      first; ``manage update dazebot`` sets it after a successful dump.

    Boot order is precise:
      backup both files →
      run returns-app aerich (creates returns.db schema if first time) →
      copy legacy data (if legacy_present) →
      run models-app aerich (the drop migration runs HERE, after the copy
      has been verified — so a copy failure aborts before the drop).

    Pre-migration backups land at ``{db_path}.pre-migration-{epoch}`` so a
    bad deploy can be reversed with a file copy.
    """
    import logging

    from aerich import Command

    logger = logging.getLogger("dazebot.orm")

    db_path = _resolve_db_path()
    returns_path = _resolve_returns_db_path()

    main_exists, main_tables = _inspect_db_state(db_path)
    returns_exists, returns_tables = _inspect_db_state(returns_path)

    main_has_aerich = "aerich" in main_tables
    main_models_present = bool(main_tables & _EXPECTED_MODEL_TABLES)
    legacy_present = bool(main_tables & _LEGACY_RETURNS_TABLES)
    returns_present = bool(returns_tables & _EXPECTED_RETURNS_TABLES)

    # --- Fresh-DB guard: dazebot.db ---
    if not main_exists or (not main_models_present and not main_has_aerich):
        if os.environ.get("DAZEBOT_ALLOW_FRESH_DB") != "1":
            raise RuntimeError(
                f"No existing dazebot DB at {db_path!r} "
                f"(file_exists={main_exists}, model_tables_present={main_models_present}). "
                "Refusing to create a fresh DB and lose any data that may "
                "have been at this path. If this is genuinely first-time "
                "setup, set DAZEBOT_ALLOW_FRESH_DB=1 and restart."
            )
        logger.warning("DAZEBOT_ALLOW_FRESH_DB=1 set; creating fresh dazebot DB at %s", db_path)
        needs_fake_apply_main = False
    elif main_models_present and not main_has_aerich:
        needs_fake_apply_main = True
    else:
        needs_fake_apply_main = False

    # --- Fresh-DB guard: returns.db ---
    # Missing returns.db is OK if legacy tables in dazebot.db will be copied
    # below. Otherwise require an explicit opt-in.
    if not returns_exists and not legacy_present and not returns_present:
        if os.environ.get("DAZEBOT_ALLOW_FRESH_RETURNS_DB") != "1":
            raise RuntimeError(
                f"No existing returns DB at {returns_path!r} and no legacy "
                "return_guesses/story_segments tables in dazebot.db to copy. "
                "Set DAZEBOT_ALLOW_FRESH_RETURNS_DB=1 to start with an empty "
                "returns.db."
            )
        logger.warning(
            "DAZEBOT_ALLOW_FRESH_RETURNS_DB=1 set; creating fresh returns DB at %s",
            returns_path,
        )

    # --- Belt-and-suspenders backups (both files) ---
    main_backup = _backup_db_before_migration(db_path)
    if main_backup is not None:
        logger.info("Backed up pre-migration dazebot DB to %s", main_backup)
    returns_backup = _backup_db_before_migration(returns_path)
    if returns_backup is not None:
        logger.info("Backed up pre-migration returns DB to %s", returns_backup)

    # --- One-shot split gate ---
    # This boot is about to copy the legacy tables to returns.db, then a
    # pending models-app migration will drop them from dazebot.db. Refuse
    # unless the deploy procedure has confirmed a fresh dump exists in
    # /opt/docker/backups/db-dumps/dazebot/ — `manage update dazebot` sets
    # this env var for the new container exactly once, after a successful
    # pre-update dump.
    if legacy_present:
        if os.environ.get("DAZEBOT_ALLOW_LEGACY_RETURNS_DROP") != "1":
            raise RuntimeError(
                f"dazebot.db at {db_path!r} still holds legacy returns tables "
                f"({sorted(main_tables & _LEGACY_RETURNS_TABLES)}). The next "
                "boot will copy them to returns.db and the pending drop "
                "migration will remove them from here. Refusing to proceed "
                "without DAZEBOT_ALLOW_LEGACY_RETURNS_DROP=1. Run "
                "`manage dump dazebot` to land a backup in "
                "/opt/docker/backups/db-dumps/dazebot/, then "
                "`manage update dazebot` (which sets the env var for one boot)."
            )

    # Provision the aerich table before EITHER app's upgrade runs — the
    # returns-app upgrade also writes into this table (it lives in the
    # default connection, shared across both apps). On a fresh dazebot.db
    # the table doesn't exist yet; on the pre-aerich-DB bootstrap path we
    # also need it before _fake_apply_initial_migration writes its row.
    # CREATE TABLE IF NOT EXISTS makes this safe to call unconditionally.
    _create_aerich_tracking_table(db_path)

    cfg = _build_tortoise_config(db_path, returns_path)

    # --- Returns app FIRST: ensures returns.db schema exists before copy ---
    returns_cmd = Command(tortoise_config=cfg, app="returns", location="./migrations")
    await returns_cmd.init()
    applied_returns = await returns_cmd.upgrade(fake=False)
    if applied_returns:
        logger.info("Applied returns-app migration(s): %s", applied_returns)

    # --- One-shot copy: legacy dazebot.db rows -> returns.db ---
    if legacy_present:
        _copy_returns_legacy_data(db_path, returns_path)

    # --- Models app: existing flow (drop migration runs HERE if pending) ---
    models_cmd = Command(tortoise_config=cfg, app="models", location="./migrations")
    await models_cmd.init()
    if needs_fake_apply_main:
        await _fake_apply_initial_migration(models_cmd.app)
    applied_models = await models_cmd.upgrade(fake=False)
    if applied_models:
        logger.info("Applied models-app migration(s): %s", applied_models)


async def close_db():
    from tortoise import Tortoise

    await Tortoise.close_connections()

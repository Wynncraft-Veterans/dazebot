from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from tortoise import fields
from tortoise.models import Model

from lib.util import ProfCategory
from lib.mc.wynn_api.player_models import CharacterProfessionsType

import uuid


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
    minecraft_account: fields.ForeignKeyNullableRelation[MinecraftAccount] = fields.ForeignKeyField(
        "models.MinecraftAccount", related_name="discord_account", null=True, on_delete=fields.SET_NULL, unique=True
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
    GuildCapacityAlert exactly. Created automatically by
    ``Tortoise.generate_schemas()`` in ``init_db`` — no migration."""

    id = fields.UUIDField(pk=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "janitor_alerts"


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
    minecraft_account: fields.ForeignKeyRelation[MinecraftAccount] = fields.ForeignKeyField(
        "models.MinecraftAccount", related_name="waitlist", on_delete=fields.CASCADE, unique=True
    )
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "waitlist"


class Blocklist(Model):
    """Users on this list are forced to the Registered role and can never become
    Honourary / Hiatus / Member / Waitlisted. See .claude/membership_spec.md §2b.
    """

    id = fields.UUIDField(pk=True)
    minecraft_account: fields.ForeignKeyRelation[MinecraftAccount] = fields.ForeignKeyField(
        "models.MinecraftAccount", related_name="blocklist", on_delete=fields.CASCADE, unique=True
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
    for membership sync and by ``lib.discord_utils.intercult_view`` for
    cross-cult messaging. Nullable so the column can be added by the
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
    DiscordAccount, enforced by ``unique=True`` on ``discord_account``.
    """

    id = fields.UUIDField(pk=True)
    cult: fields.ForeignKeyRelation[Cult] = fields.ForeignKeyField(
        "models.Cult", related_name="memberships", on_delete=fields.CASCADE
    )
    discord_account: fields.ForeignKeyRelation[DiscordAccount] = fields.ForeignKeyField(
        "models.DiscordAccount", related_name="cult_membership", on_delete=fields.CASCADE, unique=True
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


# Database initialization helper
async def init_db():
    """Initialise the Tortoise ORM connection.

    The DB path defaults to the mounted ``/app/data`` volume in the docker
    deployment so the file survives container recreation
    (``manage update dazebot``); falls back to a working-dir file for local
    development. Override with ``DAZEBOT_DB_PATH`` if needed.
    """
    import os
    from tortoise import Tortoise

    db_path = os.environ.get("DAZEBOT_DB_PATH")
    if db_path is None:
        db_path = "/app/data/dazebot.db" if os.path.isdir("/app/data") else "dazebot.db"

    db_url = f"sqlite://{db_path}"
    await Tortoise.init(db_url=db_url, modules={"models": ["orm"]})
    await Tortoise.generate_schemas()
    await _ensure_added_columns()


async def _ensure_added_columns() -> None:
    """Apply schema additions that ``generate_schemas(safe=True)`` skips.

    ``Tortoise.generate_schemas`` only ever emits ``CREATE TABLE IF NOT
    EXISTS``. New *columns* on existing tables therefore need an explicit
    ``ALTER TABLE`` (see ``.claude/data_model.md``). This helper performs
    those alters idempotently — it inspects ``PRAGMA table_info`` and only
    emits ``ALTER TABLE ... ADD COLUMN`` if the column is missing. SQLite's
    ``ADD COLUMN`` is fast (metadata-only) and safe under WAL.

    Add a new entry below whenever you add a nullable column to an existing
    model. Removing a column still requires a manual one-shot.
    """
    from tortoise import Tortoise

    # (table, column, sql_type) — sql_type must match what Tortoise would
    # have generated for the field.
    added_columns: list[tuple[str, str, str]] = [
        ("cults", "thread_id", "BIGINT"),
    ]

    conn = Tortoise.get_connection("default")
    for table, column, sql_type in added_columns:
        info = await conn.execute_query_dict(f"PRAGMA table_info({table})")
        existing = {row["name"] for row in info}
        if column in existing:
            continue
        await conn.execute_script(
            f"ALTER TABLE {table} ADD COLUMN {column} {sql_type};"
        )


# Close connections helper
async def close_db():
    from tortoise import Tortoise

    await Tortoise.close_connections()

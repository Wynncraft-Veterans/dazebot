from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from tortoise import fields
from tortoise.models import Model

from lib.lib import ProfCategory
from lib.wynn_api.player_models import CharacterProfessionsType

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
    Honourary / Hiatus / Member / Waitlisted. See instructions1.md §2b.
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
    See instructions1.md §2g.
    """

    id = fields.UUIDField(pk=True)
    disc_uuid = fields.CharField(max_length=255, unique=True)
    role_id = fields.CharField(max_length=255)
    chosen_by_staff = fields.BooleanField(default=False)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "user_vanity_choices"


class MojangNameCache(Model):
    """Persistent cache of Mojang uuid -> username lookups (instructions1.md §5).
    Refresh after `MAX_AGE` to handle name changes.
    """

    uuid = fields.CharField(pk=True, max_length=36)
    username = fields.CharField(max_length=255)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "mojang_name_cache"


class BotConfigOverride(Model):
    """Persisted overrides for Config attributes set via /config admin command.
    See instructions1.md §3.
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
    in-game username or UUID at creation time.
    """

    id = fields.UUIDField(pk=True)
    # Stored lowercased so unique=True doubles as a case-insensitive guard.
    name = fields.CharField(max_length=64, unique=True)
    owner: fields.ForeignKeyRelation[MinecraftAccount] = fields.ForeignKeyField(
        "models.MinecraftAccount", related_name="owned_cults", on_delete=fields.RESTRICT
    )
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


# Close connections helper
async def close_db():
    from tortoise import Tortoise

    await Tortoise.close_connections()

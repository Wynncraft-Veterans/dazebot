from typing import Optional

from tortoise import fields
from tortoise.models import Model

from lib.lib import ProfCategory
from lib.wynn_api.player_models import CharacterProfessionsType


class Person(Model):
    id = fields.UUIDField(pk=True)
    name = fields.CharField(max_length=255, null=True)

    class Meta:
        table = "person"


class MinecraftAccount(Model):
    id = fields.UUIDField(pk=True)
    person = fields.ForeignKeyField(
        "models.Person", related_name="minecraft_accounts", null=True, on_delete=fields.SET_NULL
    )
    uuid = fields.CharField(max_length=36, unique=True)
    guild: Optional[str] = fields.CharField(max_length=255, null=True, blank=True)  # type: ignore
    wynn_username = fields.CharField(max_length=255)
    mc_username = fields.CharField(max_length=255)
    last_online = fields.DatetimeField(use_tz=True)
    last_manual_check = fields.DatetimeField(use_tz=True)

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
    person = fields.ForeignKeyField(
        "models.Person", related_name="discord_accounts", null=True, on_delete=fields.SET_NULL
    )
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


# Database initialization helper
async def init_db():
    from tortoise import Tortoise

    await Tortoise.init(db_url="sqlite://dev.db", modules={"models": ["orm"]})
    await Tortoise.generate_schemas()


# Close connections helper
async def close_db():
    from tortoise import Tortoise

    await Tortoise.close_connections()

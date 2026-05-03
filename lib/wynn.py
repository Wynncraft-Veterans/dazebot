from datetime import datetime, timezone

from lib import mc
from lib.lib import ProfCategory
from lib.wynn_api.player import get_player_full_stats
from lib.wynn_api.player_models import WynncraftPlayer
from orm import MinecraftAccount, ProfessionCategories


async def check_player_full(uuid: str) -> tuple[str, WynncraftPlayer, MinecraftAccount]:
    mc_username = await mc.get_mc_username(uuid)
    player = await get_player_full_stats(uuid)
    account = await MinecraftAccount.get(uuid=player.uuid)
    # if player.guild is None:
    #     account.guild = None
    # Wynncraft privacy: lastJoin/firstJoin can be None. Skip update in that case.
    if player.lastJoin is not None and account.last_online < player.lastJoin:
        account.last_online = player.lastJoin
    if player.firstJoin is not None:
        account.first_join = player.firstJoin
    account.wynn_username = player.username
    account.mc_username = mc_username
    account.last_manual_check = datetime.now(timezone.utc)
    await account.save()
    # if guild_name is not None:
    #     await self._check_guild(guild_name)

    profs = {}
    if player.characters:
        for char in player.characters.values():
            for prof_type, prof in char.professions.items():
                profs[prof_type] = max(profs.setdefault(prof_type, 0), prof.level)

    for prof_type, level in profs.items():
        _prof, _created = await ProfessionCategories.update_or_create(
            minecraft_account=account,
            prof_type=prof_type,
            defaults={
                "category": ProfCategory(["pleb", "void", "dernic"][(level >= 100) + (level >= 103)]),
            },
        )

    return mc_username, player, account

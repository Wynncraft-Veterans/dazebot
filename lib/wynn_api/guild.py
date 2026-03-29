from lib.wynn_api.requestor import Requestor
from lib.wynn_api.guild_models import Guild

requestor = Requestor()


async def get_guild(guild_name: str) -> Guild:
    response = await requestor.get(f"https://api.wynncraft.com/v3/guild/{guild_name}?identifier=username")
    data = await response.json()
    if isinstance(data, list):
        guild = next(g for d in data if (g := Guild(**d)).name == guild_name)
    else:
        guild = Guild(**data)
    return guild

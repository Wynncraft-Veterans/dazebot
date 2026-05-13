"""Wynncraft v3 guild-endpoint wrapper."""

from lib.mc.wynn_api.errors import raise_for_error_envelope
from lib.mc.wynn_api.guild_models import Guild
from lib.mc.wynn_api.requestor import Requestor

requestor = Requestor()


async def get_guild(guild_name: str) -> Guild:
    url = f"https://api.wynncraft.com/v3/guild/{guild_name}?identifier=username"
    response = await requestor.get(url)
    data = await response.json()
    raise_for_error_envelope(data, url=url)
    if isinstance(data, list):
        guild = next(g for d in data if (g := Guild(**d)).name == guild_name)
    else:
        guild = Guild(**data)
    return guild

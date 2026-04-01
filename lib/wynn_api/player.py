from lib.wynn_api.requestor import Requestor
from lib.wynn_api.player_models import WynncraftPlayer

requestor = Requestor()


async def get_player_main_stats(username_or_uuid: str, *, important=False) -> WynncraftPlayer:
    if important:
        print("important!")
        response = await requestor.get0(f"https://api.wynncraft.com/v3/player/{username_or_uuid}")
    else:
        response = await requestor.get(f"https://api.wynncraft.com/v3/player/{username_or_uuid}")
    data = await response.json()
    if isinstance(data, list):
        player = next(
            wp for d in data if (wp := WynncraftPlayer(**d)).username == username_or_uuid or wp.uuid == username_or_uuid
        )
    else:
        player = WynncraftPlayer(**data)
    return player


async def get_player_full_stats(username_or_uuid: str):
    response = await requestor.get(f"https://api.wynncraft.com/v3/player/{username_or_uuid}?fullResult")
    data = await response.json()
    if isinstance(data, list):
        player = next(
            wp for d in data if (wp := WynncraftPlayer(**d)).username == username_or_uuid or wp.uuid == username_or_uuid
        )
    else:
        player = WynncraftPlayer(**data)
    return player

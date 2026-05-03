from lib.wynn_api.errors import raise_for_error_envelope
from lib.wynn_api.requestor import Requestor
from lib.wynn_api.player_models import WynncraftPlayer

requestor = Requestor()


async def get_player_main_stats(username_or_uuid: str, *, important=False) -> WynncraftPlayer:
    url = f"https://api.wynncraft.com/v3/player/{username_or_uuid}"
    if important:
        print("important!")
        response = await requestor.get0(url)
    else:
        response = await requestor.get(url)
    data = await response.json()
    raise_for_error_envelope(data, url=url)
    if isinstance(data, list):
        player = next(
            wp for d in data if (wp := WynncraftPlayer(**d)).username == username_or_uuid or wp.uuid == username_or_uuid
        )
    else:
        player = WynncraftPlayer(**data)
    return player


async def get_player_full_stats(username_or_uuid: str):
    url = f"https://api.wynncraft.com/v3/player/{username_or_uuid}?fullResult"
    response = await requestor.get(url)
    data = await response.json()
    raise_for_error_envelope(data, url=url)
    if isinstance(data, list):
        player = next(
            wp for d in data if (wp := WynncraftPlayer(**d)).username == username_or_uuid or wp.uuid == username_or_uuid
        )
    else:
        player = WynncraftPlayer(**data)
    return player

"""Wynncraft v3 player-endpoint wrappers."""

from lib.mc.wynn_api.errors import raise_for_error_envelope
from lib.mc.wynn_api.player_models import WynncraftPlayer
from lib.mc.wynn_api.requestor import Requestor

requestor = Requestor()


async def get_player_stats(
    username_or_uuid: str,
    *,
    full: bool = False,
    important: bool = False,
    background: bool = False,
) -> WynncraftPlayer:
    """Fetch a Wynncraft player envelope and parse it.

    ``full=True`` requests ``?fullResult`` (per-character + profession data);
    ``False`` is the cheaper "main stats" shape. The response Pydantic
    model is the same union -- callers that need the heavy fields just
    pass ``full=True``.

    ``important=True`` pushes the request to the front of the singleton
    Requestor queue; reserved for hot-path lookups (e.g. server-watcher
    join probes) that race against the background activity loop.

    ``background=True`` enqueues at the lowest priority tier; the request
    only drains when no normal- or priority-tier calls are pending. Used
    by opportunistic scope extensions (e.g. hiatus-user server-transition
    inference) that must yield to the existing Unknown-bucket scope.
    ``important`` and ``background`` are mutually exclusive.
    """
    if important and background:
        raise ValueError("important and background are mutually exclusive")
    suffix = "?fullResult" if full else ""
    url = f"https://api.wynncraft.com/v3/player/{username_or_uuid}{suffix}"
    if important:
        response = await requestor.get0(url)
    elif background:
        response = await requestor.get_background(url)
    else:
        response = await requestor.get(url)
    data = await response.json()
    raise_for_error_envelope(data, url=url)
    if isinstance(data, list):
        player = next(
            wp
            for d in data
            if (wp := WynncraftPlayer(**d)).username == username_or_uuid
            or wp.uuid == username_or_uuid
        )
    else:
        player = WynncraftPlayer(**data)
    return player

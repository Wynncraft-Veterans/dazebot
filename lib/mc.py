import asyncio
import logging

import aiohttp

logger = logging.getLogger("dazebot.lib.mc")

_session: aiohttp.ClientSession | None = None


async def get_session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def get_mc_username(uuid: str) -> str:
    session = await get_session()
    async with session.get(f"https://api.ashcon.app/mojang/v2/user/{uuid}") as res:
        data = await res.json()
        if "username" not in data:
            logger.error(f"For some reason `username` was not in data: {data=}")
            await asyncio.sleep(1)
            return await get_mc_username((uuid))
        return data["username"]


async def unload():
    global _session
    if _session and not _session.closed:
        await _session.close()

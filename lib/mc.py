"""Mojang username lookup helpers with persistent caching and rate-limit
fallbacks.

Provider order (per ``.vscode/instructions1.md`` \u00a75):
    1. Local DB cache (uuid -> username, refreshed after CACHE_MAX_AGE)
    2. ashcon (``api.ashcon.app``) \u2014 generally the kindest upstream
    3. mcuuid.net \u2014 fallback, lenient rate limits
    4. Mojang's official ``api.minecraftservices.com/minecraft/profile/lookup``
       \u2014 absolute last resort

Every successful upstream hit is written through to the cache.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

logger = logging.getLogger("dazebot.lib.mc")

_session: aiohttp.ClientSession | None = None
_lookup_lock: dict[str, asyncio.Lock] = {}

# Cache freshness window before we re-query upstream. Players can change names,
# but not very often; one week balances freshness vs. rate limits.
CACHE_MAX_AGE = timedelta(days=7)


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def _normalize_uuid(uuid: str) -> str:
    return uuid.replace("-", "").lower()


async def _try_ashcon(uuid: str) -> str | None:
    session = await get_session()
    try:
        async with session.get(
            f"https://api.ashcon.app/mojang/v2/user/{uuid}", timeout=aiohttp.ClientTimeout(total=10)
        ) as res:
            if res.status != 200:
                logger.debug(f"ashcon returned {res.status} for {uuid}")
                return None
            data = await res.json()
            return data.get("username")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"ashcon lookup failed for {uuid}: {e}")
        return None


async def _try_mcuuid(uuid: str) -> str | None:
    session = await get_session()
    try:
        async with session.get(
            f"https://mcuuid.net/?q={uuid}&fmt=json",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as res:
            if res.status != 200:
                logger.debug(f"mcuuid returned {res.status} for {uuid}")
                return None
            data = await res.json(content_type=None)
            return data.get("name") or data.get("username")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"mcuuid lookup failed for {uuid}: {e}")
        return None


async def _try_mojang(uuid: str) -> str | None:
    """Absolute last resort. Mojang's /profile/lookup expects the uuid in the
    URL path (no dashes) and returns the current name in the response.
    """
    session = await get_session()
    normalized = _normalize_uuid(uuid)
    try:
        async with session.get(
            f"https://api.minecraftservices.com/minecraft/profile/lookup/{normalized}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as res:
            if res.status != 200:
                logger.warning(f"Mojang official returned {res.status} for {uuid}")
                return None
            data = await res.json()
            return data.get("name")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"Mojang official lookup failed for {uuid}: {e}")
        return None


async def get_mc_username(uuid: str, *, force_refresh: bool = False) -> str:
    """Return the current Minecraft username for ``uuid``.

    Will block on the cache lookup, then fall through providers in priority
    order. Raises ``RuntimeError`` if every provider fails AND no cached value
    exists.
    """
    from orm import MojangNameCache  # local import: ORM may not be initialised at import time

    # Per-uuid lock so concurrent callers don't all hammer upstream.
    lock = _lookup_lock.setdefault(uuid, asyncio.Lock())
    async with lock:
        cached = await MojangNameCache.filter(uuid=uuid).first()
        if cached and not force_refresh:
            age = datetime.now(timezone.utc) - cached.updated_at
            if age < CACHE_MAX_AGE:
                return cached.username

        for provider, fn in (("ashcon", _try_ashcon), ("mcuuid", _try_mcuuid), ("mojang", _try_mojang)):
            name = await fn(uuid)
            if name:
                await MojangNameCache.update_or_create(uuid=uuid, defaults={"username": name})
                logger.debug(f"Resolved {uuid} -> {name} via {provider}")
                return name

        if cached:
            logger.warning(f"All upstream Mojang providers failed for {uuid}; serving stale cached value")
            return cached.username

        raise RuntimeError(f"All Mojang providers failed and no cache available for uuid {uuid}")


async def unload():
    global _session
    if _session and not _session.closed:
        await _session.close()

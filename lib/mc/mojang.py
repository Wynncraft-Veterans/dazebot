"""Mojang username lookup helpers with persistent caching and rate-limit
fallbacks.

Provider order:
    1. Local DB cache (uuid -> username, refreshed after CACHE_MAX_AGE)
    2. Mojang's official ``api.minecraftservices.com/minecraft/profile/lookup``
       — the source of truth for current usernames.
    3. PlayerDB (``playerdb.co``) — Nodecraft-operated, no rate limits, JSON.
       This is the official replacement for the (now defunct) mcuuid.net JSON
       endpoint; mcuuid.net itself explicitly redirects API users to PlayerDB.
    4. ashcon (``api.ashcon.app``) — last-resort fallback. NOTE: ashcon's
       data is occasionally stale and has been observed to return *legacy*
       names instead of current ones (see incident with WrittenInWater /
       ShiningValiant on 2026-05-04). Use only when the above are unreachable.

Every successful upstream hit is written through to the cache.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

logger = logging.getLogger("dazebot.lib.mc.mojang")

_session: aiohttp.ClientSession | None = None
_lookup_lock: dict[str, asyncio.Lock] = {}

# PlayerDB asks API consumers to send an identifying user-agent so they can
# reach out if our usage causes problems. Sent on every outbound request from
# this module.
_USER_AGENT = "dazebot/1.0 (+https://wynnvets.org/discord)"

# Cache freshness window before we re-query upstream. Players can change names,
# but not very often; one week balances freshness vs. rate limits.
CACHE_MAX_AGE = timedelta(days=7)


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(headers={"User-Agent": _USER_AGENT})
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


async def _try_playerdb(uuid: str) -> str | None:
    """PlayerDB.co — Nodecraft's JSON Mojang lookup. Returns ``data.player.username``
    on success.
    """
    session = await get_session()
    try:
        async with session.get(
            f"https://playerdb.co/api/player/minecraft/{uuid}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as res:
            if res.status != 200:
                logger.debug(f"playerdb returned {res.status} for {uuid}")
                return None
            data = await res.json(content_type=None)
            if not data.get("success"):
                logger.debug(f"playerdb returned success=false for {uuid}: {data.get('code')}")
                return None
            return (data.get("data") or {}).get("player", {}).get("username")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"playerdb lookup failed for {uuid}: {e}")
        return None


async def _try_mojang(uuid: str) -> str | None:
    """Mojang's /profile/lookup expects the uuid in the URL path (no dashes)
    and returns the current name in the response. This is the source of truth.
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


_HEX_CHARS = frozenset("0123456789abcdef")


def _to_dashed_uuid(value: str | None) -> str | None:
    """Normalise an undashed (32-char) or dashed (36-char) UUID string to the
    canonical dashed lowercase form. Returns ``None`` if not a valid UUID.
    """
    if not value:
        return None
    cleaned = value.replace("-", "").lower()
    if len(cleaned) != 32 or not all(c in _HEX_CHARS for c in cleaned):
        return None
    return f"{cleaned[0:8]}-{cleaned[8:12]}-{cleaned[12:16]}-{cleaned[16:20]}-{cleaned[20:32]}"


def _looks_like_uuid(value: str) -> bool:
    """Return ``True`` if ``value`` looks UUID-shaped (so we can skip
    name-based upstream calls that would 404).
    """
    cleaned = value.replace("-", "")
    return len(cleaned) == 32 and all(c in _HEX_CHARS for c in cleaned.lower())


async def _try_playerdb_name(username: str) -> str | None:
    """PlayerDB name → UUID. Returns canonical dashed UUID, or ``None``.

    PlayerDB accepts both names and UUIDs on the same endpoint; this is the
    name-direction call. The response's ``username`` field is ignored — see
    :func:`get_mc_uuid` for the canonical-name confirmation pattern.
    """
    session = await get_session()
    try:
        async with session.get(
            f"https://playerdb.co/api/player/minecraft/{username}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as res:
            if res.status != 200:
                logger.debug(f"playerdb name returned {res.status} for {username!r}")
                return None
            data = await res.json(content_type=None)
            if not data.get("success"):
                logger.debug(
                    f"playerdb name returned success=false for {username!r}: {data.get('code')}"
                )
                return None
            raw = (data.get("data") or {}).get("player", {}).get("id")
            return _to_dashed_uuid(raw)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"playerdb name lookup failed for {username!r}: {e}")
        return None


async def _try_mojang_name(username: str) -> str | None:
    """Mojang official name → UUID. Returns canonical dashed UUID, or ``None``.

    Source of truth for *current* names. 404s on old names (Mojang doesn't
    track name history) — those are PlayerDB's territory.
    """
    session = await get_session()
    try:
        async with session.get(
            f"https://api.minecraftservices.com/minecraft/profile/lookup/name/{username}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as res:
            if res.status != 200:
                logger.debug(f"Mojang name returned {res.status} for {username!r}")
                return None
            data = await res.json()
            return _to_dashed_uuid(data.get("id"))
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"Mojang name lookup failed for {username!r}: {e}")
        return None


async def get_mc_uuid(username: str) -> str | None:
    """Resolve a Minecraft username → UUID. The inverse of :func:`get_mc_username`.

    Lookup order (server-side application of the reliability ladder — see
    ``.claude/CLAUDE.md``):

    1. Local ``MojangNameCache`` row matching the username case-insensitively.
    2. PlayerDB (permissive, retains old-name → UUID mappings).
    3. Mojang official (authoritative, restrictive rate limit).

    ashcon is intentionally excluded — PlayerDB does the same job at higher
    accuracy. Wynncraft is excluded too because the canonical caller
    (:func:`lib.mc.resolve.ensure_mc_account`'s ``WynnApiError`` handler)
    has just exhausted that source on the same name.

    Returns the canonical dashed-form UUID, or ``None`` if every provider
    misses. **Never raises** — failures are logged and surface as ``None``
    so callers can fall through to their original error path.

    On a successful upstream resolution, fires a *single* :func:`_try_mojang`
    (UUID → name) call to get the canonical current name, then writes it to
    ``MojangNameCache``. PlayerDB's own ``username`` field is often stale, so
    we never persist it directly. If the Mojang refresh fails, return the
    UUID without writing the cache — better empty than wrong.
    """
    if not username:
        return None
    cleaned = username.strip()
    if not cleaned:
        return None
    # UUID-shape gate: name endpoints would 404, save the round-trip.
    if _looks_like_uuid(cleaned):
        return None

    from orm import MojangNameCache  # local import: ORM not always initialised at import time

    cached = await MojangNameCache.filter(username__iexact=cleaned).first()
    if cached is not None:
        return cached.uuid

    resolved_uuid = await _try_playerdb_name(cleaned)
    provider = "playerdb" if resolved_uuid else None
    if resolved_uuid is None:
        resolved_uuid = await _try_mojang_name(cleaned)
        provider = "mojang" if resolved_uuid else None

    if resolved_uuid is None:
        return None

    # Confirm the canonical Mojang name before writing the cache. PlayerDB's
    # `username` field may itself be stale; defence-in-depth like
    # `resolve_canonical_username`.
    canonical = await _try_mojang(resolved_uuid)
    if canonical:
        await MojangNameCache.update_or_create(
            uuid=resolved_uuid, defaults={"username": canonical}
        )
        logger.debug(
            "get_mc_uuid: %r -> %s via %s (cached canonical %r)",
            cleaned, resolved_uuid, provider, canonical,
        )
    else:
        logger.debug(
            "get_mc_uuid: %r -> %s via %s (canonical refresh failed; cache write skipped)",
            cleaned, resolved_uuid, provider,
        )
    return resolved_uuid


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

        for provider, fn in (
            ("mojang", _try_mojang),
            ("playerdb", _try_playerdb),
            ("ashcon", _try_ashcon),
        ):
            name = await fn(uuid)
            if name:
                await MojangNameCache.update_or_create(uuid=uuid, defaults={"username": name})
                logger.debug(f"Resolved {uuid} -> {name} via {provider}")
                return name

        if cached:
            logger.warning(f"All upstream Mojang providers failed for {uuid}; serving stale cached value")
            return cached.username

        raise RuntimeError(f"All Mojang providers failed and no cache available for uuid {uuid}")


async def resolve_canonical_username(uuid: str, hint: str | None) -> str:
    """Like ``get_mc_username``, but cross-checks the resolved name against an
    independently-sourced ``hint`` (e.g. the Wynncraft API's username for the
    same UUID). If the two disagree case-insensitively, treat that as evidence
    that one of our cached/fallback providers is stale and force a refresh
    from Mojang as the authoritative tiebreaker.

    This is the right entry point for any caller that already has a separate
    source of truth on hand (currently: the activity loop, which gets the Wynn
    username from the same API response that gives us the UUID). Callers
    without a hint should keep using ``get_mc_username``.

    Always returns a non-empty string. Falls back to ``hint`` (or whatever
    ``get_mc_username`` produced) if the Mojang refresh itself fails.
    """
    try:
        resolved = await get_mc_username(uuid)
    except RuntimeError:
        # Cache empty AND all providers down. Trust the hint if we have one,
        # otherwise re-raise.
        if hint:
            return hint
        raise

    if hint is None or resolved.lower() == hint.lower():
        return resolved

    # Mismatch: Wynn says one thing, our Mojang-cache stack says another.
    # Force-refresh from Mojang directly (bypassing both cache and the
    # other providers) and use that as the tiebreaker. ``_try_mojang`` is
    # the only provider we trust unconditionally for this purpose.
    authoritative = await _try_mojang(uuid)
    if authoritative is None:
        # Mojang itself failed. Keep whatever get_mc_username gave us
        # (probably stale), but log loudly so the discrepancy doesn't go
        # unnoticed.
        logger.warning(
            "resolve_canonical_username: name discrepancy for %s (hint=%r resolved=%r) "
            "but Mojang refresh failed; keeping resolved value",
            uuid, hint, resolved,
        )
        return resolved

    if authoritative.lower() != resolved.lower():
        # Our cached/fallback value was wrong; overwrite the cache so future
        # lookups stop returning the stale name.
        from orm import MojangNameCache  # local import: ORM may not be initialised at import time
        await MojangNameCache.update_or_create(uuid=uuid, defaults={"username": authoritative})
        logger.warning(
            "resolve_canonical_username: corrected stale cache for %s "
            "(was %r, hint=%r, mojang says %r)",
            uuid, resolved, hint, authoritative,
        )
    elif authoritative.lower() != hint.lower():
        # Mojang agrees with our cache; the *Wynn* hint is the outlier.
        # Quieter log -- this can legitimately happen if a player was
        # renamed between Wynn's last refresh and Mojang's.
        logger.info(
            "resolve_canonical_username: Wynn hint %r disagrees with Mojang %r for %s; using Mojang",
            hint, authoritative, uuid,
        )
    return authoritative


async def unload():
    global _session
    if _session and not _session.closed:
        await _session.close()

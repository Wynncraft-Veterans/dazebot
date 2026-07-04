"""Item name resolution for ``~shopping request``.

Backs onto the Wynncraft v3 ``/v3/item/search/{name}`` endpoint via the
shared :class:`~lib.mc.wynn_api.requestor.Requestor` singleton, so item
lookups share the same rate-limit budget as the rest of dazebot's
Wynncraft API traffic. Shopping is low-volume (staff-only, a handful of
lookups per session at most), so this rides alongside guild/player
polls without meaningfully affecting their throughput.

The task file originally referenced Wynnventory's API; we hit Wynncraft
directly instead because (a) Wynnventory proxies Wynncraft anyway,
(b) the v3 search endpoint is public and needs no extra key, and
(c) using the existing Requestor keeps rate-limit accounting in one
place.

Hard-validation semantics: :func:`resolve_item_name` returns the
canonical Wynncraft display name for an exact case-insensitive match,
or raises :class:`ItemLookupError`. The cog surfaces that error to
staff — nothing is written on lookup failure.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Optional

from lib.mc.wynn_api.errors import WynnApiError, raise_for_error_envelope
from lib.mc.wynn_api.requestor import Requestor

logger = logging.getLogger("dazebot.cogs.rewards.shopping.items")


_SEARCH_URL = "https://api.wynncraft.com/v3/item/search/{q}"

# Cache successful lookups for 24h, misses for 5min so a Wynncraft item
# addition lands within a working window. Process-local; state is lost on
# restart, which is fine.
_CACHE_HIT_TTL_SECONDS = 24 * 3600
_CACHE_MISS_TTL_SECONDS = 5 * 60

# (canonical_or_None, expires_at_epoch_seconds)
_cache: dict[str, tuple[Optional[str], float]] = {}


class ItemLookupError(RuntimeError):
    """Raised when :func:`resolve_item_name` cannot canonicalize the query.

    The message is intended for direct display to a staff user in
    Discord — keep it short and actionable.
    """


def _cache_get(key: str) -> Optional[tuple[Optional[str], float]]:
    entry = _cache.get(key)
    if entry is None:
        return None
    _canonical, expires_at = entry
    if expires_at <= time.time():
        _cache.pop(key, None)
        return None
    return entry


def _cache_put(key: str, canonical: Optional[str]) -> None:
    ttl = _CACHE_HIT_TTL_SECONDS if canonical is not None else _CACHE_MISS_TTL_SECONDS
    _cache[key] = (canonical, time.time() + ttl)


def _extract_candidates(data: object) -> list[dict]:
    """Normalize the ``/v3/item/search`` response into a candidate list.

    Wynncraft's v3 API returns either a JSON array or a dict keyed by
    internal name depending on the endpoint and query shape. Either
    way we care about the values.
    """
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        return [v for v in data.values() if isinstance(v, dict)]
    return []


async def resolve_item_name(query: str) -> str:
    """Return the canonical Wynncraft display name for ``query``.

    Accepts any casing / whitespace. Exact case-insensitive match wins;
    if no exact match but exactly one candidate came back, that
    candidate is accepted (Wynncraft's fuzzy search sometimes returns
    a single obvious result for a partial query). Otherwise raises
    :class:`ItemLookupError` — with a "Did you mean" suggestion when
    Wynncraft returned near-matches.
    """
    q = query.strip()
    if not q:
        raise ItemLookupError("Item name cannot be empty.")

    key = q.lower()
    cached = _cache_get(key)
    if cached is not None:
        canonical, _exp = cached
        if canonical is None:
            raise ItemLookupError(f"Item `{q}` not found in the Wynncraft item database.")
        return canonical

    url = _SEARCH_URL.format(q=urllib.parse.quote(q))
    try:
        response = await Requestor().get(url)
    except Exception as e:
        logger.warning("wynn item search transport failure for %r: %r", q, e)
        raise ItemLookupError(
            "Couldn't reach the Wynncraft item API — try again in a moment."
        ) from e

    if response.status == 404:
        _cache_put(key, None)
        raise ItemLookupError(f"Item `{q}` not found in the Wynncraft item database.")
    if response.status != 200:
        logger.warning("wynn item search returned %s for %r", response.status, q)
        raise ItemLookupError(
            f"Wynncraft item API returned status {response.status} — try again in a moment."
        )

    try:
        data = await response.json()
    except Exception as e:
        logger.warning("wynn item search JSON parse failure for %r: %r", q, e)
        raise ItemLookupError(
            "Wynncraft item API returned an unexpected response."
        ) from e

    try:
        raise_for_error_envelope(data, url=url)
    except WynnApiError as e:
        logger.info("wynn item search error envelope for %r: %r", q, e)
        raise ItemLookupError(f"Item lookup failed: {e.message}") from e

    candidates = _extract_candidates(data)
    for item in candidates:
        display = item.get("displayName")
        if isinstance(display, str) and display.lower() == key:
            _cache_put(key, display)
            return display

    if len(candidates) == 1:
        display = candidates[0].get("displayName")
        if isinstance(display, str) and display:
            _cache_put(key, display)
            return display

    if candidates:
        names = [c.get("displayName") for c in candidates
                 if isinstance(c.get("displayName"), str)]
        if names:
            suggestion = ", ".join(f"`{n}`" for n in names[:5])
            raise ItemLookupError(
                f"No exact match for `{q}`. Did you mean: {suggestion}?"
            )

    _cache_put(key, None)
    raise ItemLookupError(f"Item `{q}` not found in the Wynncraft item database.")

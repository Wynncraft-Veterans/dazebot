"""Item name resolution for ``~shopping request`` / ``~shopping edit item``.

Primary lookup is Wynncraft's ``/v3/item/search/{name}`` via the shared
:class:`~lib.mc.wynn_api.requestor.Requestor` singleton — the same
rate-limit budget as the rest of dazebot's Wynncraft traffic. Shopping
is low-volume (staff-only, a handful of lookups per session at most),
so this rides alongside guild/player polls without meaningfully
affecting throughput.

Two nuances distinguish this from a plain WAPI search:

* **Material tiers.** Materials (fishing oils, powders, etc.) share a
  single ``displayName`` in WAPI regardless of tier — the tier lives in
  their ``chances: {TIER1, TIER2, TIER3}`` payload. Staff type e.g.
  ``"Starfish Oil 1"`` to mean "tier-1 refined starfish oil", so we
  parse a trailing `` [1-3]`` suffix off the query, look up the base
  name, and stitch the tier back on when the WAPI row is
  ``type == "material"``. Non-material items reject a tier suffix, and
  a bare material name (no tier) is rejected with a
  "specify tier 1/2/3" message.

* **Wynnventory fallback.** For anything WAPI can't resolve, we retry
  the same (base, tier) pair against Wynnventory's public
  ``/api/trademarket/history/{name}?tier={N}`` endpoint via the shared
  singleton at :mod:`lib.mc.market.wynnventory`. Wynnventory tracks
  per-tier tradable variants, and its history endpoint is documented as
  key-less. In practice the fallback catches anything WAPI is briefly
  missing without introducing a new deployment prerequisite.

Canonical output is **lowercased**. Wynncraft item names are
case-unique, so lowercasing loses no information; downstream storage
(``item_name`` / ``item_name_lower``) becomes trivially consistent, and
``~shopping list`` display gets a uniform look.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from typing import Optional

from lib.mc.market.wynnventory import WynnventoryRequestor
from lib.mc.wynn_api.errors import WynnApiError, raise_for_error_envelope
from lib.mc.wynn_api.requestor import Requestor

logger = logging.getLogger("dazebot.cogs.rewards.shopping.items")


_SEARCH_URL = "https://api.wynncraft.com/v3/item/search/{q}"
_WYNNVENTORY_PATH = "api/trademarket/history/{q}"

_CACHE_HIT_TTL_SECONDS = 24 * 3600
_CACHE_MISS_TTL_SECONDS = 5 * 60

_cache: dict[str, tuple[Optional[str], float]] = {}

_TIER_SUFFIX_RE = re.compile(r"^(.*?)\s+([1-9])$")


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


def _parse_tier(query: str) -> tuple[str, Optional[int]]:
    """Split a stripped query into ``(base_name, tier_or_None)``.

    ``"Starfish Oil 1"`` → ``("Starfish Oil", 1)``; ``"Naval Stone"`` →
    ``("Naval Stone", None)``. Only accepts 1/2/3 as a tier suffix —
    matches the Wynncraft tier space for materials.
    """
    m = _TIER_SUFFIX_RE.match(query)
    if m is None:
        return query, None
    return m.group(1), int(m.group(2))


async def _wynnventory_fallback_ok(base: str, tier: Optional[int]) -> bool:
    """Ask Wynnventory whether the (base, tier) pair maps to real listings.

    Returns True on ``200 + non-empty body``. Any other status/shape
    (including network errors) is a miss, so callers can raise
    :class:`ItemLookupError` cleanly. Wynnventory's public
    ``trademarket/history`` endpoint is used because it doesn't need an
    API key — see :meth:`WynnventoryRequestor.get_public`.
    """
    path = _WYNNVENTORY_PATH.format(q=urllib.parse.quote(base))
    if tier is not None:
        path = f"{path}?tier={tier}"
    try:
        res = await WynnventoryRequestor().get_public(path)
    except Exception as e:
        logger.info("wynnventory fallback transport failure for %r: %r", base, e)
        return False
    if res.status != 200:
        return False
    try:
        data = await res.json()
    except Exception:
        return False
    return bool(data)


async def resolve_item_name(query: str) -> str:
    """Return the canonical (lowercased) item name for ``query``, or raise.

    Accepts an optional trailing ``" 1"``/``" 2"``/``" 3"`` for tiered
    materials (see module docstring). Case-insensitive; return value is
    always lowercase.
    """
    q = query.strip()
    if not q:
        raise ItemLookupError("Item name cannot be empty.")

    key = q.lower()
    cached = _cache_get(key)
    if cached is not None:
        canonical, _exp = cached
        if canonical is None:
            raise ItemLookupError(
                f"Item `{q}` not found in the Wynncraft item database."
            )
        return canonical

    base, tier = _parse_tier(q)

    canonical = await _resolve_via_wapi(base, tier)
    if canonical is None:
        if await _wynnventory_fallback_ok(base, tier):
            canonical = f"{base.lower()} {tier}" if tier is not None else base.lower()

    if canonical is None:
        _cache_put(key, None)
        raise ItemLookupError(
            f"Item `{q}` not found in the Wynncraft item database."
        )

    _cache_put(key, canonical)
    return canonical


async def _resolve_via_wapi(base: str, tier: Optional[int]) -> Optional[str]:
    """WAPI-primary resolution. Returns the canonical (lowercased) string,
    raises :class:`ItemLookupError` for user-visible rejections
    (tier-on-non-material, invalid material tier, "did you mean"), or
    returns ``None`` when WAPI has no data at all so the caller can try
    the Wynnventory fallback.
    """
    url = _SEARCH_URL.format(q=urllib.parse.quote(base))
    try:
        response = await Requestor().get(url)
    except Exception as e:
        logger.warning("wynn item search transport failure for %r: %r", base, e)
        raise ItemLookupError(
            "Couldn't reach the Wynncraft item API — try again in a moment."
        ) from e

    if response.status == 404:
        return None
    if response.status != 200:
        logger.warning("wynn item search returned %s for %r", response.status, base)
        raise ItemLookupError(
            f"Wynncraft item API returned status {response.status} — "
            f"try again in a moment."
        )

    try:
        data = await response.json()
    except Exception as e:
        logger.warning("wynn item search JSON parse failure for %r: %r", base, e)
        raise ItemLookupError(
            "Wynncraft item API returned an unexpected response."
        ) from e

    try:
        raise_for_error_envelope(data, url=url)
    except WynnApiError as e:
        logger.info("wynn item search error envelope for %r: %r", base, e)
        raise ItemLookupError(f"Item lookup failed: {e.message}") from e

    candidates = _extract_candidates(data)
    key = base.lower()
    match: Optional[dict] = None
    for item in candidates:
        display = item.get("displayName")
        if isinstance(display, str) and display.lower() == key:
            match = item
            break
    if match is None and len(candidates) == 1:
        # Wynncraft's fuzzy search sometimes returns a single obvious
        # result for a partial query — accept it.
        match = candidates[0]

    if match is not None:
        display_raw = match.get("displayName")
        if not isinstance(display_raw, str) or not display_raw:
            return None
        display = display_raw.lower()
        is_material = match.get("type") == "material"

        if tier is not None:
            if not is_material:
                raise ItemLookupError(
                    f"'{display}' is not a tiered material — drop the tier "
                    f"number."
                )
            chances = match.get("chances")
            if isinstance(chances, dict) and chances and f"TIER{tier}" not in chances:
                raise ItemLookupError(
                    f"'{display}' does not have tier {tier}."
                )
            return f"{display} {tier}"

        if is_material:
            raise ItemLookupError(
                f"'{display}' is a tiered material — specify a tier: "
                f"'{display} 1', '{display} 2', or '{display} 3'."
            )
        return display

    if candidates:
        names = [
            c.get("displayName")
            for c in candidates
            if isinstance(c.get("displayName"), str)
        ]
        if names:
            suggestion = ", ".join(f"`{n}`" for n in names[:5])
            raise ItemLookupError(
                f"No exact match for `{base}`. Did you mean: {suggestion}?"
            )

    return None

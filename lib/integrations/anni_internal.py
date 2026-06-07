"""Client for vets-anni's secret-gated ``/api/internal/*`` endpoints.

Currently used only by the CTP link-bonus reconciler to learn which
``mc_uuid``s have at least one ``RoleCapability`` row in fishbot — that's
the signal for the "have a fishbot role" 1-point award.

Auth reuses ``DAZEBOT_INTROSPECT_SECRET`` (the same shared header that
gates dazebot's own ``/api/internal/*`` — vets-anni accepts it inbound too,
so one rotation knob covers both sides of the verify-network trust
boundary). Transport failures degrade silently (``None``); the reconciler
just skips the fishbot bonus that tick and tries again next interval.
"""

from __future__ import annotations

import logging
import os

import aiohttp

from config import CurrConfig

logger = logging.getLogger("dazebot.lib.integrations.anni_internal")

_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={"User-Agent": "dazebot/1.0 (link-bonus-reconciler)"},
            timeout=aiohttp.ClientTimeout(total=10),
        )
    return _session


async def close() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
        _session = None


async def fetch_role_capability_uuids() -> set[str] | None:
    """Return the set of ``mc_uuid``s with ≥1 fishbot ``RoleCapability``.

    ``None`` on any transport / auth / unset-secret failure — caller treats
    that as "skip the fishbot bonus this tick" rather than an error.
    """
    secret = os.environ.get("DAZEBOT_INTROSPECT_SECRET")
    if not secret:
        logger.warning(
            "fetch_role_capability_uuids: DAZEBOT_INTROSPECT_SECRET unset; "
            "skipping fishbot link-bonus check."
        )
        return None
    url = CurrConfig.VETS_ANNI_INTERNAL_URL.rstrip("/") + "/role-capability-uuids"
    try:
        session = await _get_session()
        async with session.get(
            url, headers={"X-Introspect-Secret": secret}
        ) as res:
            if res.status != 200:
                logger.warning(
                    "fetch_role_capability_uuids: %s -> HTTP %d", url, res.status,
                )
                return None
            data = await res.json()
    except (aiohttp.ClientError, TimeoutError) as exc:
        logger.warning("fetch_role_capability_uuids: %s unreachable: %s", url, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("fetch_role_capability_uuids: non-object body %r", type(data))
        return None
    raw = data.get("uuids") or []
    if not isinstance(raw, list):
        logger.warning("fetch_role_capability_uuids: 'uuids' is %r", type(raw))
        return None
    return {str(u) for u in raw}

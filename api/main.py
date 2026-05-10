"""FastAPI app exposed by dazebot to other VETS services on the verify network.

Two responsibilities, one app:

1. **picolimbo link flow** — picolimbo POSTs every in-game chat line to
   ``GET /api/auth/{uuid}/{msg}`` so we can look for link codes. This was the
   API's original purpose and gives the ``/api/auth`` prefix its name. It
   is **not** a Discord <-> in-game chat bridge — that lives in
   ``temporary-server``.

2. **vetsmod key introspection** — ``temporary-server`` POSTs to
   ``/api/auth/introspect`` to validate vetsmod ``/unlock`` keys at WS connect
   time. See :mod:`lib.verify_keys`.

Both endpoints are reachable only on the docker ``verify`` network (no public
exposure via traefik).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from fastapi import Body, FastAPI, Header, HTTPException

from lib import mc
from lib.first_install_view import post_fallback_completion
from lib.linking import dm_or_log, try_consume_code
from lib.verify_keys import introspect

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger("dazebot.api")


def create_app(bot: Bot) -> FastAPI:
    app = FastAPI(title="Dazebot API", version="0.2.0")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "bot_ready": bot.is_ready(),
        }

    @app.get("/api/auth/{uuid}/{msg}")
    async def link_attempt(uuid: str, msg: str):
        """Account-link probe. Picolimbo hits this for *every* line a player
        types in chat, but from our perspective ``msg`` is just an opaque
        string we scan for a 6-character link code -- there is no chat
        bridging here, and there must not be (see module docstring).

        Returns ``{"status": "ignored"}`` when the message is not a link
        code (the common case). Returns
        ``{"status": "linked"|"refused"}`` with the human-readable reason
        when there was a pending code for this username.
        """
        try:
            username = await mc.get_mc_username(uuid)
        except Exception:  # noqa: BLE001 - Mojang is third-party
            logger.exception("link_attempt: Mojang lookup failed for %s", uuid)
            return {"status": "ignored", "reason": "mojang_lookup_failed"}

        outcome = await try_consume_code(bot, uuid, username, msg)
        if outcome is None:
            return {"status": "ignored"}

        # DM the user the result so they get feedback even if they're not in
        # the channel where they triggered the original /link flow. If the DM
        # fails (DMs closed), post a public confirmation in the fallback
        # channel so the user still sees the outcome.
        if outcome.discord_user is not None:
            verb = "Link complete" if outcome.success else "Link failed"
            dmed = await dm_or_log(
                outcome.discord_user,
                f"**{verb}** - {outcome.reason}",
                fallback_logger=logger,
            )
            if not dmed:
                await post_fallback_completion(
                    bot,
                    outcome.discord_user,
                    success=outcome.success,
                    reason=outcome.reason,
                )

        return {
            "status": "linked" if outcome.success else "refused",
            "reason": outcome.reason,
        }

    @app.post("/api/auth/introspect")
    async def introspect_key(
        body: dict = Body(...),
        x_introspect_secret: str | None = Header(default=None),
    ):
        """Validate a vetsmod ``/unlock`` key on behalf of ``temporary-server``.

        Defense in depth: even though this endpoint is only reachable on the
        verify docker network, we also gate it behind a shared secret read
        from ``DAZEBOT_INTROSPECT_SECRET``. Without the env var set, the
        endpoint refuses *all* requests — fail closed if misconfigured.

        Request body: ``{"key": "<vetsmod key>"}``
        Response: ``{"valid": bool, "tier": str|null, "ws_tier": str|null,
                    "disc_uuid": str|null, "mc_uuid": str|null,
                    "mc_username": str|null, "reason": str|null}``
        """
        expected = os.environ.get("DAZEBOT_INTROSPECT_SECRET")
        if not expected:
            logger.error(
                "introspect: DAZEBOT_INTROSPECT_SECRET not set; refusing all requests"
            )
            raise HTTPException(status_code=503, detail="introspection disabled")
        if x_introspect_secret != expected:
            # Don't leak whether the secret is wrong vs not present.
            raise HTTPException(status_code=401, detail="unauthorized")

        key = (body or {}).get("key")
        if not isinstance(key, str) or not key:
            raise HTTPException(status_code=400, detail="missing 'key' in body")

        result = await introspect(bot, key)
        return {
            "valid": result.valid,
            "disc_uuid": result.disc_uuid,
            "mc_uuid": result.mc_uuid,
            "mc_username": result.mc_username,
            "tier": result.tier,
            "ws_tier": result.ws_tier,
            "reason": result.reason,
        }

    return app

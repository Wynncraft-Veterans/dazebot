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
from lib.rank_alerts import post_rank_alert
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

    @app.post("/api/internal/rank-alert")
    async def rank_alert(
        body: dict = Body(...),
        x_introspect_secret: str | None = Header(default=None),
    ):
        """Post a guild rank-change alert (BAN or KICK) to Discord.

        Called by ``temporary-server`` after it deduplicates rank_change
        frames from vetsmod clients. Reuses ``DAZEBOT_INTROSPECT_SECRET``
        for auth — same shared secret already gates the introspection
        endpoint, and both calls travel the verify docker network.

        Body: ``{actor, target, from_rank, to_rank, classification}``
        where ``classification`` is ``"ban"`` or ``"kick"``. ``"mote"``
        events are handled entirely by temporary-server's bridge sender
        and never reach this endpoint.

        Posting + WAPI verification run in a fire-and-forget background
        task; this endpoint returns ``{"status": "scheduled"}`` immediately
        so temporary-server isn't blocked on Discord round-trips.
        """
        expected = os.environ.get("DAZEBOT_INTROSPECT_SECRET")
        if not expected:
            logger.error(
                "rank_alert: DAZEBOT_INTROSPECT_SECRET not set; refusing"
            )
            raise HTTPException(status_code=503, detail="rank-alert disabled")
        if x_introspect_secret != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

        payload = body or {}
        classification = str(payload.get("classification", "")).strip().lower()
        actor = str(payload.get("actor", "")).strip()
        target = str(payload.get("target", "")).strip()
        from_rank = str(payload.get("from_rank", "")).strip()
        to_rank = str(payload.get("to_rank", "")).strip()

        if classification not in ("ban", "kick"):
            raise HTTPException(
                status_code=400,
                detail="classification must be 'ban' or 'kick'",
            )
        if not actor or not target or not from_rank or not to_rank:
            raise HTTPException(
                status_code=400,
                detail="actor, target, from_rank, to_rank are required",
            )

        # Fire-and-forget: don't keep temporary-server's HTTP call open
        # while we do Discord/WAPI work.
        import asyncio as _asyncio  # local import keeps top-level imports tidy
        _asyncio.create_task(
            post_rank_alert(bot, classification, actor, target, from_rank, to_rank),
            name=f"rank-alert-{classification}",
        )
        return {"status": "scheduled"}

    return app

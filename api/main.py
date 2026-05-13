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

from lib.discord_utils.first_install_view import post_fallback_completion
from lib.mc import mojang as mc
from lib.mc.linking import dm_or_log, try_consume_code
from lib.mc.wynn_api.errors import WynnApiError
from lib.staff import staff_actions
from lib.staff.rank_alerts import post_rank_alert
from lib.staff.verify_keys import introspect

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

    @app.post("/api/internal/staff-action")
    async def staff_action(
        body: dict = Body(...),
        x_introspect_secret: str | None = Header(default=None),
    ):
        """Record one caution / warning / eject and apply its side-effects.

        Forwarded by ``temporary-server`` after a staff-gated WS frame
        (``caution_add`` / ``warn_add`` / ``eject_add``) lands. Same auth
        gate as ``/api/internal/rank-alert``.

        Body::

            {
              "kind": "caution" | "warning" | "eject",
              "target_username": str,         # mc username or uuid
              "actor_uuid": str,
              "actor_username": str,
              "actor_rank": str | null,       # in-game guild rank
              "message": str | null,          # formal warning / eject text
            }

        Response::

            {
              "status": "ok",
              "target_uuid": str,
              "target_username": str,         # canonical (Mojang-resolved)
              "prior_total": int,
              "new_total": int,
              "triggered": "none" | "warning" | "eject",
              "dm_sent": bool,
              "blocklisted": bool,
              "suggested_dispatch": "kick" | "demote" | null,
            }
        """
        expected = os.environ.get("DAZEBOT_INTROSPECT_SECRET")
        if not expected:
            logger.error(
                "staff_action: DAZEBOT_INTROSPECT_SECRET not set; refusing"
            )
            raise HTTPException(status_code=503, detail="staff-action disabled")
        if x_introspect_secret != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

        payload = body or {}
        kind = str(payload.get("kind", "")).strip().lower()
        target_username = str(payload.get("target_username", "")).strip()
        actor_uuid = str(payload.get("actor_uuid", "")).strip()
        actor_username = str(payload.get("actor_username", "")).strip()
        actor_rank_raw = payload.get("actor_rank")
        actor_rank = str(actor_rank_raw).strip() if actor_rank_raw else None
        message_raw = payload.get("message")
        message = str(message_raw).strip() if message_raw else None

        if kind not in ("caution", "warning", "eject"):
            raise HTTPException(
                status_code=400,
                detail="kind must be one of caution|warning|eject",
            )
        if not target_username or not actor_uuid or not actor_username:
            raise HTTPException(
                status_code=400,
                detail="target_username, actor_uuid, actor_username are required",
            )

        try:
            result = await staff_actions.record_action(
                bot,
                kind=kind,
                target_username=target_username,
                actor_uuid=actor_uuid,
                actor_username=actor_username,
                actor_rank=actor_rank,
                message=message,
            )
        except WynnApiError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"could not resolve target {target_username!r}: {exc.message}",
            )
        except Exception:  # noqa: BLE001 - third-party API
            logger.exception("staff_action: record_action failed")
            raise HTTPException(status_code=500, detail="record_action failed")

        return {
            "status": "ok",
            "target_uuid": result.target_uuid,
            "target_username": result.target_username,
            "prior_total": result.prior_total,
            "new_total": result.new_total,
            "triggered": result.triggered,
            "dm_sent": result.dm_sent,
            "blocklisted": result.blocklisted,
            "suggested_dispatch": result.suggested_dispatch,
        }

    @app.get("/api/internal/staff-actions/{name_or_uuid}")
    async def staff_actions_history(
        name_or_uuid: str,
        x_introspect_secret: str | None = Header(default=None),
    ):
        """Return the cumulative caution-point total + recent action
        history for ``name_or_uuid`` (a Minecraft username or UUID).

        Reads-only. Used by vetsmod's ``/wv check`` (forwarded via
        temporary-server) and by the Discord ``~warnings`` cog
        internally.

        Response::

            {
              "target_uuid": str,
              "target_username": str,
              "total_points": int,
              "entries": [
                {
                  "kind": str,
                  "points": int,
                  "actor_username_at_time": str,
                  "message": str | null,
                  "created_at": str,  # ISO 8601 UTC
                },
                ...
              ]
            }

        ``404`` if the target cannot be resolved (no MinecraftAccount,
        Wynncraft API doesn't recognise them either).
        """
        expected = os.environ.get("DAZEBOT_INTROSPECT_SECRET")
        if not expected:
            logger.error(
                "staff_actions_history: DAZEBOT_INTROSPECT_SECRET not set; refusing"
            )
            raise HTTPException(status_code=503, detail="staff-actions disabled")
        if x_introspect_secret != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

        try:
            target_uuid, canonical, _mc = await staff_actions.resolve_target(name_or_uuid)
        except WynnApiError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"could not resolve target {name_or_uuid!r}: {exc.message}",
            )
        except Exception:  # noqa: BLE001 - third-party API
            logger.exception("staff_actions_history: resolve failed")
            raise HTTPException(status_code=502, detail="resolve failed")

        total = await staff_actions.total_points_for(target_uuid)
        rows = await staff_actions.history_for(target_uuid)
        return {
            "target_uuid": target_uuid,
            "target_username": canonical,
            "total_points": total,
            "entries": [
                {
                    "kind": r.kind,
                    "points": r.points,
                    "actor_username_at_time": r.actor_username_at_time,
                    "message": r.message,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ],
        }

    return app

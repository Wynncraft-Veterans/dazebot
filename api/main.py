"""FastAPI app exposed by dazebot to the picolimbo (auth-stack) mini-server.

This API has exactly one job: receive in-game chat lines from picolimbo and,
if any of them happen to contain a pending account-link code, complete the
link. It is **not** a Discord <-> in-game chat bridge - that responsibility
lives in the ``temporary-server`` stack and must not be duplicated here.

Endpoint: ``GET /api/auth/{uuid}/{msg}``

The path was historically named ``/incoming_chat`` which was misleading
(picolimbo POSTs *every* chat line, not just link codes). It has been
renamed to ``/api/auth`` to reflect what dazebot actually does with the
data. The auth-stack default ``REMOTE_API_URL`` already includes this
suffix, so no per-deployment override is needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI

from lib import mc
from lib.first_install_view import post_fallback_completion
from lib.linking import dm_or_log, try_consume_code

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

    return app

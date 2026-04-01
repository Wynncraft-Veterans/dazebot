from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI

from lib import mc
from lib.link_listener import link_listeners

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger("dazebot.api")


def create_app(bot: Bot) -> FastAPI:
    app = FastAPI(title="Dazebot API", version="0.1.0")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "bot_ready": bot.is_ready(),
        }

    @app.get("/incoming_chat/{uuid}/{msg}")
    async def incoming_chat(uuid: str, msg: str):
        print(uuid, msg)

        username = await mc.get_mc_username(uuid)

        key = username.lower()
        if key in link_listeners:
            listener = link_listeners[key]
            if not listener.future.done():
                listener.future.set_result((uuid, username, msg))

    return app

import logging
import os

import uvicorn
from discord.ext import commands

from api.main import create_app
from bot import Bot

logger = logging.getLogger("dazebot.cogs.api_server")

PORT = int(os.environ["DAZEBOT_PORT"])


class APIServer(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot
        self._server: uvicorn.Server | None = None

    async def cog_load(self):
        app = create_app(self.bot)
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=PORT,
            log_level="info",
            loop="none",
        )
        self._server = uvicorn.Server(config)
        self.bot.loop.create_task(self._server.serve())
        logger.info(f"API server starting on :{PORT}")

    async def cog_unload(self):
        if self._server:
            self._server.should_exit = True
            logger.info("API server shutting down")


async def setup(bot: Bot):
    await bot.add_cog(APIServer(bot))

# This is all vibe coded based on the spec passed on,
# just crossing my fingers it works as i cant test it :p
#
# will improve it if things dont work
import asyncio
import collections
import json
import logging

import discord
import uvicorn
from discord.ext import commands, tasks
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.requests import Request

from bot import Bot
from orm import DiscordAccount, MinecraftAccount, Waitlist

logger = logging.getLogger("dazebot.cogs.chat_bridge")

DEDUP_MAXLEN = 200  # how many messages to keep track of
BATCH_INTERVAL = 0.5  # seconds


def _fingerprint(username: str, message: str) -> str:
    return username.lower() + "\x00" + message


class DeduplicatorSet:
    def __init__(self, maxlen: int = DEDUP_MAXLEN):
        self._deq: collections.deque[str] = collections.deque(maxlen=maxlen)
        self._set: set[str] = set()

    def seen(self, key: str) -> bool:
        return key in self._set

    def add(self, key: str):
        evicted = self._deq[0] if len(self._deq) == self._deq.maxlen else None
        self._deq.append(key)
        if evicted is not None:
            self._set.discard(evicted)
        self._set.add(key)


class OutboundManager:
    def __init__(self):
        # token: WebSocket
        self._clients: dict[str, WebSocket] = {}

    async def connect(self, token: str, ws: WebSocket):
        await ws.accept()
        self._clients[token] = ws
        logger.info(f"WS client connected (token={token})")

    def disconnect(self, token: str):
        self._clients.pop(token, None)
        logger.info(f"WS client disconnected (token={token})")

    async def broadcast(self, data: dict):
        msg = json.dumps(data)
        dead = []
        for token, ws in self._clients.items():
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(token)
        for t in dead:
            self.disconnect(t)


def create_bridge_app(
    bot: Bot, outbound: OutboundManager, dedup: DeduplicatorSet
) -> tuple[FastAPI, list, asyncio.Lock]:
    app = FastAPI(title="Dazebot Bridge", version="0.1.0")

    # pending inbound messages waiting to be batched
    _pending: list[dict] = []
    _batch_lock = asyncio.Lock()

    async def _get_mc_by_token(token: str) -> MinecraftAccount | None:
        return await MinecraftAccount.filter(token=token).first()

    @app.post("/v1/inbound")
    async def inbound(request: Request):
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        token = data.get("token")
        msg_type = data.get("type")
        rank = data.get("rank", "")
        username = data.get("username", "")
        message = data.get("message", "")

        if not all([token, msg_type, username, message]):
            return JSONResponse({"error": "missing fields"}, status_code=400)

        mc = await _get_mc_by_token(token)
        if mc is None:
            return JSONResponse({"error": "invalid token"}, status_code=401)

        fp = _fingerprint(username, message)
        if dedup.seen(fp):
            return JSONResponse({"status": "duplicate"})
        dedup.add(fp)

        payload = {"type": msg_type, "rank": rank, "username": username, "message": message}

        async with _batch_lock:
            _pending.append(payload)

        # broadcast to WS clients immediately (Discord batching is separate)
        await outbound.broadcast(payload)

        return JSONResponse({"status": "ok"})

    @app.websocket("/v1/outbound")
    async def outbound_ws(ws: WebSocket, token: str = ""):
        mc = await _get_mc_by_token(token)
        if mc is None:
            await ws.close(code=4001)
            return

        await outbound.connect(token, ws)
        try:
            while True:
                # keep connection alive, we only push from server side
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            outbound.disconnect(token)

    return app, _pending, _batch_lock


async def _is_eligible(mc: MinecraftAccount) -> bool:
    if mc.guild == "Returners":
        return True
    if mc.is_honourary:
        return True
    return await Waitlist.filter(minecraft_account=mc).exists()


class ChatBridge(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        self._outbound = OutboundManager()
        self._dedup = DeduplicatorSet()
        self._server: uvicorn.Server | None = None
        self._pending: list[dict] = []
        self._batch_lock: asyncio.Lock | None = None
        logger.info("ChatBridge cog initialized")

    async def cog_load(self):
        app, self._pending, self._batch_lock = create_bridge_app(self.bot, self._outbound, self._dedup)

        import os

        port = int(os.environ["BRIDGE_PORT"])
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", loop="none")
        self._server = uvicorn.Server(config)
        self.bot.loop.create_task(self._server.serve())
        self._batch_sender.start()
        self._token_cleanup.start()
        logger.info(f"Bridge server starting on :{port}")

    async def cog_unload(self):
        self._batch_sender.cancel()
        self._token_cleanup.cancel()
        if self._server:
            self._server.should_exit = True

    @tasks.loop(seconds=BATCH_INTERVAL)
    async def _batch_sender(self):
        if not self._pending or self._batch_lock is None:
            return

        async with self._batch_lock:
            batch = self._pending.copy()
            self._pending.clear()

        if not batch:
            return

        channel = self.bot.get_channel(self.bot.config.BRIDGE_CHANNEL)
        if not isinstance(channel, discord.TextChannel):
            logger.error("BRIDGE_CHANNEL not found or not a text channel")
            return

        lines = []
        for msg in batch:
            rank = f"[{msg['rank']}] " if msg.get("rank") else ""
            lines.append(f"**{rank}{msg['username']}**: {msg['message']}")

        await channel.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @tasks.loop(minutes=1)
    async def _token_cleanup(self):
        async for mc in MinecraftAccount.filter(token__not_isnull=True):
            if not await _is_eligible(mc):
                mc.token = None
                await mc.save(update_fields=["token"])
                logger.info(f"Revoked token for {mc.mc_username} (no longer eligible)")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.channel.id != self.bot.config.BRIDGE_CHANNEL:
            return

        disc = await DiscordAccount.filter(disc_uuid=str(message.author.id)).select_related("minecraft_account").first()
        rank = "Recruiter"
        if disc and disc.minecraft_account:
            # TODO[006]: Dont hardcode, put in config
            role_ids = {r.id for r in message.author.roles}
            if 1313778812361904188 in role_ids:
                rank = "Chief"
            elif 1313782599378010163 in role_ids:
                rank = "Strategist"
            elif 1337992726079213712 in role_ids:
                rank = "Captain"

        payload = {
            "type": "bridge",
            "rank": rank,
            "username": message.author.display_name,
            "message": message.content,
            "source": "discord",
        }
        await self._outbound.broadcast(payload)


async def setup(bot: Bot):
    await bot.add_cog(ChatBridge(bot))
    logger.info("ChatBridge cog loaded successfully")

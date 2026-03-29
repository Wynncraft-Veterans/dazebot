import asyncio
import collections
import logging
import time
from typing import Any, Awaitable, Optional
import aiohttp
from aiohttp import ClientResponse
from uuid import UUID, uuid4

logger = logging.getLogger("dazebot.lib.requestor")


class SingletonMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class Requestor(metaclass=SingletonMeta):
    def __init__(self):
        self.deq: collections.deque[tuple[UUID, str, dict[str, Any]]] = collections.deque()
        self.out: dict[UUID, ClientResponse] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None

    async def _ensure_loop(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _probe(self, url: str, **kwargs: Any) -> ClientResponse:
        logger.debug("Started probing")
        session = await self._get_session()
        res = await session.get(url, **kwargs)
        reset = float(res.headers["RateLimit-Reset"])
        if res.status == 429:
            logger.info(f"Hit 429 during probing! Waiting {reset} seconds")
            await asyncio.sleep(reset)
            return await self._probe(url, **kwargs)
        logger.debug("Completed probing")
        return res

    async def _loop(self):
        session = await self._get_session()
        while True:
            if not self.deq:
                # check 20 times a second
                # TODO: maybe replace with asyncio.Event? idk, too lazy to check it out
                await asyncio.sleep(1 / 20)
                continue

            # probe a single request first, to get current ratelimit capacity (completes first request in line)
            uuid, url, kwargs = self.deq.popleft()
            probe = await self._probe(url, **kwargs)
            self.out[uuid] = probe
            remaining = int(float(probe.headers["RateLimit-Remaining"]))
            limit = int(float(probe.headers["RateLimit-Limit"]))

            to_collect = min(len(self.deq), remaining)
            logger.debug(f"{len(self.deq)=} {limit=} {remaining=} {to_collect=}")

            if not to_collect:
                continue

            collection = [self.deq.popleft() for _ in range(to_collect)]

            tasks: list[Awaitable[ClientResponse]] = []
            for _uuid, url, kwargs in collection:
                tasks.append(session.get(url, **kwargs))

            results = await asyncio.gather(*tasks)

            for res, c in zip(results, collection):
                uuid, _url, _kwargs = c
                if res.status == 200:
                    self.out[uuid] = res
                elif res.status == 429:
                    d = {k: v for k, v in res.headers.items() if "ratelimit" in k.lower()}
                    logger.warning(f"Hit status code 429! {d}")
                    self.deq.appendleft(c)
                else:
                    d = {k: v for k, v in res.headers.items() if "ratelimit" in k.lower()}
                    logger.warning(f"Hit status code {res.status}!: {d}")
                    self.out[uuid] = res

    async def _add_request(self, url: str, **kwargs: Any) -> UUID:
        uuid = uuid4()
        self.deq.append((uuid, url, kwargs))
        return uuid

    # TODO: could probably use res.headers.Expires to also cache locally, but meh, it's unlikely we'll request cached data very often
    async def get(self, url: str, **kwargs: Any) -> ClientResponse:
        await self._ensure_loop()

        uuid = await self._add_request(url, **kwargs)
        start = time.time()
        while uuid not in self.out:
            await asyncio.sleep(1 / 20)

        res = self.out.pop(uuid)

        return res

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

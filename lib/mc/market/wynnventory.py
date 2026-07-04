"""Singleton aiohttp client for the Wynnventory REST API.

Held on the shelf: not currently called from anywhere in dazebot, but
the plumbing is here so future features (price statistics lookups,
loot / raid pool queries, trade-market submissions) can use it without
one-off boilerplate.

Shape mirrors :mod:`lib.mc.wynn_api.requestor` — three priority queues
(priority > normal > background) drained by a single loop task, with
rate-limit awareness cribbed from the same header-parsing dance. Two
divergences worth calling out:

* Auth is ``X-API-Key: <dazebot's key>``, not ``Authorization: Bearer``.
  Wynnventory-side rate-limits are bucketed per API key, same as
  Wynncraft's WAPI. The vets-deploy stack runs on one VPS with several
  services; each service uses its own key so per-service headroom stays
  isolated. Keyless requests fall into a global bucket shared across
  every service on the host and should be avoided.
* POST is supported (Wynnventory has submit and search-by-body
  endpoints). The queue entries store the HTTP method alongside the
  URL, and the loop dispatches via :meth:`aiohttp.ClientSession.request`
  rather than a fixed ``.get``.

Base URL defaults to ``https://wynnventory.com`` and is overridable
via ``WYNNVENTORY_BASE_URL`` for staging / local testing. API key
comes from ``WYNNVENTORY_API_KEY`` and is required — missing key
raises on the first call.

Not wired into :meth:`bot.Bot.close`. If dazebot ever starts issuing
these calls in production, add ``await WynnventoryRequestor().close()``
alongside the existing ``Requestor().close()`` line in ``Bot.close()``
at that time. Same convention as
:mod:`lib.integrations.anni_internal`.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
from typing import Any, Awaitable, Optional
from uuid import UUID, uuid4

import aiohttp
from aiohttp import ClientResponse

logger = logging.getLogger("dazebot.lib.mc.market.wynnventory")


_DEFAULT_BASE_URL = "https://wynnventory.com"
_DEFAULT_TIMEOUT_SECONDS = 15


class WynnventoryConfigError(RuntimeError):
    """Raised when ``WYNNVENTORY_API_KEY`` is missing at call time.

    Distinct from a transport / HTTP-status error because the fix is
    "configure the environment", not "retry later".
    """


class SingletonMeta(type):
    _instances: dict = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class WynnventoryRequestor(metaclass=SingletonMeta):
    """Rate-limit-aware queued client for Wynnventory.

    See module docstring. Mirrors the priority-queue shape of
    :class:`lib.mc.wynn_api.requestor.Requestor`. Public API returns
    a raw :class:`aiohttp.ClientResponse` — callers are responsible for
    ``.json()`` / status handling.
    """

    def __init__(self) -> None:
        # (UUID, method, url, kwargs) tuples. Method stored per-request
        # so the same singleton serves GETs and POSTs interleaved.
        self.deq: collections.deque[tuple[UUID, str, str, dict[str, Any]]] = collections.deque()
        self.prio_deq: collections.deque[tuple[UUID, str, str, dict[str, Any]]] = collections.deque()
        self.background_deq: collections.deque[tuple[UUID, str, str, dict[str, Any]]] = collections.deque()
        self.out: dict[UUID, ClientResponse] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._public_session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None

    async def _ensure_loop(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            key = os.environ.get("WYNNVENTORY_API_KEY", "").strip()
            if not key:
                raise WynnventoryConfigError(
                    "WYNNVENTORY_API_KEY is not set. Configure dazebot's "
                    "Wynnventory key in the environment before calling "
                    "this client."
                )
            headers = {
                "X-API-Key": key,
                "User-Agent": "dazebot/1.0 (wynnventory-client)",
            }
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=_DEFAULT_TIMEOUT_SECONDS),
            )
        return self._session

    async def _get_public_session(self) -> aiohttp.ClientSession:
        """Session for Wynnventory endpoints documented as public (no key).

        Kept separate from :attr:`_session` so that the keyed session can
        stay warm (and rate-limit-scoped to dazebot's key) even when a
        public call happens to fire — and, crucially, so this method
        doesn't raise :class:`WynnventoryConfigError` when the env var is
        unset. Callers that only need public endpoints (e.g. the shopping
        cog's item-name fallback) can use :meth:`get_public` without any
        env dependency.
        """
        if self._public_session is None or self._public_session.closed:
            headers = {
                "User-Agent": "dazebot/1.0 (wynnventory-client)",
            }
            self._public_session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=_DEFAULT_TIMEOUT_SECONDS),
            )
        return self._public_session

    @staticmethod
    def _resolve_url(url_or_path: str) -> str:
        """Absolute URLs pass through; bare paths get the base URL prefix."""
        if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
            return url_or_path
        base = os.environ.get("WYNNVENTORY_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
        return f"{base}/{url_or_path.lstrip('/')}"

    async def _probe(
        self, method: str, url: str, **kwargs: Any,
    ) -> ClientResponse:
        """Send one request, honor 429s, and return the response.

        Same shape as :meth:`Requestor._probe` — retries on 429 after
        waiting ``RateLimit-Reset`` / ``Retry-After`` / 1s.
        """
        logger.debug("Started probing %s %s", method, url)
        session = await self._get_session()
        res = await session.request(method, url, **kwargs)
        reset_hdr = (
            res.headers.get("RateLimit-Reset")
            or res.headers.get("Retry-After")
        )
        try:
            reset = float(reset_hdr) if reset_hdr is not None else 1.0
        except ValueError:
            reset = 1.0
        if res.status == 429:
            logger.info("Hit 429 during probing! Waiting %s seconds", reset)
            await asyncio.sleep(reset)
            return await self._probe(method, url, **kwargs)
        logger.debug("Completed probing")
        return res

    async def _loop(self) -> None:
        # Pre-open the session so a missing key fails fast in the loop
        # rather than at first request completion.
        await self._get_session()
        while True:
            if (not self.deq) and (not self.prio_deq) and (not self.background_deq):
                await asyncio.sleep(1 / 20)
                continue
            # Strict priority fallthrough: prio first, then normal, then
            # background. Mirrors Requestor's ordering.
            deq = self.prio_deq or self.deq or self.background_deq
            uuid, method, url, kwargs = deq.popleft()
            probe = await self._probe(method, url, **kwargs)
            self.out[uuid] = probe

            # Wynnventory does not currently expose RateLimit-* headers
            # on public endpoints; scoped-key responses may or may not.
            # When headers are absent, fall through to the "no capacity
            # this tick" branch — same defensive posture as Requestor,
            # which caps effective throughput at ~1 req/sec until the
            # server tells us otherwise.
            try:
                remaining = int(float(probe.headers["RateLimit-Remaining"]))
                limit = int(float(probe.headers["RateLimit-Limit"]))
                reset = int(float(probe.headers["RateLimit-Reset"]))
            except (KeyError, ValueError):
                logger.debug(
                    "Probe response missing RateLimit-* headers "
                    "(status=%s); skipping batch this tick",
                    probe.status,
                )
                await asyncio.sleep(1)
                continue

            usable = max(0, remaining - reset)

            if usable <= 1:
                await asyncio.sleep(1)

            to_collect = min(len(deq), usable)
            logger.debug(
                "%s %s limit=%s remaining=%s to_collect=%s reset=%s usable=%s",
                len(deq), method, limit, remaining, to_collect, reset, usable,
            )

            if not to_collect:
                continue

            collection = [deq.popleft() for _ in range(to_collect)]

            session = await self._get_session()
            tasks: list[Awaitable[ClientResponse]] = []
            for _uuid, m, u, kw in collection:
                tasks.append(session.request(m, u, **kw))

            results = await asyncio.gather(*tasks)

            for res, c in zip(results, collection):
                uuid, _m, _u, _kw = c
                if res.status == 200:
                    self.out[uuid] = res
                elif res.status == 429:
                    d = {k: v for k, v in res.headers.items() if "ratelimit" in k.lower()}
                    logger.warning("Hit status code 429! %s", d)
                    deq.appendleft(c)
                else:
                    d = {k: v for k, v in res.headers.items() if "ratelimit" in k.lower()}
                    logger.warning("Hit status code %s!: %s", res.status, d)
                    self.out[uuid] = res

    async def _enqueue(
        self,
        deq: collections.deque,
        method: str,
        url_or_path: str,
        **kwargs: Any,
    ) -> UUID:
        uuid = uuid4()
        deq.append((uuid, method.upper(), self._resolve_url(url_or_path), kwargs))
        return uuid

    async def _await_result(self, uuid: UUID) -> ClientResponse:
        while uuid not in self.out:
            await asyncio.sleep(1 / 20)
        return self.out.pop(uuid)

    # --- Public API --------------------------------------------------

    async def get(self, url_or_path: str, **kwargs: Any) -> ClientResponse:
        """Normal-priority GET. Returns a raw :class:`aiohttp.ClientResponse`."""
        await self._ensure_loop()
        uuid = await self._enqueue(self.deq, "GET", url_or_path, **kwargs)
        return await self._await_result(uuid)

    async def get0(self, url_or_path: str, **kwargs: Any) -> ClientResponse:
        """Priority GET — jumps to the front of the queue."""
        await self._ensure_loop()
        uuid = await self._enqueue(self.prio_deq, "GET", url_or_path, **kwargs)
        return await self._await_result(uuid)

    async def get_background(
        self, url_or_path: str, **kwargs: Any,
    ) -> ClientResponse:
        """Low-priority GET — drains only when higher tiers are empty."""
        await self._ensure_loop()
        uuid = await self._enqueue(
            self.background_deq, "GET", url_or_path, **kwargs,
        )
        return await self._await_result(uuid)

    async def post(self, url_or_path: str, **kwargs: Any) -> ClientResponse:
        """Normal-priority POST. Pass ``json=...`` for a JSON body,
        ``data=...`` for form-encoded, etc. — everything ``aiohttp`` accepts.
        """
        await self._ensure_loop()
        uuid = await self._enqueue(self.deq, "POST", url_or_path, **kwargs)
        return await self._await_result(uuid)

    async def post0(self, url_or_path: str, **kwargs: Any) -> ClientResponse:
        """Priority POST — jumps to the front of the queue."""
        await self._ensure_loop()
        uuid = await self._enqueue(self.prio_deq, "POST", url_or_path, **kwargs)
        return await self._await_result(uuid)

    async def get_public(self, url_or_path: str, **kwargs: Any) -> ClientResponse:
        """Un-queued, un-authenticated GET for Wynnventory's public endpoints.

        Bypasses the priority queue and the ``X-API-Key`` header. Sized for
        the shopping cog's item-name fallback, which fires only on WAPI
        misses (rare). Handles 429 with a one-shot retry against
        ``RateLimit-Reset`` / ``Retry-After`` / 1s.

        Deliberately not merged into :meth:`get` so that (a) callers with
        a keyed workload keep their scoped rate-limit accounting isolated
        and (b) this method can be invoked without ``WYNNVENTORY_API_KEY``
        being set.
        """
        session = await self._get_public_session()
        url = self._resolve_url(url_or_path)
        res = await session.request("GET", url, **kwargs)
        if res.status == 429:
            reset_hdr = (
                res.headers.get("RateLimit-Reset")
                or res.headers.get("Retry-After")
            )
            try:
                reset = float(reset_hdr) if reset_hdr is not None else 1.0
            except ValueError:
                reset = 1.0
            logger.info("Public probe hit 429, retrying after %s seconds", reset)
            await asyncio.sleep(reset)
            res = await session.request("GET", url, **kwargs)
        return res

    async def close(self) -> None:
        """Shut down the aiohttp sessions and the drain loop. Idempotent."""
        if self._session and not self._session.closed:
            await self._session.close()

        if self._public_session and not self._public_session.closed:
            await self._public_session.close()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

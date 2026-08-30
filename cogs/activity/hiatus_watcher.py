"""Bulk-endpoint watcher for hiatus users online on Wynncraft.

The ``/v3/player?identifier=uuid`` endpoint returns ``{uuid: server}``
for the entire online roster in a single PLAYER-bucket request, 30s
shared-cache TTL. Polling at 30s (= cache TTL) costs ~2 req/min and
detects every HIATUS-role user whose ``onlineStatus`` is *not* hidden.
Membership in the response IS the online signal — no per-UUID
confirmation needed.

Privacy-hidden HIATUS users (~16% of online players at any moment) are
covered separately by ``cogs/activity/server_watcher.py`` via the
per-UUID server-transition inference path.

Detection is a set-arithmetic diff: a UUID newly appearing in the bulk
response that wasn't there last tick is the "offline -> online" trigger.
``_prev_online`` is in-memory and drops a UUID as soon as it leaves the
online set, so an offline-then-online cycle re-fires after the 24h ORM
cooldown elapses. Both paths funnel through
``lib/mc/hiatus_alerts.maybe_alert_hiatus``.

Bucket-utilization audit (PLAYER bucket = 50 req/60s, observed
2026-05-24). At steady state the bot's PLAYER-bucket spend is:

  - Activity weekly prof refresh: ~1 burst of Returners-size calls per
    11-min tick, but only for members past their weekly TTL.
  - server_watcher Returners/waitlisted Unknown-bucket: ~N_unknown calls
    per 3-min tick at normal priority. Bucket free during the rest of
    the 3-min window.
  - server_watcher HIATUS-Unknown (this PR): same cadence, *background*
    queue, only drains when no normal/priority calls are pending; 120s
    TTL guard skips guaranteed cache hits.
  - hiatus_watcher (this file): 1 call per 30-sec tick, normal priority.
    30s cadence = upstream cache TTL, so every tick draws fresh data;
    average detection latency 15s. ~2 req/min against the 50/60s budget.

The 11-min /v3/guild/Returners cadence in Activity is intentionally not
touched here -- its TTL is 120s so it's plausibly under-polled, but
guild-roster freshness affects membership state transitions and would
need a separate review.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from discord.ext import commands, tasks

from bot import Bot
from lib.mc.hiatus_alerts import maybe_alert_hiatus
from lib.mc.wynn_api.requestor import Requestor
from lib.role_state import hiatus_member_uuids
from orm import MinecraftAccount

logger = logging.getLogger("dazebot.cogs.activity.hiatus_watcher")

BULK_ENDPOINT = "https://api.wynncraft.com/v3/player?identifier=uuid"


class HiatusWatcher(commands.Cog):
    bot: Bot
    _prev_online: set[str]

    def __init__(self, bot: Bot):
        self.bot = bot
        self._prev_online = set()
        self.poll.start()
        logger.info("HiatusWatcher cog initialized")

    def cog_unload(self):
        self.poll.cancel()

    @tasks.loop(seconds=30)
    async def poll(self):
        # Master kill-switch: when alerts are muted there's no point
        # spending an API call to compute a diff we'd throw away.
        if not self.bot.config.HIATUS_ALERTS_ENABLED:
            self._prev_online = set()
            return
        hiatus_uuids = await hiatus_member_uuids(self.bot)
        if not hiatus_uuids:
            self._prev_online = set()
            return

        try:
            res = await Requestor().get(BULK_ENDPOINT)
            if res.status != 200:
                logger.warning(
                    "bulk /v3/player returned status=%d (%s); skipping tick",
                    res.status, res.content_type,
                )
                return
            data = await res.json()
        except Exception:
            logger.exception("bulk /v3/player fetch failed; skipping tick")
            return

        players = data.get("players")
        if not isinstance(players, dict):
            logger.warning("bulk /v3/player returned unexpected shape; skipping tick")
            return

        online_hiatus = hiatus_uuids & players.keys()
        newly_online = online_hiatus - self._prev_online
        self._prev_online = online_hiatus

        # Persist the online heartbeat for the whole cohort, not just the
        # newly-online slice: one UPDATE, no API spend, and it is what lets
        # the *next* tick tell a real login from an artefact of our own
        # state having been reset. Read the prior values first — the write
        # below destroys exactly the evidence we need.
        now = datetime.now(timezone.utc)
        gap = timedelta(minutes=float(self.bot.config.HIATUS_RETURN_DM_LOGOUT_GAP_MINUTES))
        still_warm: set[str] = set()
        if newly_online:
            still_warm = set(
                await MinecraftAccount.filter(
                    uuid__in=list(newly_online), online_seen_at__gt=now - gap
                ).values_list("uuid", flat=True)
            )
        if online_hiatus:
            await MinecraftAccount.filter(uuid__in=list(online_hiatus)).update(online_seen_at=now)

        if not newly_online:
            return
        logger.debug(f"newly-online hiatus uuids: {newly_online}")
        for uuid in newly_online:
            # ``newly_online`` is a diff against an in-memory set that is
            # empty on every start, so after a restart it names everyone who
            # happens to be online rather than anyone who just logged in.
            # A persisted observation from inside the logout gap is proof
            # this is that case: we were watching them a moment ago.
            await maybe_alert_hiatus(
                self.bot,
                uuid,
                server=players.get(uuid),
                login_edge=uuid not in still_warm,
            )

    @poll.before_loop
    async def _before_poll(self):
        # ``hiatus_member_uuids`` reads ``bot.get_guild(...).members`` which
        # is None until the guild cache is populated at READY. Same root
        # cause / fix as server_watcher's _before_poll.
        await self.bot.wait_until_ready()


async def setup(bot: Bot):
    await bot.add_cog(HiatusWatcher(bot))
    logger.info("HiatusWatcher cog loaded successfully")

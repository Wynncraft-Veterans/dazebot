"""Server-field watcher for privacy-hidden players.

The Wynncraft v3 ``/player`` response carries an independently-togglable
``server`` field that, in practice, is exposed even for players who have
opted out of ``lastJoin``/``onlineStatus`` visibility (see
``../.claude/membership_spec.md §7`` and the privacy docs at
https://docs.wynncraft.com/privacy). The field is the *last* server they
were on — not a live "currently on" indicator — so it changes on
every login.

This cog polls the unknown-bucket players (those whose ``last_online`` is
the ``UNKNOWN_LAST_ONLINE`` sentinel because the guild/player endpoint
returned ``lastJoin=null``) and, on any transition between two non-null
server values, infers in-interval activity and bumps ``last_online =
now``. The existing ``/purgelist`` logic then surfaces them out of the
Unknown section.

Scope is **Returners *and* waitlisted accounts**. Returners is the
original Unknown-bucket use case; waitlisted accounts were added because
``waitlist_cleanup`` no longer purges the sentinel (an API-hidden
waitlisted player would otherwise sit at epoch forever, untracked) — this
watch is their only path to a real ``last_online`` and thus to the normal
inactivity rule. The waitlist is small, so the extra rows are a rounding
error against the rate budget below.

First observation just records the baseline; we cannot date a change we
have not yet seen happen. Asymmetric transitions involving ``None``
(``EU37 → None`` or ``None → EU37``) are treated as state-only
observations because a privacy toggle is at least as plausible as real
activity.

Cadence: 3 minutes. The PLAYER bucket is 50 req/45s with a 120s upstream
cache TTL. Polling at 180s guarantees uncached responses (no
cache-thrashing waste) while leaving >95% of the bucket free for
interactive lookups.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from discord.ext import commands, tasks
from tortoise.expressions import Q

from bot import Bot
from lib.mc.wynn_api.player import get_player_stats
from orm import UNKNOWN_LAST_ONLINE, MinecraftAccount, is_last_online_unknown

logger = logging.getLogger("dazebot.cogs.activity.server_watcher")


class ServerWatcher(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        self.poll.start()
        logger.info("ServerWatcher cog initialized")

    def cog_unload(self):
        self.poll.cancel()

    @tasks.loop(minutes=3)
    async def poll(self):
        # Server-side filter for the unknown sentinel: matches
        # ``is_last_online_unknown`` (anything within 24h of epoch).
        # Scope: Returners (original use case) OR waitlisted — see module
        # docstring. ``waitlist`` is the reverse relation on MinecraftAccount;
        # Waitlist.minecraft_account is unique, so the join can't fan out.
        candidates = await MinecraftAccount.filter(
            Q(last_online__lte=UNKNOWN_LAST_ONLINE + timedelta(days=1))
            & (Q(guild="Returners") | Q(waitlist__isnull=False))
        )
        if not candidates:
            return
        logger.debug(f"polling {len(candidates)} unknown-bucket player(s)")
        await asyncio.gather(*(self._check_one(a) for a in candidates))

    @poll.before_loop
    async def _before_poll(self):
        # _check_one bulk-saves MinecraftAccount rows; each save fires the
        # vanity post_save signal -> bot.get_guild(), which is None until the
        # guild cache is populated at READY. Without this wait the first pre-
        # READY tick logs "Guild not found" once per polled account. Same
        # root cause and fix as activity.py's _before_check_guild.
        await self.bot.wait_until_ready()

    async def _check_one(self, account: MinecraftAccount):
        try:
            player = await get_player_stats(account.uuid)
        except Exception:
            logger.exception(f"lookup failed for {account.mc_username}")
            return

        observed = player.server
        now = datetime.now(timezone.utc)

        # Free-win path: player un-hid lastJoin between ticks. Adopt the
        # real timestamp directly so they clear out of the Unknown bucket
        # without needing to wait for a server transition. Same monotonic
        # discipline as ``lib/mc/wynn.py``: never roll back.
        if player.lastJoin is not None:
            if is_last_online_unknown(account.last_online) or account.last_online < player.lastJoin:
                account.last_online = player.lastJoin
            account.last_seen_server = observed
            account.server_observed_at = now
            await account.save()
            logger.info(
                f"{account.mc_username}: un-hid lastJoin ({player.lastJoin.isoformat()}); "
                "cleared from Unknown bucket"
            )
            return

        prev_server = account.last_seen_server
        prev_observed_at = account.server_observed_at

        if prev_observed_at is None:
            # Baseline only — we have nothing to compare against yet.
            account.last_seen_server = observed
            account.server_observed_at = now
            await account.save()
            logger.debug(f"{account.mc_username}: baseline server={observed!r}")
            return

        if prev_server is not None and observed is not None and observed != prev_server:
            # Two distinct non-null values across ticks => login activity in
            # (prev_observed_at, now]. ``now`` is the best lower bound we can
            # cite and is sufficient to clear the Unknown bucket.
            account.last_online = now
            account.last_seen_server = observed
            account.server_observed_at = now
            await account.save()
            logger.info(
                f"{account.mc_username}: server {prev_server!r} -> {observed!r}; "
                "bumping last_online=now"
            )
            return

        # No actionable change (same server, or asymmetric None). Touch the
        # observation timestamp so the *next* change interval starts from
        # this poll rather than from the original baseline.
        account.last_seen_server = observed
        account.server_observed_at = now
        await account.save()


async def setup(bot: Bot):
    await bot.add_cog(ServerWatcher(bot))
    logger.info("ServerWatcher cog loaded successfully")

"""Server-field watcher for privacy-hidden players.

The Wynncraft v3 ``/player`` response carries an independently-togglable
``server`` field that, in practice, is exposed even for players who have
opted out of ``lastJoin``/``onlineStatus`` visibility (see
``../.claude/membership_spec.md §7`` and the privacy docs at
https://docs.wynncraft.com/privacy). The field is the *last* server they
were on — not a live "currently on" indicator — so it changes on
every login.

This cog polls accounts whose ``lastJoin`` was observed hidden at the
last guild tick (``lastjoin_hidden_at`` is non-null) OR whose stored
``last_online`` is still the ``UNKNOWN_LAST_ONLINE`` sentinel (legacy /
never-polled case). On any transition between two non-null server
values, or any monotonic stat-counter increase, the watcher infers
in-interval activity and bumps ``last_online = now``. The existing
``/purgelist`` logic then surfaces them out of the Unknown section.

The ``lastjoin_hidden_at`` part of the scope filter closes a long-
standing asymmetry: previously the watcher only polled accounts at the
epoch sentinel, so once an account got bumped out (real activity
detected, or a brief lastJoin un-hide caught by activity.py) it dropped
out of scope permanently. If the player then re-hid lastJoin, their
``last_online`` would age silently until purgelist's freshness verify
reclassified them into the "Unknown (API disabled)" bucket — with no
path back. ``_apply_guild`` now stamps ``lastjoin_hidden_at`` every
tick that observes hidden state, and NULLs it the instant lastJoin
becomes visible again, so the watcher's scope tracks live privacy state
rather than a one-shot decision made on day zero.

Scope is **Returners *and* waitlisted accounts** at normal priority,
**and HIATUS-role accounts at background priority**. Returners is the
original Unknown-bucket use case; waitlisted accounts were added because
``waitlist_cleanup`` no longer purges the sentinel (an API-hidden
waitlisted player would otherwise sit at epoch forever, untracked) — this
watch is their only path to a real ``last_online`` and thus to the normal
inactivity rule. The waitlist is small, so the extra rows are a rounding
error against the rate budget below.

HIATUS-role accounts (ex-members, currently guildless) were added so a
"hiatus user is online" alert fires even when the bulk ``/v3/player``
endpoint can't see them (privacy-hidden ``onlineStatus``). They poll via
the Requestor's background queue, which only drains when no normal- or
priority-tier requests are pending — so this scope can never starve the
existing Unknown-bucket polls or interactive lookups. A 120s TTL guard
on ``server_observed_at`` further prevents us from spending a background
slot on a guaranteed upstream cache hit. Two layers of "background only
when no useful normal work pending" are in effect:

  1. Cog-level: ``poll()`` ``await``s the normal-tier ``gather`` to
     completion before enqueuing any background calls. Within one tick
     of this cog, background never runs alongside foreground.
  2. Requestor-level: ``prio_deq → deq → background_deq`` strict
     fallthrough. Even if another cog enqueues a normal call while our
     background calls are in flight, the Requestor still drains higher
     tiers first on every subsequent loop iteration.

Successful detections funnel through
``lib/mc/hiatus_alerts.maybe_alert_hiatus`` (24h per-UUID cooldown,
``HIATUS_ALERTS_ENABLED`` kill-switch). The DB writes on
``last_online``/``server_observed_at`` continue regardless of the alert
toggle since they feed other features (purgelist, /info, etc.).

First observation just records the baseline; we cannot date a change we
have not yet seen happen. Asymmetric transitions involving ``None``
(``EU37 → None`` or ``None → EU37``) are treated as state-only
observations because a privacy toggle is at least as plausible as real
activity.

Cadence: 2 minutes — exactly the upstream cache TTL. Each tick all
Unknown-bucket candidates draw fresh data; polling faster would hit the
shared cache. PLAYER bucket is 50/60s, so per-tick spend (N_unknown +
the optional HIATUS background pass) sits well under bucket capacity.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from discord.ext import commands, tasks
from tortoise.expressions import Q

from bot import Bot
from lib.mc.hiatus_alerts import maybe_alert_hiatus
from lib.mc.wynn_api.player import get_player_stats
from lib.mc.wynn_api.player_models import WynncraftPlayer
from lib.role_state import hiatus_member_uuids
from orm import UNKNOWN_LAST_ONLINE, MinecraftAccount, is_last_online_unknown


def _build_stat_snapshot(player: WynncraftPlayer) -> dict[str, int | float]:
    """Flatten every monotonic counter in the /v3/player envelope into a
    flat dict, keyed by dotted-path field name. Missing/null fields are
    skipped so a privacy-hidden subset doesn't pollute deltas (a key
    absent from one tick won't register as a 'decrease' against the
    other; absent on both ticks is just not compared).

    The set is intentionally inclusive — every key here is something
    that strictly increases while the player is online. Branch B's
    activity inference ORs strict-increase across the whole snapshot,
    so adding a new counter to this function automatically extends
    detection coverage with no migration.
    """
    snap: dict[str, int | float] = {}
    if player.playtime is not None:
        snap["playtime"] = player.playtime
    g = player.globalData
    if g is None:
        return snap
    for k in ("contentCompletion", "wars", "totalLevel", "mobsKilled",
              "chestsFound", "worldEvents", "lootruns", "caves",
              "completedQuests"):
        v = getattr(g, k, None)
        if v is not None:
            snap[k] = v
    if g.dungeons is not None:
        snap["dungeons.total"] = g.dungeons.total
    if g.raids is not None:
        snap["raids.total"] = g.raids.total
    if g.raidStats is not None:
        for k in ("damageTaken", "damageDealt", "healthHealed", "deaths",
                  "buffsTaken", "gambitsUsed"):
            snap[f"raidStats.{k}"] = getattr(g.raidStats, k)
    if g.pvp is not None:
        snap["pvp.kills"] = g.pvp.kills
        snap["pvp.deaths"] = g.pvp.deaths
    return snap

logger = logging.getLogger("dazebot.cogs.activity.server_watcher")


class ServerWatcher(commands.Cog):
    bot: Bot
    _hiatus_uuid_set: set[str]

    def __init__(self, bot: Bot):
        self.bot = bot
        self._hiatus_uuid_set = set()
        self.poll.start()
        logger.info("ServerWatcher cog initialized")

    def cog_unload(self):
        self.poll.cancel()

    @tasks.loop(minutes=2)
    async def poll(self):
        # Resolve the current HIATUS-role MC UUID set once per tick;
        # ``_check_one`` consults it after a successful last_online bump
        # to decide whether to fire a hiatus-spotted alert. Also used to
        # gate the lower-priority second pass below.
        self._hiatus_uuid_set = await hiatus_member_uuids(self.bot)

        # Server-side filter: accounts whose ``lastJoin`` was observed
        # hidden at the last guild tick, OR whose ``last_online`` is still
        # the epoch sentinel (legacy / never-polled). Scope: Returners
        # (original use case) OR waitlisted — see module docstring.
        # ``waitlist`` is the reverse relation on MinecraftAccount;
        # Waitlist.minecraft_account is unique, so the join can't fan out.
        candidates = await MinecraftAccount.filter(
            (
                Q(last_online__lte=UNKNOWN_LAST_ONLINE + timedelta(days=1))
                | Q(lastjoin_hidden_at__isnull=False)
            )
            & (Q(guild="Returners") | Q(waitlist__isnull=False))
        )
        if candidates:
            logger.debug(f"polling {len(candidates)} unknown-bucket player(s)")
            await asyncio.gather(*(self._check_one(a) for a in candidates))

        # Lower-priority pass: HIATUS-role users with hidden lastJoin who
        # aren't already in the Returners/waitlist scope above. Dispatched
        # via the Requestor's background queue so non-HIATUS Unknown-bucket
        # polls (above) and any priority lookups always preempt. TTL guard
        # skips anything we polled within the upstream cache window (120s)
        # so we never spend a background slot on a guaranteed cache hit.
        if not self._hiatus_uuid_set:
            return
        now = datetime.now(timezone.utc)
        ttl_cutoff = now - timedelta(seconds=120)
        existing_uuids = {a.uuid for a in candidates}
        hiatus_candidates = await MinecraftAccount.filter(
            (
                Q(last_online__lte=UNKNOWN_LAST_ONLINE + timedelta(days=1))
                | Q(lastjoin_hidden_at__isnull=False)
            )
            & Q(uuid__in=list(self._hiatus_uuid_set))
            & ~Q(guild="Returners")
            & (Q(server_observed_at__isnull=True) | Q(server_observed_at__lt=ttl_cutoff))
        )
        hiatus_candidates = [a for a in hiatus_candidates if a.uuid not in existing_uuids]
        if not hiatus_candidates:
            return
        logger.debug(
            f"polling {len(hiatus_candidates)} hiatus unknown-bucket player(s) "
            "via background queue"
        )
        await asyncio.gather(*(self._check_one(a, background=True) for a in hiatus_candidates))

    @poll.before_loop
    async def _before_poll(self):
        # _check_one bulk-saves MinecraftAccount rows; each save fires the
        # vanity post_save signal -> bot.get_guild(), which is None until the
        # guild cache is populated at READY. Without this wait the first pre-
        # READY tick logs "Guild not found" once per polled account. Same
        # root cause and fix as activity.py's _before_check_guild.
        await self.bot.wait_until_ready()

    async def _check_one(self, account: MinecraftAccount, *, background: bool = False):
        try:
            player = await get_player_stats(account.uuid, background=background)
        except Exception:
            logger.exception(f"lookup failed for {account.mc_username}")
            return

        observed = player.server
        now = datetime.now(timezone.utc)

        # Stat-delta activity signal: every monotonic counter the envelope
        # carries is positive proof of online play even when the player
        # keeps logging into the same world (no server transition for the
        # branch below to catch). The /v3/player envelope already carries
        # all of these — no extra API spend. Any one strict increase
        # across the snapshot is enough; decreases are ignored (rare;
        # character deletion is the main cause) but the baseline is still
        # refreshed so subsequent increases will register.
        prev_snap = account.last_stat_snapshot or {}
        new_snap = _build_stat_snapshot(player)
        stat_signal = any(
            isinstance(prev_snap.get(k), (int, float)) and new_v > prev_snap[k]
            for k, new_v in new_snap.items()
        )
        # Always persist the snapshot when it differs from what we have —
        # the next tick's comparison baseline must be the latest observation.
        stat_fields: list[str] = []
        if new_snap != prev_snap:
            account.last_stat_snapshot = new_snap
            stat_fields.append("last_stat_snapshot")

        # Free-win path: player un-hid lastJoin between ticks. Adopt the
        # real timestamp directly so they clear out of the Unknown bucket
        # without needing to wait for a server transition. Same monotonic
        # discipline as ``lib/mc/wynn.py``: never roll back.
        if player.lastJoin is not None:
            fields = ["last_seen_server", "server_observed_at"] + stat_fields
            if is_last_online_unknown(account.last_online) or account.last_online < player.lastJoin:
                account.last_online = player.lastJoin
                fields.append("last_online")
            account.last_seen_server = observed
            account.server_observed_at = now
            await account.save(update_fields=fields)
            logger.info(
                f"{account.mc_username}: un-hid lastJoin ({player.lastJoin.isoformat()}); "
                "cleared from Unknown bucket"
            )
            # A hiatus user who just exposed a real lastJoin is, by
            # definition, observably online (or has been recently). Treat
            # as a spot. ``maybe_alert_hiatus`` enforces the 24h cooldown.
            if account.uuid in self._hiatus_uuid_set:
                await maybe_alert_hiatus(self.bot, account.uuid, server=observed)
            return

        prev_server = account.last_seen_server
        prev_observed_at = account.server_observed_at

        if prev_observed_at is None:
            # Baseline only — we have nothing to compare against yet.
            account.last_seen_server = observed
            account.server_observed_at = now
            await account.save(update_fields=["last_seen_server", "server_observed_at"] + stat_fields)
            logger.debug(f"{account.mc_username}: baseline server={observed!r}")
            return

        if prev_server is not None and observed is not None and observed != prev_server:
            # Two distinct non-null values across ticks => login activity in
            # (prev_observed_at, now]. ``now`` is the best lower bound we can
            # cite and is sufficient to clear the Unknown bucket.
            account.last_online = now
            account.last_seen_server = observed
            account.server_observed_at = now
            await account.save(update_fields=["last_online", "last_seen_server", "server_observed_at"] + stat_fields)
            logger.info(
                f"{account.mc_username}: server {prev_server!r} -> {observed!r}; "
                "bumping last_online=now"
            )
            if account.uuid in self._hiatus_uuid_set:
                await maybe_alert_hiatus(self.bot, account.uuid, server=observed)
            return

        # No actionable change in the server field. Touch the observation
        # timestamp so the *next* change interval starts from this poll
        # rather than the original baseline. A stat-delta signal counts
        # here as a stand-alone positive proof of activity — bump
        # last_online=now even though no server transition was seen.
        fields = ["last_seen_server", "server_observed_at"] + stat_fields
        account.last_seen_server = observed
        account.server_observed_at = now
        if stat_signal:
            account.last_online = now
            fields.append("last_online")
            # Surface which counter(s) tripped the signal — useful when
            # diagnosing why someone exited the Unknown bucket and which
            # privacy-toggle subsets remain useful for which players.
            deltas = ", ".join(
                f"{k} {prev_snap[k]}->{new_v}"
                for k, new_v in new_snap.items()
                if isinstance(prev_snap.get(k), (int, float)) and new_v > prev_snap[k]
            )
            logger.info(
                f"{account.mc_username}: stat delta ({deltas}); bumping last_online=now"
            )
            if account.uuid in self._hiatus_uuid_set:
                await maybe_alert_hiatus(self.bot, account.uuid, server=observed)
        await account.save(update_fields=fields)


async def setup(bot: Bot):
    await bot.add_cog(ServerWatcher(bot))
    logger.info("ServerWatcher cog loaded successfully")

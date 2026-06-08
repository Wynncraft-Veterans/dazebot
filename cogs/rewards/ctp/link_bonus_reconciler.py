"""Periodic reconciler that awards CTP "link bonus" points.

Three 1-point milestones, each awarded at most once per user:

* ``mc_link`` — primary Discord↔MC link set on ``DiscordAccount``
* ``vetsmod`` — non-revoked ``VerifyKey`` (issued by ``/vetsmod``)
* ``fishbot_role`` — ≥1 ``RoleCapability`` on the vets-anni side

The reconciler runs once on ``on_ready`` (the retroactive backfill the
spec calls for) and then on a recurring interval. Idempotency is
delegated to :mod:`cogs.rewards.lib.link_bonus`, which keys on
``(source='link_bonus', comment=<kind>)`` in the existing ledger — no
new tables, no flag column. Newly-linked users pick up their bonuses on
the next tick rather than via an event hook, which keeps the wiring in
one place and means a missed event (bot restart, edge-case path) still
self-heals.

Cadence is set by ``CurrConfig.LINK_BONUS_RECONCILER_HOURS`` (default 6h,
mirroring the janitor). The whole loop is gated by
``CurrConfig.LINK_BONUS_RECONCILER_ENABLED`` so it can be flipped off
without unloading the cog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from discord.ext import commands, tasks

from bot import Bot
from cogs.rewards.ctp.lib import balance as balance_svc
from cogs.rewards.ctp.lib import link_bonus
from config import Config, CurrConfig
from lib.integrations import anni_internal
from orm import DiscordAccount, VerifyKey

logger = logging.getLogger("dazebot.cogs.rewards.link_bonus_reconciler")


@dataclass
class _TickResult:
    """Per-kind tally for one reconciler pass. Logged only — no Discord
    surface; the awards themselves show up in ``~ctp history``."""

    mc_link: int = 0
    vetsmod: int = 0
    fishbot_role: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_granted(self) -> int:
        return self.mc_link + self.vetsmod + self.fishbot_role


async def _award_mc_link(result: _TickResult) -> None:
    """One ``mc_link`` bonus per ``DiscordAccount`` with a primary MC link."""
    discs = await DiscordAccount.filter(minecraft_account_id__isnull=False)
    for disc in discs:
        try:
            if await link_bonus.grant(disc, link_bonus.BONUS_MC_LINK) is not None:
                result.mc_link += 1
        except Exception:  # noqa: BLE001 — one bad row must not abort the sweep
            result.errors.append(f"mc_link disc={disc.disc_uuid}")
            logger.exception("link-bonus mc_link: failed for disc=%s", disc.disc_uuid)


async def _award_vetsmod(result: _TickResult) -> None:
    """One ``vetsmod`` bonus per non-revoked ``VerifyKey``.

    ``VerifyKey`` stores ``disc_uuid`` as a string (no FK), so we
    get-or-create the matching ``DiscordAccount`` — same convention as the
    rest of the CTP cog's ``balance_svc.get_or_create_disc``.
    """
    keys = await VerifyKey.filter(revoked_at__isnull=True).values_list(
        "disc_uuid", flat=True
    )
    for disc_uuid in keys:
        try:
            disc = await balance_svc.get_or_create_disc(disc_uuid)
            if await link_bonus.grant(disc, link_bonus.BONUS_VETSMOD) is not None:
                result.vetsmod += 1
        except Exception:  # noqa: BLE001
            result.errors.append(f"vetsmod disc={disc_uuid}")
            logger.exception("link-bonus vetsmod: failed for disc=%s", disc_uuid)


async def _award_fishbot_role(result: _TickResult) -> None:
    """One ``fishbot_role`` bonus per linked user whose ``mc_uuid`` is in
    the set vets-anni reports as having ≥1 ``RoleCapability``.

    Unreachable vets-anni → silently skip the kind for this tick (logged
    by the client). The bonus is awarded eventually on a later tick.
    """
    capability_uuids = await anni_internal.fetch_role_capability_uuids()
    if capability_uuids is None:
        return
    discs = await DiscordAccount.filter(
        minecraft_account_id__isnull=False
    ).select_related("minecraft_account")
    for disc in discs:
        mc_uuid = disc.minecraft_account.uuid if disc.minecraft_account else None
        if not link_bonus.has_fishbot_role(mc_uuid, capability_uuids):
            continue
        try:
            if await link_bonus.grant(disc, link_bonus.BONUS_FISHBOT_ROLE) is not None:
                result.fishbot_role += 1
        except Exception:  # noqa: BLE001
            result.errors.append(f"fishbot_role disc={disc.disc_uuid}")
            logger.exception(
                "link-bonus fishbot_role: failed for disc=%s", disc.disc_uuid,
            )


async def run_once() -> _TickResult:
    """One reconciliation pass. Public so ``~ctp linkbonus_run`` (if ever
    added) and tests can call it without touching the cog."""
    r = _TickResult()
    await _award_mc_link(r)
    await _award_vetsmod(r)
    await _award_fishbot_role(r)
    return r


class LinkBonusReconciler(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        if getattr(CurrConfig, "LINK_BONUS_RECONCILER_ENABLED", True):
            self.reconciler_loop.start()
            logger.info("LinkBonusReconciler initialized (loop started)")
        else:
            logger.info(
                "LinkBonusReconciler initialized (disabled — loop not started)"
            )

    def cog_unload(self):
        try:
            self.reconciler_loop.cancel()
        except RuntimeError:
            pass

    @tasks.loop(hours=Config.LINK_BONUS_RECONCILER_HOURS)
    async def reconciler_loop(self):
        await self._tick(trigger="scheduled")

    @reconciler_loop.before_loop
    async def _before(self):
        # Wait for the bot to be ready so the initial on_ready-equivalent
        # tick aligns with cog state being settled. ``tasks.loop`` fires the
        # body immediately after this returns, so this also serves as the
        # one-shot retroactive backfill mentioned in the module docstring.
        await self.bot.wait_until_ready()

    async def _tick(self, *, trigger: str) -> _TickResult:
        try:
            r = await run_once()
        except Exception:  # noqa: BLE001 — a crashed tick must not stop future ones
            logger.exception("link-bonus reconciler: tick crashed (%s)", trigger)
            return _TickResult()
        if r.total_granted or r.errors:
            logger.info(
                "link-bonus tick (%s): mc_link=+%d vetsmod=+%d fishbot_role=+%d errors=%d",
                trigger, r.mc_link, r.vetsmod, r.fishbot_role, len(r.errors),
            )
        else:
            logger.debug("link-bonus tick (%s): no awards", trigger)
        return r


async def setup(bot: Bot) -> None:
    await bot.add_cog(LinkBonusReconciler(bot))
    logger.info("LinkBonusReconciler cog loaded successfully")

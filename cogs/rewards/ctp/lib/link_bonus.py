"""CTP "link bonus" — one-time 1-point awards for crossing identity milestones.

Three milestones, each worth 1 point and awardable exactly once per user:

* ``mc_link`` — has a primary Discord↔MC link (``DiscordAccount.minecraft_account_id``)
* ``vetsmod`` — has a non-revoked ``VerifyKey`` (issued by ``/vetsmod``)
* ``fishbot_role`` — has ≥1 ``RoleCapability`` row in vets-anni (per fishbot)

These are stored as plain ``CTPLedger`` rows with ``source='link_bonus'``;
the milestone kind is carried in the ``comment`` field, which doubles as
the idempotency key. The reconciler in
``cogs/rewards/link_bonus_reconciler.py`` walks eligible users on boot and
on a recurring interval, awarding any not-yet-awarded bonus.

The ledger remains the source of truth — there is no separate
"has the bonus been awarded?" flag table. ``already_awarded`` is a single
``EXISTS`` query against the same rows ``~ctp history`` already renders.
"""

from __future__ import annotations

import logging
from typing import Optional

from cogs.rewards.ctp.lib import balance as balance_svc
from orm import CTPLedger, DiscordAccount, VerifyKey

logger = logging.getLogger("dazebot.cogs.rewards.lib.link_bonus")


BONUS_MC_LINK = "mc_link"
BONUS_VETSMOD = "vetsmod"
BONUS_FISHBOT_ROLE = "fishbot_role"

BONUS_KINDS = (BONUS_MC_LINK, BONUS_VETSMOD, BONUS_FISHBOT_ROLE)

BONUS_LABELS = {
    BONUS_MC_LINK: "Linked Minecraft account",
    BONUS_VETSMOD: "Issued vetsmod key",
    BONUS_FISHBOT_ROLE: "Declared a fishbot role",
}

BONUS_POINTS = 1

# Actor string written to ``CTPLedger.actor_disc_uuid`` — the reconciler
# isn't a user, but the column is non-null so we use a stable sentinel.
ACTOR_SYSTEM = "system:link_bonus_reconciler"


async def already_awarded(disc: DiscordAccount, kind: str) -> bool:
    """True iff a ``link_bonus`` row for ``(disc, kind)`` already exists.

    The ``(source, comment)`` pair is the de-facto idempotency key — no
    separate flag column needed.
    """
    return await CTPLedger.filter(
        discord_account=disc, source=balance_svc.SOURCE_LINK_BONUS, comment=kind
    ).exists()


async def grant(disc: DiscordAccount, kind: str) -> Optional[CTPLedger]:
    """Idempotently award the given bonus. Returns the new row, or ``None``
    if it was already on file (no double-award).
    """
    if kind not in BONUS_KINDS:
        raise ValueError(f"unknown link-bonus kind: {kind!r}")
    if await already_awarded(disc, kind):
        return None
    row = await balance_svc.append_ledger(
        disc=disc,
        amount_delta=BONUS_POINTS,
        source=balance_svc.SOURCE_LINK_BONUS,
        actor_disc_uuid=ACTOR_SYSTEM,
        comment=kind,
    )
    logger.info(
        "link_bonus granted: disc=%s kind=%s row=%s",
        disc.disc_uuid, kind, row.id,
    )
    return row


# ---------------------------------------------------------------------------
# Eligibility helpers — each returns True iff the user qualifies *right now*.
# The reconciler combines these with ``already_awarded`` to decide.
# ---------------------------------------------------------------------------


def has_mc_link(disc: DiscordAccount) -> bool:
    """A primary Discord↔MC link is the only condition. Alts don't count
    (the spec describes "linking one's discord account to minecraft",
    which is the primary-link path)."""
    return disc.minecraft_account_id is not None


async def has_vetsmod_key(disc_uuid: str) -> bool:
    """A non-revoked ``VerifyKey`` exists for this Discord user."""
    return await VerifyKey.filter(disc_uuid=disc_uuid, revoked_at__isnull=True).exists()


def has_fishbot_role(mc_uuid: Optional[str], capability_uuids: set[str]) -> bool:
    """``mc_uuid`` is in the set the vets-anni internal endpoint just returned.

    Pure function — the network fetch is in
    ``lib/integrations/anni_internal.py`` so the reconciler can do one batch
    fetch per tick instead of one HTTP round-trip per user.
    """
    if mc_uuid is None:
        return False
    return mc_uuid in capability_uuids

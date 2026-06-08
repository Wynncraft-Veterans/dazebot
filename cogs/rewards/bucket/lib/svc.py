"""DB operations for the ``~bucket`` cog.

The service layer is pure async/DB code — no ``discord.py`` types — so
it can be exercised from a REPL or unit-tested without standing up a
bot. The cog layer in :mod:`cogs.rewards.bucket.bucket` handles user /
member resolution and message rendering.

"Outstanding" means a pull row whose ``redeemed_at`` is null AND whose
``expires_at`` is still in the future. The same predicate gates
:func:`count_outstanding_by_tier`, :func:`count_outstanding_for_tier`,
and the row :func:`redeem_oldest` is willing to consume.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from orm import BucketPull, DiscordAccount


# Six months ~ 180 days. Coarse on purpose: the user-facing message
# says "your pulls expire in 6mo" — staff and users both read it as
# "about half a year", not "to the calendar day". timedelta keeps the
# code dependency-free; the codebase doesn't otherwise use dateutil.
EXPIRY = timedelta(days=180)

MIN_TIER = 1
MAX_TIER = 6


def is_valid_tier(tier: int) -> bool:
    return MIN_TIER <= tier <= MAX_TIER


async def get_or_create_disc(disc_uuid: str) -> DiscordAccount:
    """Fetch the user's DiscordAccount, creating an empty one if absent.

    Mirrors the same get-or-create pattern as
    :func:`cogs.rewards.ctp.lib.balance.get_or_create_disc` so a staff
    member can award a bucket pull to a user before that user has ever
    linked or otherwise interacted with the bot.
    """
    disc, _ = await DiscordAccount.get_or_create(disc_uuid=disc_uuid)
    return disc


async def award(
    *,
    disc: DiscordAccount,
    tier: int,
    reason: str,
    actor_disc_uuid: str,
) -> BucketPull:
    """Create a new outstanding pull. Expiry is snapshotted at creation so
    a later tweak to :data:`EXPIRY` doesn't move anyone's existing window.
    """
    return await BucketPull.create(
        discord_account=disc,
        tier=tier,
        reason=reason,
        expires_at=datetime.now(timezone.utc) + EXPIRY,
        actor_disc_uuid=actor_disc_uuid,
    )


async def count_outstanding_by_tier(disc: DiscordAccount) -> dict[int, int]:
    """Return ``{tier: count}`` of outstanding pulls for the user, omitting
    tiers with zero. Used by ``~bucket check``.
    """
    now = datetime.now(timezone.utc)
    rows = await BucketPull.filter(
        discord_account=disc,
        redeemed_at__isnull=True,
        expires_at__gt=now,
    ).all()
    counts: dict[int, int] = {}
    for r in rows:
        counts[r.tier] = counts.get(r.tier, 0) + 1
    return counts


async def count_outstanding_for_tier(tier: int) -> int:
    """Total outstanding pulls in ``tier`` across all users. Used by
    ``~bucket status``.
    """
    now = datetime.now(timezone.utc)
    return await BucketPull.filter(
        tier=tier,
        redeemed_at__isnull=True,
        expires_at__gt=now,
    ).count()


async def redeem_oldest(
    *,
    disc: DiscordAccount,
    tier: int,
    actor_disc_uuid: str,
) -> Optional[BucketPull]:
    """Mark the user's oldest outstanding pull in ``tier`` as redeemed and
    return it. ``None`` if they have no outstanding pull of that tier.

    The row is mutated in place rather than deleted so ``~bucket info``
    can show the full audit trail (who claimed what, when, by whom).
    """
    now = datetime.now(timezone.utc)
    pull = (
        await BucketPull.filter(
            discord_account=disc,
            tier=tier,
            redeemed_at__isnull=True,
            expires_at__gt=now,
        )
        .order_by("created_at")
        .first()
    )
    if pull is None:
        return None
    pull.redeemed_at = now
    pull.redeemed_by_disc_uuid = actor_disc_uuid
    await pull.save()
    return pull


async def list_history(disc: DiscordAccount) -> list[BucketPull]:
    """Every pull for the user, newest first. Powers ``~bucket info``."""
    return await BucketPull.filter(discord_account=disc).order_by("-created_at")

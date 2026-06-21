"""Service layer for the ``~donations`` cog.

Pure async + DB code — no ``discord.py`` types — so it can be exercised
without a bot. The cog at :mod:`cogs.rewards.donations.donations` handles
user resolution, embeds, and channel gating; this module owns the data.

The 5%-milestone math lives here as :func:`donation_milestone_recipients`,
which the ``/api/internal/glinted`` endpoint calls to fill slots 7-8.
Computed live from the source table — donations are editable/removable,
so a derived "milestones" table would need invalidation. At the expected
volume (tens to low hundreds per week) walking all rows is cheap.
"""

from __future__ import annotations

import json
from typing import Optional

from orm import Donation, MinecraftAccount

DEFAULT_RECENT_LIMIT = 100
DEFAULT_HIGHEST_LIMIT = 200

# Donation qualifies as a "glint milestone" when value_emeralds is at least
# this fraction of the recipient's cumulative-received total (including the
# donation itself). 5% per the spec.
MILESTONE_NUMERATOR = 1
MILESTONE_DENOMINATOR = 20  # 5% = 1/20


async def record_donation(
    *,
    recipient: MinecraftAccount,
    value_emeralds: int,
    comment: Optional[str],
    image_urls: list[str],
    recorder_disc_uuid: str,
) -> Donation:
    """Insert a Donation row.

    ``image_urls`` is serialised to JSON; an empty list is stored as ``None``
    (matches the ``CTPLedger.comment`` null-when-empty pattern).
    """
    return await Donation.create(
        recipient_mc=recipient,
        value_emeralds=value_emeralds,
        comment=comment or None,
        image_urls_json=json.dumps(image_urls) if image_urls else None,
        recorder_disc_uuid=recorder_disc_uuid,
    )


async def get_donation(donation_id: int) -> Optional[Donation]:
    """Fetch one Donation by PK with the recipient prefetched."""
    return await Donation.filter(id=donation_id).select_related("recipient_mc").first()


async def list_recent(limit: int = DEFAULT_RECENT_LIMIT) -> list[Donation]:
    """Newest-first list of recent donations, with recipient prefetched."""
    return list(
        await Donation.all()
        .order_by("-created_at")
        .limit(limit)
        .select_related("recipient_mc")
    )


async def leaderboard_totals() -> list[tuple[MinecraftAccount, int]]:
    """Cumulative donation totals per recipient, biggest first.

    One row per recipient. Computed by walking all donations in Python —
    Tortoise's ``annotate(Sum)`` + group_by doesn't return the related
    ``MinecraftAccount`` in a single query without extra ceremony, and the
    table is small enough that an in-memory aggregation is cheaper than
    the second round-trip.
    """
    donations = await Donation.all().select_related("recipient_mc")
    totals: dict[str, tuple[MinecraftAccount, int]] = {}
    for d in donations:
        mc = d.recipient_mc
        existing = totals.get(mc.id)
        new_total = (existing[1] if existing else 0) + d.value_emeralds
        totals[mc.id] = (mc, new_total)
    return sorted(totals.values(), key=lambda t: t[1], reverse=True)


async def leaderboard_totals_for_mc_ids(
    mc_ids,
) -> list[tuple[MinecraftAccount, int]]:
    """Same shape as :func:`leaderboard_totals`, restricted to recipients
    whose ``MinecraftAccount.id`` is in ``mc_ids``.

    Empty ``mc_ids`` returns ``[]`` rather than degenerating into "all
    recipients" — the caller (``/api/internal/glinted`` slot 6) is asking
    "of these online accounts, who has received the most", and an empty
    online set means "nobody is online", not "show everyone".
    """
    if not mc_ids:
        return []
    donations = await Donation.filter(
        recipient_mc_id__in=list(mc_ids)
    ).select_related("recipient_mc")
    totals: dict = {}
    for d in donations:
        mc = d.recipient_mc
        existing = totals.get(mc.id)
        new_total = (existing[1] if existing else 0) + d.value_emeralds
        totals[mc.id] = (mc, new_total)
    return sorted(totals.values(), key=lambda t: t[1], reverse=True)


async def highest_donations(limit: int = DEFAULT_HIGHEST_LIMIT) -> list[Donation]:
    """Single donations ordered by value desc; recipients may repeat."""
    return list(
        await Donation.all()
        .order_by("-value_emeralds")
        .limit(limit)
        .select_related("recipient_mc")
    )


async def edit_donation_value(donation: Donation, new_value: int) -> Donation:
    donation.value_emeralds = new_value
    await donation.save(update_fields=["value_emeralds"])
    return donation


async def edit_donation_comment(
    donation: Donation, new_comment: Optional[str]
) -> Donation:
    donation.comment = new_comment or None
    await donation.save(update_fields=["comment"])
    return donation


async def remove_donation(donation: Donation) -> None:
    """Delete a Donation row outright.

    The spec calls this an ``edit`` operation, but a hard delete keeps the
    leaderboard / milestone recomputation honest — the alternative would
    be a "voided" flag and filtering everywhere. Caller must gate with
    a ConfirmView.
    """
    await donation.delete()


async def donation_milestone_recipients(limit: int = 2) -> list[MinecraftAccount]:
    """Return the ``limit`` distinct recipients whose latest qualifying
    donation has the latest ``created_at``.

    A donation qualifies as a "milestone" when
    ``value_emeralds * MILESTONE_DENOMINATOR >= cumulative_total`` for the
    recipient at that point in time (i.e. value is at least 5% of their
    cumulative-received-so-far including this donation). The first
    donation any user receives always qualifies (100% of cumulative).

    Computation is live: load every donation in chronological order, walk
    per-recipient cumulative, mark the row's qualification, and track each
    recipient's latest qualifying timestamp. Sort recipients by that
    timestamp descending and take the top ``limit``.

    Order is stable: ties on ``created_at`` are broken by ``id`` desc
    (since auto-increment IntField rows monotonically increase).
    """
    donations = await Donation.all().order_by("created_at", "id").select_related("recipient_mc")

    cumulative: dict[str, int] = {}
    latest_qualifying: dict[str, tuple] = {}  # mc_id -> (created_at, id, mc)

    for d in donations:
        mc = d.recipient_mc
        mc_id = mc.id
        new_cum = cumulative.get(mc_id, 0) + d.value_emeralds
        cumulative[mc_id] = new_cum
        # Qualifies if value * 20 >= cumulative (i.e. value >= 5% of cumulative).
        # Guard against zero-value cumulatives (shouldn't happen — donations
        # are positive — but defensive).
        if new_cum > 0 and d.value_emeralds * MILESTONE_DENOMINATOR >= new_cum:
            latest_qualifying[mc_id] = (d.created_at, d.id, mc)

    ranked = sorted(
        latest_qualifying.values(),
        key=lambda t: (t[0], t[1]),
        reverse=True,
    )
    return [mc for (_ts, _id, mc) in ranked[:limit]]


def parse_image_urls(d: Donation) -> list[str]:
    """Decode ``image_urls_json`` to a list of URLs. ``[]`` when null."""
    if not d.image_urls_json:
        return []
    try:
        result = json.loads(d.image_urls_json)
    except (ValueError, TypeError):
        return []
    return result if isinstance(result, list) else []

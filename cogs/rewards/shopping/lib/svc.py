"""Service layer for the ``~shopping`` cog.

Pure async + DB code — no ``discord.py`` types — so it can be exercised
without a bot. The cog at :mod:`cogs.rewards.shopping.shopping` handles
user resolution, embeds, and channel gating; this module owns the data.

Shape mirrors :mod:`cogs.rewards.donations.lib.svc` deliberately, so
that reviewers familiar with the donations service layer can navigate
here without a fresh mental model. Two intentional divergences:

* :func:`void_donation` (soft) replaces the donations ``remove_donation``
  (hard delete). The soft-void keeps the audit trail and lets us
  restore the fulfilling qty to the parent request atomically.
* :func:`append_adjustment` writes to the append-only
  :class:`ShoppingRequestAdjustment` log alongside the denormalized
  ``qty_remaining`` mutation. The log is authoritative; the field
  is a materialized view.

:func:`top_monthly_shopping_donor_online` is called by
``/api/internal/glinted`` slot 5. Rolling 30-day window; excludes
voided donations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from config import CurrConfig
from orm import (
    MinecraftAccount,
    ShoppingDonation,
    ShoppingRequest,
    ShoppingRequestAdjustment,
)


MONTHLY_WINDOW_DAYS = 30

# Over-request credit: 1/5 = 20% of the request's unit value applies to
# any qty a donor delivers past the request's remaining count.
OVER_REQUEST_NUMERATOR = 1
OVER_REQUEST_DENOMINATOR = 5


class ShoppingError(RuntimeError):
    """User-facing error raised from service functions when the caller
    tried to do something the data won't allow (e.g. an ``adjust`` that
    would leave ``qty_remaining`` negative). Message is safe to reply.
    """


# --- Requests -------------------------------------------------------------


async def create_request(
    *,
    item_name: str,
    item_name_lower: str,
    unit_value_emeralds: int,
    qty: int,
    requester_disc_uuid: str,
    comment: Optional[str],
) -> ShoppingRequest:
    return await ShoppingRequest.create(
        item_name=item_name,
        item_name_lower=item_name_lower,
        unit_value_emeralds=unit_value_emeralds,
        qty_initial=qty,
        qty_remaining=qty,
        requester_disc_uuid=requester_disc_uuid,
        comment=comment or None,
    )


async def get_request(request_id: int) -> Optional[ShoppingRequest]:
    return await ShoppingRequest.filter(id=request_id).first()


async def find_open_request_for_item(item_name_lower: str) -> Optional[ShoppingRequest]:
    """Oldest open request matching the (already lowercased) item name.

    Prefers ``qty_remaining > 0`` outstanding requests over ``qty_remaining
    == 0`` wishlist entries, so a fresh donation fulfils real demand
    first and only spills over to the always-open wishlist row when no
    outstanding request exists. Within each bucket, oldest-first so
    donors fill the queue in the order staff posted it. Returns ``None``
    when no open request matches.
    """
    outstanding = (
        await ShoppingRequest
        .filter(
            item_name_lower=item_name_lower,
            closed_at__isnull=True,
            qty_remaining__gt=0,
        )
        .order_by("created_at", "id")
        .first()
    )
    if outstanding is not None:
        return outstanding
    return (
        await ShoppingRequest
        .filter(
            item_name_lower=item_name_lower,
            closed_at__isnull=True,
            qty_remaining=0,
        )
        .order_by("created_at", "id")
        .first()
    )


async def list_open_requests() -> list[ShoppingRequest]:
    # qty_remaining == 0 rows are "wishlist" entries (see create_request):
    # staff still credit overflow donations against them via
    # find_open_request_for_item, but they're hidden from the public-ish
    # ~shopping list so members don't see satisfied items in the queue.
    return list(
        await ShoppingRequest
        .filter(closed_at__isnull=True, qty_remaining__gt=0)
        .order_by("created_at", "id")
    )


async def edit_request_item(
    request: ShoppingRequest, new_item_name: str, new_item_name_lower: str
) -> ShoppingRequest:
    request.item_name = new_item_name
    request.item_name_lower = new_item_name_lower
    await request.save(update_fields=["item_name", "item_name_lower", "updated_at"])
    return request


async def edit_request_value(
    request: ShoppingRequest, new_unit_value_emeralds: int
) -> ShoppingRequest:
    """Change the request's per-unit value for future donations.

    Does NOT rewrite historical ``ShoppingDonation.unit_value_emeralds_at_time``
    — those snapshots are frozen at record time so that a value edit
    can't retroactively rewrite credit that's already been given.
    """
    request.unit_value_emeralds = new_unit_value_emeralds
    await request.save(update_fields=["unit_value_emeralds", "updated_at"])
    return request


async def edit_request_comment(
    request: ShoppingRequest, new_comment: Optional[str]
) -> ShoppingRequest:
    request.comment = new_comment or None
    await request.save(update_fields=["comment", "updated_at"])
    return request


# --- Adjustments (append-only log) ----------------------------------------


async def append_adjustment(
    *,
    request: ShoppingRequest,
    delta_qty: int,
    reason: Optional[str],
    actor_disc_uuid: str,
) -> ShoppingRequestAdjustment:
    """Append a qty adjustment to a request. Bumps denormalized
    ``qty_remaining``; auto-closes when it reaches 0, auto-reopens when
    it climbs back above 0.

    Raises :class:`ShoppingError` if the resulting ``qty_remaining``
    would be negative (staff mis-typed the delta).
    """
    new_remaining = request.qty_remaining + delta_qty
    if new_remaining < 0:
        raise ShoppingError(
            f"Adjustment would drop qty_remaining to {new_remaining} "
            f"(currently {request.qty_remaining}). Refusing."
        )

    adj = await ShoppingRequestAdjustment.create(
        request=request,
        delta_qty=delta_qty,
        reason=reason or None,
        actor_disc_uuid=actor_disc_uuid,
    )

    request.qty_remaining = new_remaining
    if new_remaining == 0 and request.closed_at is None:
        request.closed_at = datetime.now(timezone.utc)
        await request.save(update_fields=["qty_remaining", "closed_at", "updated_at"])
    elif new_remaining > 0 and request.closed_at is not None:
        request.closed_at = None
        await request.save(update_fields=["qty_remaining", "closed_at", "updated_at"])
    else:
        await request.save(update_fields=["qty_remaining", "updated_at"])

    return adj


# --- Donations ------------------------------------------------------------


def _compute_split(qty: int, qty_remaining: int, unit_value: int) -> tuple[int, int]:
    """Given (delivered qty, request's current qty_remaining, unit value),
    return ``(qty_at_full_value, value_emeralds)``.

    Full-value share fulfils the outstanding request; any excess counts
    at :data:`OVER_REQUEST_NUMERATOR` / :data:`OVER_REQUEST_DENOMINATOR`
    of unit value (20% by default). Integer floor on the over-request
    portion — donors don't get fractional emeralds.

    When ``CurrConfig.SHOPPING_OVERFLOW_PENALTY_ENABLED`` is False, the
    penalty is bypassed and overflow qty is credited at full value. The
    check is done at record time (not stored), so a later flip does not
    retro-rewrite past donations.
    """
    qty_at_full = min(qty, max(0, qty_remaining))
    qty_over = qty - qty_at_full
    if CurrConfig.SHOPPING_OVERFLOW_PENALTY_ENABLED:
        overflow_value = (
            qty_over * unit_value * OVER_REQUEST_NUMERATOR
        ) // OVER_REQUEST_DENOMINATOR
    else:
        overflow_value = qty_over * unit_value
    value = qty_at_full * unit_value + overflow_value
    return qty_at_full, value


async def record_donation(
    *,
    request: ShoppingRequest,
    recipient: MinecraftAccount,
    qty: int,
    comment: Optional[str],
    recorder_disc_uuid: str,
) -> ShoppingDonation:
    """Insert a shopping donation against ``request``, split value, and
    decrement the request's ``qty_remaining`` (auto-closing on 0).

    ``qty`` must be strictly positive; caller validates.
    """
    qty_at_full, value = _compute_split(
        qty=qty,
        qty_remaining=request.qty_remaining,
        unit_value=request.unit_value_emeralds,
    )

    donation = await ShoppingDonation.create(
        request=request,
        recipient_mc=recipient,
        qty=qty,
        unit_value_emeralds_at_time=request.unit_value_emeralds,
        qty_at_full_value=qty_at_full,
        value_emeralds=value,
        comment=comment or None,
        recorder_disc_uuid=recorder_disc_uuid,
    )

    if qty_at_full > 0:
        request.qty_remaining -= qty_at_full
        if request.qty_remaining == 0 and request.closed_at is None:
            request.closed_at = datetime.now(timezone.utc)
            await request.save(
                update_fields=["qty_remaining", "closed_at", "updated_at"]
            )
        else:
            await request.save(update_fields=["qty_remaining", "updated_at"])

    return donation


async def get_donation(donation_id: int) -> Optional[ShoppingDonation]:
    return (
        await ShoppingDonation
        .filter(id=donation_id)
        .select_related("recipient_mc", "request")
        .first()
    )


async def void_donation(
    *,
    donation: ShoppingDonation,
    voided_by_disc_uuid: str,
    reason: Optional[str],
) -> ShoppingDonation:
    """Soft-void a donation: mark it voided, restore its
    ``qty_at_full_value`` to the parent request's ``qty_remaining``,
    and auto-reopen the request if it was closed.

    Idempotent — voiding an already-voided donation is a no-op that
    returns the row unchanged.
    """
    if donation.voided_at is not None:
        return donation

    donation.voided_at = datetime.now(timezone.utc)
    donation.voided_by_disc_uuid = voided_by_disc_uuid
    donation.voided_reason = reason or None
    await donation.save(
        update_fields=["voided_at", "voided_by_disc_uuid", "voided_reason"]
    )

    if donation.qty_at_full_value > 0 and donation.request_id is not None:
        request = await ShoppingRequest.filter(id=donation.request_id).first()
        if request is not None:
            request.qty_remaining += donation.qty_at_full_value
            if request.closed_at is not None:
                request.closed_at = None
            await request.save(
                update_fields=["qty_remaining", "closed_at", "updated_at"]
            )

    return donation


async def edit_donation_comment(
    donation: ShoppingDonation, new_comment: Optional[str]
) -> ShoppingDonation:
    donation.comment = new_comment or None
    await donation.save(update_fields=["comment"])
    return donation


# --- History / info -------------------------------------------------------


async def request_history(
    request_id: int,
) -> tuple[list[ShoppingRequestAdjustment], list[ShoppingDonation]]:
    """Return the ordered history for a request: adjustments and donations
    (both including voided rows), oldest-first.

    Used by ``~shopping info <id>`` — voided donations are surfaced so
    the audit trail is complete; the format layer dims them.
    """
    adjustments = list(
        await ShoppingRequestAdjustment
        .filter(request_id=request_id)
        .order_by("created_at", "id")
    )
    donations = list(
        await ShoppingDonation
        .filter(request_id=request_id)
        .order_by("created_at", "id")
        .select_related("recipient_mc")
    )
    return adjustments, donations


# --- Leaderboard / glint --------------------------------------------------


async def leaderboard_totals() -> list[tuple[MinecraftAccount, int]]:
    """All-time cumulative shopping donation value per recipient,
    biggest first. Excludes voided donations.
    """
    donations = (
        await ShoppingDonation
        .filter(voided_at__isnull=True)
        .select_related("recipient_mc")
    )
    totals: dict = {}
    for d in donations:
        mc = d.recipient_mc
        if mc is None:
            continue
        existing = totals.get(mc.id)
        new_total = (existing[1] if existing else 0) + d.value_emeralds
        totals[mc.id] = (mc, new_total)
    return sorted(totals.values(), key=lambda t: t[1], reverse=True)


async def top_monthly_shopping_donor_online(
    online_mc_ids,
) -> Optional[MinecraftAccount]:
    """Top shopping-donation recipient over the rolling 30-day window,
    restricted to ``online_mc_ids``.

    Empty ``online_mc_ids`` returns ``None`` — the caller
    (``/api/internal/glinted`` slot 5) is asking "of these online
    accounts, who has received the most this month", and no online
    set means no candidate. No fallthrough: this returns exactly one
    account (or ``None``); the endpoint owns eligibility + dedup.

    Deterministic tiebreak by lowest ``MinecraftAccount.id`` so that
    two donors on identical totals don't flap between endpoint calls.
    """
    if not online_mc_ids:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=MONTHLY_WINDOW_DAYS)
    donations = (
        await ShoppingDonation
        .filter(
            recipient_mc_id__in=list(online_mc_ids),
            voided_at__isnull=True,
            created_at__gte=cutoff,
        )
        .select_related("recipient_mc")
    )
    if not donations:
        return None

    totals: dict = {}
    for d in donations:
        mc = d.recipient_mc
        if mc is None:
            continue
        existing = totals.get(mc.id)
        new_total = (existing[1] if existing else 0) + d.value_emeralds
        totals[mc.id] = (mc, new_total)
    if not totals:
        return None

    # Sort by (-total, mc.id) so highest total wins, ties break to lowest id.
    ranked = sorted(totals.values(), key=lambda t: (-t[1], t[0].id))
    return ranked[0][0]

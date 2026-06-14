"""Glint-investment service for ``~ctp glints bid`` / ``~ctp glints bids``.

Investments are cumulative-only: ``CTPGlintInvestment.total_invested``
can only ever increase (spec line 92). A bid both:

* writes a negative ledger row (``source='glint_invest'``) that debits
  the user's balance, AND
* increments ``CTPGlintInvestment.total_invested`` by the same amount.

The leaderboard shows only users currently holding MEMBER /
WAITLISTED / HONOURARY — HIATUS, REGISTERED, or no-state users keep
their investment row but vanish from the rank (spec lines 70–73, 96).
They pop back on when they re-enter an eligible state.

Ties on ``total_invested`` are resolved by a deterministic, wall-clock
rotation (``TIE_ROTATION_PERIOD_SECONDS``): each group of tied bidders
is sorted by a stable intra-group key and rotated by
``epoch // PERIOD mod group_size``. This propagates to both
``~ctp glints bids`` / ``~ctp status`` and the in-game shimmer via
``/api/internal/glinted``, so when a tied group straddles the cutoff
the members alternate who is "in" every 3 h instead of one being
permanently locked out by undefined SQL row order.
"""

from __future__ import annotations

import time
from typing import Optional

import discord

from cogs.rewards.ctp.lib import balance as balance_svc
from config import CurrConfig
from lib.role_state import RoleState, state_of
from orm import CTPGlintInvestment, DiscordAccount


# Spec lines 75–86: top 8 visible, position 9+ is "Standby".
GLINT_VISIBLE_CUTOFF = 8

# Tied bidders rotate through the cutoff slots on this cadence. 3h is a
# divisor of 24h, so rotation boundaries land at 00:00 / 03:00 / 06:00 …
# UTC. Vetsmod's SupportersPoller refreshes every 5 min, so the in-game
# shimmer flips within one poll of each boundary.
TIE_ROTATION_PERIOD_SECONDS = 3 * 3600


# Roles whose holders are eligible to appear on the glints leaderboard.
# Computed lazily because CurrConfig attrs may be overridden at runtime
# via the /config admin command.
def _eligible_role_ids() -> frozenset[int]:
    return frozenset({
        CurrConfig.ROLE_MEMBER,
        CurrConfig.ROLE_WAITLISTED,
        CurrConfig.ROLE_HONOURARY,
    })


def is_eligible_member(member: discord.Member) -> bool:
    """True if ``member`` currently holds one of the leaderboard-eligible
    state roles. HIATUS doesn't count — that's the whole point of the
    filter.
    """
    eligible = RoleState.MEMBER | RoleState.WAITLISTED | RoleState.HONOURARY
    return bool(state_of(member) & eligible)


async def invest(
    *,
    disc: DiscordAccount,
    amount: int,
    actor_disc_uuid: str,
) -> tuple[CTPGlintInvestment, int]:
    """Append the ledger debit + increment the investment row. Returns
    ``(investment_row, new_total)``. Caller must have balance-checked
    upstream.
    """
    inv, _ = await CTPGlintInvestment.get_or_create(
        discord_account=disc,
        defaults={"total_invested": 0},
    )
    inv.total_invested += amount
    await inv.save(update_fields=["total_invested", "updated_at"])
    await balance_svc.append_ledger(
        disc=disc,
        amount_delta=-amount,
        source=balance_svc.SOURCE_GLINT_INVEST,
        actor_disc_uuid=actor_disc_uuid,
    )
    return inv, inv.total_invested


async def _all_investments_desc() -> list[CTPGlintInvestment]:
    """Every investment row, biggest first, with ``discord_account``
    prefetched. Used by ``_ranked_eligible`` so the eligibility filter
    and the tie rotation are applied identically across surfaces.
    """
    return (
        await CTPGlintInvestment.filter(total_invested__gt=0)
        .order_by("-total_invested")
        .select_related("discord_account")
    )


def _rotation_bucket(now: Optional[float] = None) -> int:
    """Index of the current rotation window. Bumps every
    ``TIE_ROTATION_PERIOD_SECONDS`` on the wall clock so a bot restart
    doesn't reset the rotation phase.
    """
    if now is None:
        now = time.time()
    return int(now) // TIE_ROTATION_PERIOD_SECONDS


def _rotate_ties(
    eligible: list[tuple[discord.Member, int]],
    bucket: int,
) -> list[tuple[discord.Member, int]]:
    """Group runs of consecutive entries with identical ``total_invested``
    and rotate each group by ``bucket % len(group)`` after a stable
    intra-group sort by ``member.id``. Singletons pass through unchanged.
    """
    if not eligible:
        return eligible

    out: list[tuple[discord.Member, int]] = []
    i = 0
    while i < len(eligible):
        j = i + 1
        while j < len(eligible) and eligible[j][1] == eligible[i][1]:
            j += 1
        group = eligible[i:j]
        if len(group) > 1:
            group = sorted(group, key=lambda mt: mt[0].id)
            shift = bucket % len(group)
            group = group[shift:] + group[:shift]
        out.extend(group)
        i = j
    return out


async def _ranked_eligible(
    bot, *, bucket: Optional[int] = None,
) -> list[tuple[discord.Member, int]]:
    """Eligibility-filtered, tie-rotated leaderboard. Single source of
    truth for ``leaderboard()`` and ``rank_of()`` so Discord display
    and in-game shimmer can't disagree about who is currently glinted.
    Returns ``[]`` when the configured guild isn't in the bot's cache.
    """
    guild = bot.get_guild(CurrConfig.GUILD)
    if guild is None:
        return []

    rows = await _all_investments_desc()
    eligible: list[tuple[discord.Member, int]] = []
    for inv in rows:
        try:
            uid = int(inv.discord_account.disc_uuid)
        except (TypeError, ValueError):
            continue
        member = guild.get_member(uid)
        if member is None or not is_eligible_member(member):
            continue
        eligible.append((member, inv.total_invested))

    if bucket is None:
        bucket = _rotation_bucket()
    return _rotate_ties(eligible, bucket)


def find_cutoff_tied_group(
    ranked: list[tuple[discord.Member, int]],
    cutoff: int = GLINT_VISIBLE_CUTOFF,
) -> tuple[int, int, int]:
    """Inspect a ``_ranked_eligible`` result to find a tied group that
    straddles ``cutoff``. Returns ``(rank, total, size)`` of the tied
    group at the boundary, or ``(0, 0, 0)`` when no tie crosses it
    (everyone above the cutoff has a different total from everyone
    below, OR the list is shorter than ``cutoff + 1``).
    """
    if len(ranked) <= cutoff:
        return 0, 0, 0
    boundary_total = ranked[cutoff - 1][1]
    if ranked[cutoff][1] != boundary_total:
        return 0, 0, 0
    start = cutoff - 1
    while start > 0 and ranked[start - 1][1] == boundary_total:
        start -= 1
    end = cutoff
    while end + 1 < len(ranked) and ranked[end + 1][1] == boundary_total:
        end += 1
    return start + 1, boundary_total, end - start + 1


async def leaderboard(
    bot,
) -> tuple[list[tuple[discord.Member, int]], list[tuple[discord.Member, int]]]:
    """Return ``(glinted, standby)``: members ranked 1..GLINT_VISIBLE_CUTOFF
    are "Currently Glinted"; ranks GLINT_VISIBLE_CUTOFF+1.. are "Standby".

    Both lists contain only members currently holding MEMBER /
    WAITLISTED / HONOURARY (spec lines 70–73). Members who aren't in
    the configured guild's cache at all are skipped silently (typical
    when an investment row outlives the user's server membership).
    """
    ranked = await _ranked_eligible(bot)
    return ranked[:GLINT_VISIBLE_CUTOFF], ranked[GLINT_VISIBLE_CUTOFF:]


async def rank_of(
    *, disc: DiscordAccount, bot,
) -> tuple[Optional[int], int, bool, int]:
    """Return ``(rank, total_invested, is_glinted, cutoff_tie_size)``.

    ``rank`` is 1-indexed against the eligible leaderboard, or ``None``
    when the user has no investment or isn't eligible (HIATUS /
    REGISTERED / not in guild). ``is_glinted`` is True only when
    ``rank <= GLINT_VISIBLE_CUTOFF`` — used by ``~ctp status``'s
    "are/are_not glinted" line.

    ``cutoff_tie_size`` is the count of bidders in the user's tied
    group when that group straddles ``GLINT_VISIBLE_CUTOFF`` (i.e. some
    members are currently glinted and some are on standby this rotation
    bucket); ``0`` otherwise. Callers use it to surface the "tied with
    N others; rotates every 3 h" annotation.
    """
    inv = await CTPGlintInvestment.filter(discord_account=disc).first()
    total = inv.total_invested if inv else 0
    if total <= 0:
        return None, total, False, 0

    ranked = await _ranked_eligible(bot)
    try:
        target_uid = int(disc.disc_uuid)
    except (TypeError, ValueError):
        return None, total, False, 0
    user_idx: Optional[int] = None
    for idx, (member, _t) in enumerate(ranked, start=1):
        if member.id == target_uid:
            user_idx = idx
            break
    if user_idx is None:
        return None, total, False, 0
    _r, straddle_total, straddle_size = find_cutoff_tied_group(
        ranked, GLINT_VISIBLE_CUTOFF,
    )
    cutoff_tie_size = straddle_size if straddle_total == total else 0
    return user_idx, total, user_idx <= GLINT_VISIBLE_CUTOFF, cutoff_tie_size

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
"""

from __future__ import annotations

from typing import Optional

import discord

from cogs.rewards.ctp.lib import balance as balance_svc
from config import CurrConfig
from lib.role_state import RoleState, state_of
from orm import CTPGlintInvestment, DiscordAccount


# Spec lines 75–86: top 8 visible, position 9+ is "Standby".
GLINT_VISIBLE_CUTOFF = 8


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
    prefetched. Used by both the leaderboard and the per-user rank
    lookup so the eligibility filter is applied identically in both.
    """
    return (
        await CTPGlintInvestment.filter(total_invested__gt=0)
        .order_by("-total_invested")
        .select_related("discord_account")
    )


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
    guild = bot.get_guild(CurrConfig.GUILD)
    if guild is None:
        return [], []

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

    glinted = eligible[:GLINT_VISIBLE_CUTOFF]
    standby = eligible[GLINT_VISIBLE_CUTOFF:]
    return glinted, standby


async def rank_of(*, disc: DiscordAccount, bot) -> tuple[Optional[int], int, bool]:
    """Return ``(rank, total_invested, is_glinted)`` for the user.

    ``rank`` is 1-indexed against the eligible leaderboard, or ``None``
    when the user has no investment or isn't eligible (HIATUS /
    REGISTERED / not in guild). ``is_glinted`` is True only when
    ``rank <= GLINT_VISIBLE_CUTOFF`` — used by ``~ctp status``'s
    "are/are_not glinted" line.
    """
    inv = await CTPGlintInvestment.filter(discord_account=disc).first()
    total = inv.total_invested if inv else 0

    guild = bot.get_guild(CurrConfig.GUILD)
    if guild is None:
        return None, total, False
    try:
        member = guild.get_member(int(disc.disc_uuid))
    except (TypeError, ValueError):
        member = None
    if member is None or not is_eligible_member(member) or total <= 0:
        return None, total, False

    rows = await _all_investments_desc()
    rank_counter = 0
    for r in rows:
        try:
            uid = int(r.discord_account.disc_uuid)
        except (TypeError, ValueError):
            continue
        m = guild.get_member(uid)
        if m is None or not is_eligible_member(m):
            continue
        rank_counter += 1
        if uid == int(disc.disc_uuid):
            return rank_counter, total, rank_counter <= GLINT_VISIBLE_CUTOFF

    return None, total, False

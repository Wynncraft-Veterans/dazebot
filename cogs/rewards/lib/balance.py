"""Append-only point ledger operations for the ``~ctp`` cog.

The ledger is the source of truth for every CTP point movement. Balance =
``SUM(amount_delta) WHERE discord_account = X``. There is no mutable
balance column; every command that "adjusts" a balance writes a new row.

This module is pure async/DB code — no ``discord.py`` types — so it can be
exercised from a REPL and unit-tested without standing up a bot. The cog
layer in :mod:`cogs.rewards.ctp` handles user/member resolution and
embeds.
"""

from __future__ import annotations

from typing import Optional

from tortoise.functions import Sum

from orm import CTPBoard, CTPLedger, CTPPrize, DiscordAccount


# Source discriminators on CTPLedger.source. Kept here so the cog imports
# named constants instead of stringly-typing every call site.
SOURCE_REWARD = "reward"
SOURCE_REDEEM = "redeem"
SOURCE_GIFT_SENT = "gift_sent"
SOURCE_GIFT_RECEIVED = "gift_received"
SOURCE_GLINT_INVEST = "glint_invest"
SOURCE_ADMIN_SET = "admin_set"
# Awarded by ``cogs/rewards/link_bonus_reconciler.py`` for one-time
# milestones: linking MC, issuing a vetsmod key, declaring a fishbot role.
# ``comment`` carries the kind discriminator (see ``link_bonus.BONUS_KINDS``)
# and doubles as the idempotency key.
SOURCE_LINK_BONUS = "link_bonus"


async def compute_balance(disc: DiscordAccount) -> int:
    """Return the user's current CTP point balance.

    Sums the signed ``amount_delta`` of every ledger row for the account.
    A user with no rows returns 0. Implemented via Tortoise's
    ``annotate(Sum)`` so the math runs in SQLite, not Python.
    """
    row = (
        await CTPLedger.filter(discord_account=disc)
        .annotate(total=Sum("amount_delta"))
        .first()
        .values("total")
    )
    if not row or row.get("total") is None:
        return 0
    return int(row["total"])


async def compute_lifetime_received(disc: DiscordAccount) -> int:
    """Sum of all positive ledger deltas for the account.

    "Lifetime received" ignores spends (redeem / gift_sent / glint_invest)
    and any downward admin correction; it's the running total of points
    that have ever flowed *in*. Powers the threshold-role trigger, where
    crossing a fixed cumulative-received total grants a Discord role
    that's never stripped — spending points doesn't undo having received
    them.
    """
    row = (
        await CTPLedger.filter(discord_account=disc, amount_delta__gt=0)
        .annotate(total=Sum("amount_delta"))
        .first()
        .values("total")
    )
    if not row or row.get("total") is None:
        return 0
    return int(row["total"])


async def append_ledger(
    *,
    disc: DiscordAccount,
    amount_delta: int,
    source: str,
    actor_disc_uuid: str,
    board: Optional[CTPBoard] = None,
    task_number: Optional[int] = None,
    prize: Optional[CTPPrize] = None,
    prize_display_at_time: Optional[str] = None,
    prize_category_at_time: Optional[str] = None,
    expires_at=None,
    counterparty_disc_uuid: Optional[str] = None,
    comment: Optional[str] = None,
) -> CTPLedger:
    """Append a single ledger row. Returns the created row.

    All optional fields default to None so callers only pass what their
    ``source`` actually populates (e.g. a ``reward`` row needs
    ``board``/``task_number``/``comment`` but never a ``prize``).
    """
    return await CTPLedger.create(
        discord_account=disc,
        amount_delta=amount_delta,
        source=source,
        board=board,
        task_number=task_number,
        prize=prize,
        prize_display_at_time=prize_display_at_time,
        prize_category_at_time=prize_category_at_time,
        expires_at=expires_at,
        counterparty_disc_uuid=counterparty_disc_uuid,
        actor_disc_uuid=actor_disc_uuid,
        comment=comment,
    )


async def list_history(disc: DiscordAccount, *, limit: Optional[int] = None) -> list[CTPLedger]:
    """Return the user's full history, newest first, with ``board`` and
    ``prize`` prefetched for rendering. Pass ``limit`` to cap the result
    when the caller plans to paginate elsewhere.
    """
    qs = (
        CTPLedger.filter(discord_account=disc)
        .order_by("-created_at")
        .select_related("board", "prize")
    )
    if limit is not None:
        qs = qs.limit(limit)
    return await qs


async def award(
    *,
    target: DiscordAccount,
    board: Optional[CTPBoard],
    task_number: int,
    points: int,
    comment: Optional[str],
    actor_disc_uuid: str,
) -> CTPLedger:
    """Staff ``~ctp reward`` path: append a positive ledger row.

    ``task_number == 0`` is the spec-defined "N/A dummy" used when the
    reward isn't tied to a tasks.wynnvets.org task; callers pass it
    verbatim. ``board=None`` is allowed for the same reason.
    """
    return await append_ledger(
        disc=target,
        amount_delta=points,
        source=SOURCE_REWARD,
        board=board,
        task_number=task_number,
        comment=comment,
        actor_disc_uuid=actor_disc_uuid,
    )


async def transfer_points(
    *,
    sender: DiscordAccount,
    receiver: DiscordAccount,
    amount: int,
    actor_disc_uuid: str,
) -> tuple[CTPLedger, CTPLedger]:
    """Two-row gift: -amount on sender, +amount on receiver, mutual
    counterparty linkage. Caller must have already balance-checked the
    sender — this function does NOT re-validate. SQLite serialises writes
    per transaction, but the two rows are deliberately separate inserts
    so the receiver's history shows a real ``gift_received`` row rather
    than a derived view.
    """
    sent = await append_ledger(
        disc=sender,
        amount_delta=-amount,
        source=SOURCE_GIFT_SENT,
        counterparty_disc_uuid=receiver.disc_uuid,
        actor_disc_uuid=actor_disc_uuid,
    )
    received = await append_ledger(
        disc=receiver,
        amount_delta=amount,
        source=SOURCE_GIFT_RECEIVED,
        counterparty_disc_uuid=sender.disc_uuid,
        actor_disc_uuid=actor_disc_uuid,
    )
    return sent, received


async def set_balance(
    *,
    target: DiscordAccount,
    new_value: int,
    actor_disc_uuid: str,
    comment: Optional[str] = None,
) -> CTPLedger:
    """Admin ``~ctp set`` path: write a corrective delta so the user's
    summed balance lands on ``new_value`` exactly. Returns the
    just-written row. Pre-existing history rows are untouched — the
    correction shows up as its own ``admin_set`` entry in ``~ctp history``.
    """
    current = await compute_balance(target)
    delta = new_value - current
    return await append_ledger(
        disc=target,
        amount_delta=delta,
        source=SOURCE_ADMIN_SET,
        actor_disc_uuid=actor_disc_uuid,
        comment=comment,
    )


async def get_or_create_disc(disc_uuid: str) -> DiscordAccount:
    """Fetch the user's DiscordAccount, creating an empty one if absent.

    CTP creates a row on first touch (reward / status / etc.) rather than
    refusing the command, so users don't need a prior link / interaction
    for staff to start awarding them points.
    """
    disc, _ = await DiscordAccount.get_or_create(disc_uuid=disc_uuid)
    return disc

"""Prize catalog CRUD and redemption logic for the ``~ctp prize`` /
``~ctp redeem`` / ``~ctp access`` commands.

``(category, enum_name)`` is the prize lookup key. Categories are
admin-defined — there is no system-side allowlist. Any non-empty input
is titlecased on write, and subsequent lookups resolve case-
insensitively against the stored capitalisation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from tortoise.expressions import Q

from cogs.rewards.ctp.lib import balance as balance_svc
from orm import CTPLedger, CTPPrize, DiscordAccount


def normalize_category(value: str) -> Optional[str]:
    """Titlecase ``value`` for storage / lookup. Returns None only when
    the input is empty/whitespace. Categories are entirely admin-
    defined: ``~ctp prize add <NewCategory> <ENUM> ...`` mints a fresh
    one on first use, and ``~ctp prize edit newcategory.ENUM ...``
    resolves back to it via the same titlecase rule.
    """
    s = value.strip()
    if not s:
        return None
    return s[0].upper() + s[1:].lower()


def parse_dotted_key(arg: str) -> Optional[tuple[str, str]]:
    """Parse the ``<category.enum>`` shorthand used by ``~ctp prize edit``
    / ``disable`` / ``remove`` / ``disclaim`` (e.g. ``GIFT.NAZ``). Returns
    ``(category_canonical, enum_uppercase)`` or None on a malformed input.
    """
    if "." not in arg:
        return None
    cat, _, enum = arg.partition(".")
    canon = normalize_category(cat)
    if canon is None or not enum:
        return None
    return canon, enum.strip().upper()


async def find(category: str, enum_name: str) -> Optional[CTPPrize]:
    """Exact match on the unique pair. ``category`` may be in any case;
    ``enum_name`` is compared case-insensitively too so admins can type
    ``chiefs_corner`` instead of ``CHIEFS_CORNER``.
    """
    canon = normalize_category(category)
    if canon is None:
        return None
    return await CTPPrize.filter(category=canon, enum_name__iexact=enum_name).first()


async def resolve_loose(category: str, enum_arg: str) -> Optional[CTPPrize]:
    """Looser lookup used by ``~ctp redeem``: an exact ``enum_name`` match
    first, then a case-insensitive substring match within the category.
    Lets staff type ``~ctp redeem @user access chief`` and have it find
    ``CHIEFS_CORNER`` without memorising the full enum.
    """
    exact = await find(category, enum_arg)
    if exact is not None:
        return exact
    canon = normalize_category(category)
    if canon is None:
        return None
    return (
        await CTPPrize.filter(category=canon)
        .filter(Q(enum_name__icontains=enum_arg) | Q(display__icontains=enum_arg))
        .first()
    )


async def all_visible(include_disabled: bool = False) -> list[CTPPrize]:
    """Prizes ordered alphabetically by category then enum_name. Used
    by ``~ctp prize info``. With ``include_disabled=True`` (admin view)
    the ``disabled=False`` filter is dropped so admins can see hidden
    rows in the same paginated embed.
    """
    rows = (
        await CTPPrize.all()
        if include_disabled
        else await CTPPrize.filter(disabled=False)
    )
    rows.sort(key=lambda p: (p.category, p.enum_name))
    return rows


async def resolve_targets(
    category_enum: str,
) -> tuple[list[CTPPrize], Optional[str]]:
    """Resolve the ``<category.enum>`` argument used by the bulk-capable
    admin commands (``~ctp prize disable`` / ``enable`` / ``remove`` /
    ``disclaim``) into the list of prizes to operate on.

    * ``Access.CHIEFS_CORNER`` → ``([single_prize], None)`` if it exists,
      else ``([], "No prize ...")``.
    * ``Access.*`` → every prize in the (canonicalised) category,
      regardless of ``disabled``. Empty category returns an error so the
      caller can tell the admin nothing happened.
    * Anything else (no dot, unknown category, ``*`` enum on bad
      category, etc.) returns ``([], "Could not parse ...")``.
    """
    parsed = parse_dotted_key(category_enum)
    if parsed is None:
        return (
            [],
            f"Could not parse `{category_enum}` — expected `CATEGORY.ENUM` or `CATEGORY.*`.",
        )
    cat, enum_name = parsed
    if enum_name == "*":
        rows = await CTPPrize.filter(category=cat).order_by("enum_name")
        if not rows:
            return [], f"No prizes in `{cat}` to operate on."
        return list(rows), None
    prize = await find(cat, enum_name)
    if prize is None:
        return [], f"No prize `{cat}.{enum_name}`."
    return [prize], None


async def create(
    *,
    category: str,
    enum_name: str,
    cost: int,
    duration_seconds: Optional[int],
    display: str,
) -> Optional[CTPPrize]:
    """Insert a new prize. Returns None when the category is unknown or
    when ``(category, enum_name)`` already exists (the unique_together
    constraint fires; callers should report the duplicate to the admin).
    """
    canon = normalize_category(category)
    if canon is None:
        return None
    existing = await find(canon, enum_name)
    if existing is not None:
        return None
    return await CTPPrize.create(
        category=canon,
        enum_name=enum_name.upper(),
        cost=cost,
        duration_seconds=duration_seconds,
        display=display,
        disclaimer=None,
        disabled=False,
    )


async def edit_field(prize: CTPPrize, field: str, raw_value: str) -> tuple[bool, str]:
    """Apply ``~ctp prize edit <category.enum> <field> <value>``.

    Supported fields per spec line 167: ``display`` | ``cost`` |
    ``duration``. Returns ``(ok, message)`` — the cog uses ``message``
    verbatim in its reply.
    """
    fld = field.lower()
    if fld == "display":
        prize.display = raw_value
        await prize.save(update_fields=["display", "updated_at"])
        return True, f"Display for `{prize.category}.{prize.enum_name}` updated."
    if fld == "cost":
        try:
            new_cost = int(raw_value)
        except ValueError:
            return False, f"`{raw_value}` is not a valid integer cost."
        prize.cost = new_cost
        await prize.save(update_fields=["cost", "updated_at"])
        return True, f"Cost for `{prize.category}.{prize.enum_name}` set to {new_cost}."
    if fld == "duration":
        if raw_value == "0" or raw_value.lower() in {"none", "null", "one-time"}:
            prize.duration_seconds = None
        else:
            try:
                prize.duration_seconds = int(raw_value)
            except ValueError:
                return False, f"`{raw_value}` is not a valid integer duration (seconds, or `0` for one-time)."
        await prize.save(update_fields=["duration_seconds", "updated_at"])
        human = "one-time" if prize.duration_seconds is None else f"{prize.duration_seconds}s"
        return True, f"Duration for `{prize.category}.{prize.enum_name}` set to {human}."
    return False, f"Unknown field `{field}` — expected `display`, `cost`, or `duration`."


async def set_disabled(prize: CTPPrize, value: bool) -> CTPPrize:
    """Toggle the prize's visibility / redeemability. Disabled prizes are
    hidden from ``~ctp prize info`` and refused by ``~ctp redeem`` but
    their ledger snapshots survive unaffected.
    """
    prize.disabled = value
    await prize.save(update_fields=["disabled", "updated_at"])
    return prize


async def set_disclaimer(prize: CTPPrize, msg: Optional[str]) -> CTPPrize:
    """Set / clear the disclaimer string shown alongside the prize in
    ``~ctp prize info`` (spec lines 176–179). Pass ``None`` or an empty
    string to clear.
    """
    prize.disclaimer = msg or None
    await prize.save(update_fields=["disclaimer", "updated_at"])
    return prize


async def delete(prize: CTPPrize) -> None:
    """Hard-delete the catalog row. Ledger rows that referenced this
    prize have their FK set to NULL by the schema, but their
    ``prize_display_at_time`` / ``prize_category_at_time`` snapshots
    keep history intact.
    """
    await prize.delete()


@dataclass(frozen=True)
class RedemptionResult:
    """Outcome of :func:`redeem`. ``ok=False`` carries an explanation in
    ``message``; ``ok=True`` carries the just-written ledger row.
    """

    ok: bool
    message: str
    entry: Optional[CTPLedger] = None


async def redeem(
    *,
    target: DiscordAccount,
    prize: CTPPrize,
    actor_disc_uuid: str,
) -> RedemptionResult:
    """Staff ``~ctp redeem`` path.

    Validates the target's balance against ``prize.cost``, refuses on
    insufficient funds or on a disabled prize, otherwise writes a single
    ledger row with the prize fields snapshotted (so a later prize edit /
    delete leaves the history record intact) and ``expires_at`` computed
    from ``prize.duration_seconds + now`` when the prize is time-bound.
    """
    if prize.disabled:
        return RedemptionResult(False, f"Prize `{prize.category}.{prize.enum_name}` is disabled.")
    current = await balance_svc.compute_balance(target)
    if current < prize.cost:
        return RedemptionResult(
            False,
            f"Insufficient balance: user has {current} points, prize costs {prize.cost}.",
        )
    expires_at = None
    if prize.duration_seconds is not None and prize.duration_seconds > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=prize.duration_seconds)
    entry = await balance_svc.append_ledger(
        disc=target,
        amount_delta=-prize.cost,
        source=balance_svc.SOURCE_REDEEM,
        prize=prize,
        prize_display_at_time=prize.display,
        prize_category_at_time=prize.category,
        expires_at=expires_at,
        actor_disc_uuid=actor_disc_uuid,
    )
    return RedemptionResult(True, f"Redeemed `{prize.display}` for {prize.cost} points.", entry)


async def active_redemptions() -> list[CTPLedger]:
    """All currently-active (unexpired) time-bound redemptions, oldest
    first, with ``discord_account`` prefetched. ``~ctp access`` groups
    these by ``prize_display_at_time`` for the per-prize "who's inside"
    list. Rows without an ``expires_at`` (one-time prizes) are excluded
    by design — they never appear here.
    """
    now = datetime.now(timezone.utc)
    return (
        await CTPLedger.filter(
            source=balance_svc.SOURCE_REDEEM,
            expires_at__isnull=False,
            expires_at__gt=now,
        )
        .order_by("expires_at")
        .select_related("discord_account")
    )

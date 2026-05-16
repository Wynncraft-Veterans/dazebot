"""Role-state machine for VETS membership.

Implements the transition table from ``../.claude/membership_spec.md`` \u00a76.
Pure functions where possible; the apply step takes a ``discord.Member`` and
the desired action enum, computes the role delta, and performs it.

States are derived from which of the five state-roles a member currently has.
"""

from __future__ import annotations

import enum
import logging
from typing import Iterable

import discord

from config import CurrConfig

logger = logging.getLogger("dazebot.lib.role_state")


class RoleState(enum.Flag):
    NONE = 0
    REGISTERED = enum.auto()
    HIATUS = enum.auto()
    MEMBER = enum.auto()
    HONOURARY = enum.auto()
    WAITLISTED = enum.auto()


class Trigger(enum.Enum):
    ADDED_TO_WAITLIST = "added_to_waitlist"
    JOINED_VETS = "joined_vets"
    BECAME_GUILDLESS = "became_guildless"
    INACTIVE_MEMBER = "inactive_member"  # x days
    INACTIVE_WAITLIST = "inactive_waitlist"  # y days
    JOINED_OTHER_GUILD = "joined_other_guild"


_STATE_ROLE_MAP = {
    RoleState.REGISTERED: lambda: CurrConfig.ROLE_REGISTERED,
    RoleState.HIATUS: lambda: CurrConfig.ROLE_HIATUS,
    RoleState.MEMBER: lambda: CurrConfig.ROLE_MEMBER,
    RoleState.HONOURARY: lambda: CurrConfig.ROLE_HONOURARY,
    RoleState.WAITLISTED: lambda: CurrConfig.ROLE_WAITLISTED,
}


def _all_state_role_ids() -> set[int]:
    return {fn() for fn in _STATE_ROLE_MAP.values()}


def state_of(member: discord.Member) -> RoleState:
    """Compute the current ``RoleState`` of a guild member from their roles."""
    held_role_ids = {r.id for r in member.roles}
    state = RoleState.NONE
    for s, fn in _STATE_ROLE_MAP.items():
        if fn() in held_role_ids:
            state |= s
    return state


def _state_to_role_ids(state: RoleState) -> set[int]:
    return {fn() for s, fn in _STATE_ROLE_MAP.items() if s in state}


class TransitionResult:
    __slots__ = ("error", "to_add", "to_remove", "side_effects")

    def __init__(
        self,
        *,
        error: str | None = None,
        to_add: Iterable[int] = (),
        to_remove: Iterable[int] = (),
        side_effects: Iterable[str] = (),
    ):
        self.error = error
        self.to_add = set(to_add)
        self.to_remove = set(to_remove)
        self.side_effects = list(side_effects)

    @property
    def is_error(self) -> bool:
        return self.error is not None

    @property
    def is_noop(self) -> bool:
        return not self.to_add and not self.to_remove and not self.error


def compute_transition(state: RoleState, trigger: Trigger) -> TransitionResult:
    """Compute the role delta for a given current state + trigger.

    Mirrors the table in ../.claude/membership_spec.md \u00a76 verbatim. Anything
    not listed explicitly is a no-op (returns empty result, not an error).
    """
    REG = RoleState.REGISTERED
    HIA = RoleState.HIATUS
    MEM = RoleState.MEMBER
    HON = RoleState.HONOURARY
    WL = RoleState.WAITLISTED

    has = lambda *flags: all(f in state for f in flags)

    role = _STATE_ROLE_MAP

    # --- ADDED_TO_WAITLIST ---
    if trigger == Trigger.ADDED_TO_WAITLIST:
        # Error states: anyone already waitlisted, or currently a Member
        if WL in state:
            return TransitionResult(error="User is already waitlisted.")
        if MEM in state:
            return TransitionResult(error="User is currently a Member \u2014 cannot waitlist.")
        if has(REG) or has(HIA) or has(HON):
            return TransitionResult(to_add={role[WL]()})
        return TransitionResult(error="User has no membership state \u2014 cannot waitlist.")

    # --- JOINED_VETS ---
    if trigger == Trigger.JOINED_VETS:
        if MEM in state:
            return TransitionResult(error="User is already a Member.")
        adds: set[int] = {role[MEM]()}
        removes: set[int] = set()
        if WL in state:
            removes.add(role[WL]())
        if REG in state:
            removes.add(role[REG]())
        if HIA in state:
            removes.add(role[HIA]())
        if HON in state:
            removes.add(role[HON]())
        if not (adds - {role[r]() for r in (REG, HIA, MEM, HON, WL) if r in state}):
            # we're adding member; they had nothing before \u2014 still ok
            pass
        return TransitionResult(to_add=adds, to_remove=removes)

    # --- BECAME_GUILDLESS ---
    if trigger == Trigger.BECAME_GUILDLESS:
        if HIA in state:
            return TransitionResult(error="Hiatus user is already guildless (state error).")
        if MEM in state:
            return TransitionResult(to_remove={role[MEM]()}, to_add={role[HIA]()})
        return TransitionResult()  # do nothing

    # --- INACTIVE_MEMBER (DM warning, no role change) ---
    if trigger == Trigger.INACTIVE_MEMBER:
        if MEM in state:
            return TransitionResult(side_effects=["dm_inactive_warning"])
        return TransitionResult()

    # --- INACTIVE_WAITLIST ---
    if trigger == Trigger.INACTIVE_WAITLIST:
        if WL in state:
            return TransitionResult(to_remove={role[WL]()})
        return TransitionResult()

    # --- JOINED_OTHER_GUILD ---
    if trigger == Trigger.JOINED_OTHER_GUILD:
        # Member -> Registered
        if MEM in state and WL not in state:
            return TransitionResult(
                to_remove={role[MEM]()}, to_add={role[REG]()}
            )
        # Hiatus alone -> Registered
        if HIA in state and WL not in state:
            return TransitionResult(
                to_remove={role[HIA]()}, to_add={role[REG]()}
            )
        # Reg+WL -> drop WL
        if has(REG, WL):
            return TransitionResult(to_remove={role[WL]()})
        # Hiatus+WL -> drop WL+Hiatus, add Reg
        if has(HIA, WL):
            return TransitionResult(
                to_remove={role[WL](), role[HIA]()}, to_add={role[REG]()}
            )
        # Honourary+WL -> drop WL+Member(if any)+Honourary edge case from table
        if has(HON, WL):
            removes = {role[WL]()}
            adds = {role[REG]()}
            if MEM in state:
                removes.add(role[MEM]())
            return TransitionResult(to_remove=removes, to_add=adds)
        # Honourary alone or Registered alone: do nothing
        return TransitionResult()

    return TransitionResult(error=f"Unknown trigger: {trigger!r}")


async def resolve_guild_member(bot, disc_uuid: str) -> discord.Member | None:
    """Resolve a Discord member from a ``disc_uuid`` string across the bot's
    guilds: cached ``get_member`` first, REST ``fetch_member`` on a cache miss.

    Returns ``None`` only if the user is in *no* guild (a cache miss is
    resolved via REST before concluding "not present") or the id is invalid.
    Shared by the waitlist commands, the ``waitlist_cleanup`` loop and the
    janitor so every role/DB reconciliation uses one consistent lookup.
    """
    try:
        uid = int(disc_uuid)
    except (TypeError, ValueError):
        return None
    for guild in bot.guilds:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                continue
            except discord.HTTPException:
                continue
        return member
    return None


async def apply_transition(
    member: discord.Member,
    trigger: Trigger,
    *,
    reason: str | None = None,
) -> TransitionResult:
    """Compute and apply a role-state transition. Returns the result for the
    caller to inspect (e.g. to surface errors in a slash response).
    """
    state = state_of(member)
    result = compute_transition(state, trigger)

    if result.is_error:
        logger.warning(
            f"RoleState transition error for {member} ({member.id}): {result.error} "
            f"[trigger={trigger.value} state={state}]"
        )
        return result

    if result.to_add or result.to_remove:
        guild = member.guild
        add_roles = [r for r in (guild.get_role(rid) for rid in result.to_add) if r is not None]
        rem_roles = [r for r in (guild.get_role(rid) for rid in result.to_remove) if r is not None]
        if rem_roles:
            await member.remove_roles(*rem_roles, reason=reason or f"role_state:{trigger.value}")
        if add_roles:
            await member.add_roles(*add_roles, reason=reason or f"role_state:{trigger.value}")
        logger.info(
            f"Applied transition {trigger.value} to {member} ({member.id}): "
            f"+{[r.name for r in add_roles]} -{[r.name for r in rem_roles]}"
        )

    return result


async def force_to_registered_only(member: discord.Member, *, reason: str | None = None) -> None:
    """Strip Hiatus/Member/Honourary/Waitlisted and ensure Registered. Used by
    ``/block`` per ../.claude/membership_spec.md \u00a72b.
    """
    state = state_of(member)
    to_remove_ids = {
        rid
        for s, fn in _STATE_ROLE_MAP.items()
        if s != RoleState.REGISTERED and s in state
        and (rid := fn())
    }
    to_add_ids = set()
    if RoleState.REGISTERED not in state:
        to_add_ids.add(CurrConfig.ROLE_REGISTERED)

    guild = member.guild
    rem_roles = [r for r in (guild.get_role(rid) for rid in to_remove_ids) if r is not None]
    add_roles = [r for r in (guild.get_role(rid) for rid in to_add_ids) if r is not None]
    if rem_roles:
        await member.remove_roles(*rem_roles, reason=reason or "block:force-registered")
    if add_roles:
        await member.add_roles(*add_roles, reason=reason or "block:force-registered")


async def ensure_linked_baseline(
    member: discord.Member,
    *,
    in_returners: bool,
    in_other_guild: bool = False,
    blocked: bool = False,
    reason: str | None = None,
) -> None:
    """Enforce the invariant: any linked Discord user must hold a *valid*
    primary membership state, never none.

    Preservation-based: it strips only primary roles that are **invalid**
    given the inputs, and grants a baseline **only when no valid primary role
    remains**. A manually-assigned HIATUS / HONOURARY is kept when it is still
    valid \u2014 re-running enforcement (on a re-link, /link set, /waitlist add,
    or the janitor) must never quietly demote a legitimate manual tag.

    Validity by situation (exactly one of ``in_returners`` / ``in_other_guild``
    may be True; both False = guildless):

    - ``blocked``       \u2192 REGISTERED only (authoritative, membership_spec
                          \u00a72b; clears MEMBER/HIATUS/HONOURARY).
    - ``in_returners``  \u2192 MEMBER only (authoritative \u2014 they *are* in the
                          guild; clears REGISTERED/HIATUS/HONOURARY, matching
                          the JOINED_VETS transition).
    - ``in_other_guild``\u2192 valid = {REGISTERED, HONOURARY}. HIATUS is invalid
                          (in a guild \u2192 never Hiatus) and a stale MEMBER is
                          invalid (not in Returners); both stripped. HONOURARY
                          is preserved (matches JOINED_OTHER_GUILD).
    - guildless         \u2192 valid = {REGISTERED, HIATUS, HONOURARY}. A
                          manually-parked HIATUS is legitimate and preserved;
                          only a stale MEMBER is stripped.

    When no valid primary remains the safe baseline is granted (MEMBER for the
    in_returners case, REGISTERED otherwise \u2014 the "Flame" no-state self-heal;
    purely additive). WAITLISTED is never touched. Idempotent: a member
    already holding a valid primary and no invalid ones is a no-op (no API
    calls).
    """
    REG = RoleState.REGISTERED
    MEM = RoleState.MEMBER
    HIA = RoleState.HIATUS
    HON = RoleState.HONOURARY

    state = state_of(member)

    if blocked:
        valid = {REG}
    elif in_returners:
        valid = {MEM}
    elif in_other_guild:
        valid = {REG, HON}
    else:  # guildless
        valid = {REG, HIA, HON}

    primary_state_flags = (REG, MEM, HIA, HON)
    held_valid = any(s in state for s in valid)
    to_remove_ids = {
        _STATE_ROLE_MAP[s]()
        for s in primary_state_flags
        if s in state and s not in valid
    }
    to_add_ids: set[int] = set()
    if not held_valid:
        baseline = MEM if valid == {MEM} else REG
        to_add_ids.add(_STATE_ROLE_MAP[baseline]())

    if not to_add_ids and not to_remove_ids:
        return

    guild = member.guild
    rem_roles = [r for r in (guild.get_role(rid) for rid in to_remove_ids) if r is not None]
    add_roles = [r for r in (guild.get_role(rid) for rid in to_add_ids) if r is not None]
    if to_add_ids:
        default_reason = f"linked baseline -> {'MEMBER' if valid == {MEM} else 'REGISTERED'}"
    else:
        default_reason = "linked baseline: prune invalid role(s)"
    if to_add_ids and not add_roles:
        logger.error(
            "ensure_linked_baseline: baseline role(s) %s could not be resolved in guild %s "
            "(check CurrConfig.ROLE_MEMBER / ROLE_REGISTERED ids). Aborting for %s.",
            to_add_ids, guild.id, member,
        )
        return
    if to_remove_ids and not rem_roles:
        logger.warning(
            "ensure_linked_baseline: stale role(s) %s could not be resolved in guild %s for %s",
            to_remove_ids, guild.id, member,
        )
    if rem_roles:
        await member.remove_roles(*rem_roles, reason=reason or default_reason)
    if add_roles:
        await member.add_roles(*add_roles, reason=reason or default_reason)
    logger.info(
        "ensure_linked_baseline: %s (%s) +%s -%s",
        member, member.id, [r.name for r in add_roles], [r.name for r in rem_roles],
    )

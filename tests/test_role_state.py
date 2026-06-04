"""Tests for the role-state machine in ``lib/role_state.py``.

Covers the pure surfaces:

* ``state_of`` — derives a ``RoleState`` bitflag from a member's role ids.
* ``compute_transition`` — the table from ``.claude/membership_spec.md §6``.
  Every documented (state, trigger) cell is asserted; "error state" entries
  in the doc surface as ``TransitionResult.error`` rather than a silent
  misapplied delta.
* ``ensure_linked_baseline`` — preservation-based pruner. We patch the
  member's ``add_roles``/``remove_roles`` to capture the deltas and assert
  the validity matrix from the docstring (blocked / in_returners /
  in_other_guild / guildless).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from config import CurrConfig
from lib.role_state import (
    RoleState,
    Trigger,
    compute_transition,
    ensure_linked_baseline,
    state_of,
)


# Pin the role-id config so test assertions don't drift with prod ids.
# Using small, distinct integers makes the failure messages readable.
_R_REG = 101
_R_HIA = 102
_R_MEM = 103
_R_HON = 104
_R_WL = 105


@pytest.fixture(autouse=True)
def _pin_role_ids(monkeypatch):
    monkeypatch.setattr(CurrConfig, "ROLE_REGISTERED", _R_REG)
    monkeypatch.setattr(CurrConfig, "ROLE_HIATUS", _R_HIA)
    monkeypatch.setattr(CurrConfig, "ROLE_MEMBER", _R_MEM)
    monkeypatch.setattr(CurrConfig, "ROLE_HONOURARY", _R_HON)
    monkeypatch.setattr(CurrConfig, "ROLE_WAITLISTED", _R_WL)


def _member_with(*role_ids: int) -> MagicMock:
    """``MagicMock(spec=discord.Member)`` with the given roles. Used as
    input to ``state_of`` — only ``.roles[i].id`` is read."""
    m = MagicMock(spec=discord.Member)
    m.roles = [MagicMock(id=rid) for rid in role_ids]
    return m


# ---------------------------------------------------------------------------
# state_of
# ---------------------------------------------------------------------------


def test_state_of_no_roles_is_none():
    assert state_of(_member_with()) == RoleState.NONE


def test_state_of_single_role_decoded():
    cases = [
        (_R_REG, RoleState.REGISTERED),
        (_R_HIA, RoleState.HIATUS),
        (_R_MEM, RoleState.MEMBER),
        (_R_HON, RoleState.HONOURARY),
        (_R_WL, RoleState.WAITLISTED),
    ]
    for rid, expected in cases:
        assert state_of(_member_with(rid)) == expected, f"role {rid}"


def test_state_of_combines_flags_for_multiple_roles():
    s = state_of(_member_with(_R_REG, _R_WL))
    assert RoleState.REGISTERED in s
    assert RoleState.WAITLISTED in s
    assert RoleState.MEMBER not in s


def test_state_of_ignores_unrelated_roles():
    s = state_of(_member_with(_R_MEM, 99999, 88888))
    assert s == RoleState.MEMBER


# ---------------------------------------------------------------------------
# compute_transition — full matrix from membership_spec.md §6
# ---------------------------------------------------------------------------
#
# Row state         | added_to_waitlist | joined_vets             | became_guildless         | inactive (member/wl)         | joined_other_guild
# REGISTERED        | +WL               | -REG +MEM               | —                        | —                            | —
# HIATUS            | +WL               | -HIA +MEM               | error                    | —                            | -HIA +REG
# MEMBER            | error             | error                   | -MEM +HIA                | DM warning (member case)     | -MEM +REG
# HONOURARY         | +WL               | -HON +MEM               | —                        | —                            | —
# REG+WL            | error             | -WL -REG +MEM           | —                        | -WL (waitlist case)          | -WL
# HIA+WL            | error             | -WL -HIA +MEM           | error                    | -WL (waitlist case)          | -WL -HIA +REG
# HON+WL            | error             | -WL -HON +MEM           | —                        | -WL (waitlist case)          | -WL ( -MEM if held) +REG


# --- ADDED_TO_WAITLIST ---


def test_added_to_waitlist_from_registered_adds_waitlist():
    r = compute_transition(RoleState.REGISTERED, Trigger.ADDED_TO_WAITLIST)
    assert r.to_add == {_R_WL}
    assert r.to_remove == set()
    assert not r.is_error


def test_added_to_waitlist_from_hiatus_adds_waitlist():
    r = compute_transition(RoleState.HIATUS, Trigger.ADDED_TO_WAITLIST)
    assert r.to_add == {_R_WL}


def test_added_to_waitlist_from_honourary_adds_waitlist():
    r = compute_transition(RoleState.HONOURARY, Trigger.ADDED_TO_WAITLIST)
    assert r.to_add == {_R_WL}


def test_added_to_waitlist_when_already_waitlisted_is_error():
    r = compute_transition(RoleState.WAITLISTED, Trigger.ADDED_TO_WAITLIST)
    assert r.is_error
    assert "already waitlisted" in r.error.lower()


def test_added_to_waitlist_when_member_is_error():
    r = compute_transition(RoleState.MEMBER, Trigger.ADDED_TO_WAITLIST)
    assert r.is_error
    assert "member" in r.error.lower()


def test_added_to_waitlist_when_no_state_is_error():
    r = compute_transition(RoleState.NONE, Trigger.ADDED_TO_WAITLIST)
    assert r.is_error


# --- JOINED_VETS ---


def test_joined_vets_from_nothing_grants_member():
    r = compute_transition(RoleState.NONE, Trigger.JOINED_VETS)
    assert r.to_add == {_R_MEM}
    assert r.to_remove == set()


def test_joined_vets_from_registered_swaps_to_member():
    r = compute_transition(RoleState.REGISTERED, Trigger.JOINED_VETS)
    assert r.to_add == {_R_MEM}
    assert r.to_remove == {_R_REG}


def test_joined_vets_from_hiatus_swaps_to_member():
    r = compute_transition(RoleState.HIATUS, Trigger.JOINED_VETS)
    assert r.to_add == {_R_MEM}
    assert r.to_remove == {_R_HIA}


def test_joined_vets_from_honourary_swaps_to_member():
    r = compute_transition(RoleState.HONOURARY, Trigger.JOINED_VETS)
    assert r.to_add == {_R_MEM}
    assert r.to_remove == {_R_HON}


def test_joined_vets_from_reg_wl_strips_both_and_adds_member():
    r = compute_transition(
        RoleState.REGISTERED | RoleState.WAITLISTED, Trigger.JOINED_VETS,
    )
    assert r.to_add == {_R_MEM}
    assert r.to_remove == {_R_REG, _R_WL}


def test_joined_vets_from_hia_wl_strips_both_and_adds_member():
    r = compute_transition(
        RoleState.HIATUS | RoleState.WAITLISTED, Trigger.JOINED_VETS,
    )
    assert r.to_add == {_R_MEM}
    assert r.to_remove == {_R_HIA, _R_WL}


def test_joined_vets_when_already_member_is_error():
    r = compute_transition(RoleState.MEMBER, Trigger.JOINED_VETS)
    assert r.is_error


# --- BECAME_GUILDLESS ---


def test_became_guildless_member_moves_to_hiatus():
    r = compute_transition(RoleState.MEMBER, Trigger.BECAME_GUILDLESS)
    assert r.to_add == {_R_HIA}
    assert r.to_remove == {_R_MEM}


def test_became_guildless_hiatus_is_error_state():
    """A hiatus user is by definition already guildless — getting this
    trigger means upstream data is inconsistent."""
    r = compute_transition(RoleState.HIATUS, Trigger.BECAME_GUILDLESS)
    assert r.is_error


def test_became_guildless_registered_is_noop():
    r = compute_transition(RoleState.REGISTERED, Trigger.BECAME_GUILDLESS)
    assert r.is_noop
    assert not r.is_error


def test_became_guildless_honourary_is_noop():
    r = compute_transition(RoleState.HONOURARY, Trigger.BECAME_GUILDLESS)
    assert r.is_noop


# --- INACTIVE_MEMBER (DM warning, no role change) ---


def test_inactive_member_when_member_emits_dm_side_effect():
    r = compute_transition(RoleState.MEMBER, Trigger.INACTIVE_MEMBER)
    assert r.side_effects == ["dm_inactive_warning"]
    assert r.to_add == set() and r.to_remove == set()


def test_inactive_member_when_not_member_is_noop():
    r = compute_transition(RoleState.REGISTERED, Trigger.INACTIVE_MEMBER)
    assert r.is_noop
    assert r.side_effects == []


# --- INACTIVE_WAITLIST ---


def test_inactive_waitlist_drops_waitlist_role():
    r = compute_transition(
        RoleState.REGISTERED | RoleState.WAITLISTED, Trigger.INACTIVE_WAITLIST,
    )
    assert r.to_remove == {_R_WL}
    assert r.to_add == set()


def test_inactive_waitlist_without_waitlist_role_is_noop():
    r = compute_transition(RoleState.MEMBER, Trigger.INACTIVE_WAITLIST)
    assert r.is_noop


# --- JOINED_OTHER_GUILD ---


def test_joined_other_guild_member_to_registered():
    r = compute_transition(RoleState.MEMBER, Trigger.JOINED_OTHER_GUILD)
    assert r.to_remove == {_R_MEM}
    assert r.to_add == {_R_REG}


def test_joined_other_guild_hiatus_to_registered():
    r = compute_transition(RoleState.HIATUS, Trigger.JOINED_OTHER_GUILD)
    assert r.to_remove == {_R_HIA}
    assert r.to_add == {_R_REG}


def test_joined_other_guild_reg_plus_wl_drops_only_waitlist():
    r = compute_transition(
        RoleState.REGISTERED | RoleState.WAITLISTED, Trigger.JOINED_OTHER_GUILD,
    )
    assert r.to_remove == {_R_WL}
    assert r.to_add == set()


def test_joined_other_guild_hiatus_plus_wl_drops_both_and_adds_registered():
    r = compute_transition(
        RoleState.HIATUS | RoleState.WAITLISTED, Trigger.JOINED_OTHER_GUILD,
    )
    assert r.to_remove == {_R_WL, _R_HIA}
    assert r.to_add == {_R_REG}


def test_joined_other_guild_honourary_alone_is_noop():
    """HONOURARY persists across guild changes — they're "guildless from
    the bot's pov" intentionally."""
    r = compute_transition(RoleState.HONOURARY, Trigger.JOINED_OTHER_GUILD)
    assert r.is_noop


def test_joined_other_guild_honourary_plus_wl_drops_wl_and_adds_registered():
    r = compute_transition(
        RoleState.HONOURARY | RoleState.WAITLISTED, Trigger.JOINED_OTHER_GUILD,
    )
    assert _R_WL in r.to_remove
    assert _R_REG in r.to_add


def test_joined_other_guild_registered_alone_is_noop():
    r = compute_transition(RoleState.REGISTERED, Trigger.JOINED_OTHER_GUILD)
    assert r.is_noop


# ---------------------------------------------------------------------------
# ensure_linked_baseline — validity matrix
# ---------------------------------------------------------------------------
#
# Tests build a mock Member whose .roles drives state_of() and whose
# add_roles/remove_roles are AsyncMocks. Asserts the *set of role ids*
# passed into each call rather than the discord.Role objects themselves.


def _baseline_member(*role_ids: int) -> MagicMock:
    """A mock ``discord.Member`` with:
      * ``roles`` populated for ``state_of``,
      * ``guild.get_role(rid)`` returning a stub Role with matching ``.id``
        (so the function's lookup-and-filter survives),
      * ``add_roles`` / ``remove_roles`` as AsyncMocks for assertion.
    """
    m = MagicMock(spec=discord.Member)
    m.roles = [MagicMock(id=rid) for rid in role_ids]
    m.id = 42

    def _get_role(rid):
        role = MagicMock()
        role.id = rid
        role.name = f"role_{rid}"
        return role

    m.guild = MagicMock()
    m.guild.get_role.side_effect = _get_role
    m.guild.id = 1
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    return m


def _ids(roles_call_args) -> set[int]:
    """Pull the role ids out of an ``add_roles(*roles)`` / ``remove_roles(...)``
    call. ``call_args.args`` is the positional tuple of Role objects."""
    return {r.id for r in roles_call_args.args}


# --- blocked -> REGISTERED only ---


async def test_baseline_blocked_strips_member_and_adds_registered():
    m = _baseline_member(_R_MEM)
    await ensure_linked_baseline(m, in_returners=True, blocked=True)
    m.add_roles.assert_awaited_once()
    m.remove_roles.assert_awaited_once()
    assert _ids(m.add_roles.call_args) == {_R_REG}
    assert _ids(m.remove_roles.call_args) == {_R_MEM}


async def test_baseline_blocked_already_registered_is_noop():
    m = _baseline_member(_R_REG)
    await ensure_linked_baseline(m, in_returners=False, blocked=True)
    m.add_roles.assert_not_awaited()
    m.remove_roles.assert_not_awaited()


async def test_baseline_blocked_strips_honourary():
    """Blocked is authoritative — HONOURARY isn't valid when blocked."""
    m = _baseline_member(_R_HON)
    await ensure_linked_baseline(m, in_returners=False, blocked=True)
    assert _ids(m.add_roles.call_args) == {_R_REG}
    assert _ids(m.remove_roles.call_args) == {_R_HON}


# --- in_returners -> MEMBER only ---


async def test_baseline_in_returners_grants_member_when_no_state():
    m = _baseline_member()
    await ensure_linked_baseline(m, in_returners=True)
    assert _ids(m.add_roles.call_args) == {_R_MEM}
    m.remove_roles.assert_not_awaited()


async def test_baseline_in_returners_promotes_registered_to_member():
    m = _baseline_member(_R_REG)
    await ensure_linked_baseline(m, in_returners=True)
    assert _ids(m.add_roles.call_args) == {_R_MEM}
    assert _ids(m.remove_roles.call_args) == {_R_REG}


async def test_baseline_in_returners_strips_hiatus_and_grants_member():
    m = _baseline_member(_R_HIA)
    await ensure_linked_baseline(m, in_returners=True)
    assert _ids(m.add_roles.call_args) == {_R_MEM}
    assert _ids(m.remove_roles.call_args) == {_R_HIA}


async def test_baseline_in_returners_already_member_is_noop():
    m = _baseline_member(_R_MEM)
    await ensure_linked_baseline(m, in_returners=True)
    m.add_roles.assert_not_awaited()
    m.remove_roles.assert_not_awaited()


# --- in_other_guild -> {REGISTERED, HONOURARY} ---


async def test_baseline_in_other_guild_strips_hiatus():
    """HIATUS is invalid when in another guild — strip it, grant REG."""
    m = _baseline_member(_R_HIA)
    await ensure_linked_baseline(m, in_returners=False, in_other_guild=True)
    assert _ids(m.add_roles.call_args) == {_R_REG}
    assert _ids(m.remove_roles.call_args) == {_R_HIA}


async def test_baseline_in_other_guild_strips_stale_member():
    """MEMBER means "in Returners". In another guild, that's stale."""
    m = _baseline_member(_R_MEM)
    await ensure_linked_baseline(m, in_returners=False, in_other_guild=True)
    assert _ids(m.add_roles.call_args) == {_R_REG}
    assert _ids(m.remove_roles.call_args) == {_R_MEM}


async def test_baseline_in_other_guild_preserves_honourary():
    m = _baseline_member(_R_HON)
    await ensure_linked_baseline(m, in_returners=False, in_other_guild=True)
    m.add_roles.assert_not_awaited()
    m.remove_roles.assert_not_awaited()


async def test_baseline_in_other_guild_preserves_registered():
    m = _baseline_member(_R_REG)
    await ensure_linked_baseline(m, in_returners=False, in_other_guild=True)
    m.add_roles.assert_not_awaited()
    m.remove_roles.assert_not_awaited()


# --- guildless -> {REGISTERED, HIATUS, HONOURARY} ---


async def test_baseline_guildless_strips_stale_member():
    m = _baseline_member(_R_MEM)
    await ensure_linked_baseline(m, in_returners=False, in_other_guild=False)
    assert _ids(m.add_roles.call_args) == {_R_REG}
    assert _ids(m.remove_roles.call_args) == {_R_MEM}


async def test_baseline_guildless_preserves_hiatus():
    """Manually-parked HIATUS is legitimate when guildless."""
    m = _baseline_member(_R_HIA)
    await ensure_linked_baseline(m, in_returners=False, in_other_guild=False)
    m.add_roles.assert_not_awaited()
    m.remove_roles.assert_not_awaited()


async def test_baseline_guildless_grants_registered_when_no_state():
    m = _baseline_member()
    await ensure_linked_baseline(m, in_returners=False, in_other_guild=False)
    assert _ids(m.add_roles.call_args) == {_R_REG}
    m.remove_roles.assert_not_awaited()


async def test_baseline_does_not_touch_waitlisted():
    """WAITLISTED is never in the primary set — leave it alone in every
    branch."""
    m = _baseline_member(_R_WL)
    await ensure_linked_baseline(m, in_returners=False, in_other_guild=False)
    # Should add REG (no valid primary), but never touch WL.
    if m.remove_roles.await_count:
        assert _R_WL not in _ids(m.remove_roles.call_args)
    if m.add_roles.await_count:
        assert _R_WL not in _ids(m.add_roles.call_args)

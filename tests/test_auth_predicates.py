"""Tests for the sync permission predicates in lib/auth.py.

These five predicates underpin every command's permission gate. They are
pure (no I/O) and trivially mockable: a ``MagicMock(spec=discord.Member)``
satisfies ``isinstance(m, discord.Member)`` while a
``MagicMock(spec=discord.User)`` does not, which is exactly the branch
the predicates discriminate on.
"""

from __future__ import annotations

import pytest

from config import CurrConfig
from lib.auth import (
    _has_admin_perm,
    _has_guild_role,
    _has_registered_role,
    _has_staff_role,
    _is_operator,
)


# ---------------------------------------------------------------------------
# _is_operator
# ---------------------------------------------------------------------------


def test_is_operator_true_when_id_in_admins(mock_member, monkeypatch):
    monkeypatch.setattr(CurrConfig, "ADMINS", {12345})
    assert _is_operator(mock_member(user_id=12345)) is True


def test_is_operator_false_when_id_not_in_admins(mock_member, monkeypatch):
    monkeypatch.setattr(CurrConfig, "ADMINS", {12345})
    assert _is_operator(mock_member(user_id=99999)) is False


def test_is_operator_also_works_for_user(mock_user, monkeypatch):
    # _is_operator only inspects .id, so a non-Member User is fine.
    monkeypatch.setattr(CurrConfig, "ADMINS", {42})
    assert _is_operator(mock_user(user_id=42)) is True


# ---------------------------------------------------------------------------
# _has_admin_perm
# ---------------------------------------------------------------------------


def test_has_admin_perm_true_for_admin_member(mock_member):
    assert _has_admin_perm(mock_member(admin=True)) is True


def test_has_admin_perm_false_for_non_admin_member(mock_member):
    assert _has_admin_perm(mock_member(admin=False)) is False


def test_has_admin_perm_false_for_non_member_user(mock_user):
    assert _has_admin_perm(mock_user()) is False


# ---------------------------------------------------------------------------
# _has_staff_role
# ---------------------------------------------------------------------------


def test_has_staff_role_true_when_role_present(mock_member):
    assert _has_staff_role(mock_member(role_ids=[CurrConfig.STAFF_ROLE])) is True


def test_has_staff_role_false_when_role_absent(mock_member):
    assert _has_staff_role(mock_member(role_ids=[])) is False


def test_has_staff_role_false_with_unrelated_role(mock_member):
    assert _has_staff_role(mock_member(role_ids=[CurrConfig.ROLE_REGISTERED])) is False


def test_has_staff_role_false_for_non_member_user(mock_user):
    assert _has_staff_role(mock_user()) is False


# ---------------------------------------------------------------------------
# _has_guild_role  (Waitlisted | Honourary | Hiatus | Member)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role_attr",
    ["ROLE_WAITLISTED", "ROLE_HONOURARY", "ROLE_HIATUS", "ROLE_MEMBER"],
)
def test_has_guild_role_true_for_each_guild_role(mock_member, role_attr):
    role_id = getattr(CurrConfig, role_attr)
    assert _has_guild_role(mock_member(role_ids=[role_id])) is True


def test_has_guild_role_false_with_no_guild_roles(mock_member):
    assert _has_guild_role(mock_member(role_ids=[CurrConfig.ROLE_REGISTERED])) is False


def test_has_guild_role_false_with_empty_roles(mock_member):
    assert _has_guild_role(mock_member(role_ids=[])) is False


def test_has_guild_role_false_for_non_member_user(mock_user):
    assert _has_guild_role(mock_user()) is False


# ---------------------------------------------------------------------------
# _has_registered_role
# ---------------------------------------------------------------------------


def test_has_registered_role_true_with_registered_role(mock_member):
    assert _has_registered_role(mock_member(role_ids=[CurrConfig.ROLE_REGISTERED])) is True


@pytest.mark.parametrize(
    "role_attr",
    ["ROLE_WAITLISTED", "ROLE_HONOURARY", "ROLE_HIATUS", "ROLE_MEMBER"],
)
def test_has_registered_role_true_via_guild_role_delegation(mock_member, role_attr):
    role_id = getattr(CurrConfig, role_attr)
    assert _has_registered_role(mock_member(role_ids=[role_id])) is True


def test_has_registered_role_false_with_no_membership_roles(mock_member):
    assert _has_registered_role(mock_member(role_ids=[])) is False


def test_has_registered_role_false_with_unrelated_role(mock_member):
    # STAFF_ROLE alone does not satisfy REGISTERED — staff isn't on the
    # membership-state ladder; the predicate cares about identity tiers.
    assert _has_registered_role(mock_member(role_ids=[CurrConfig.STAFF_ROLE])) is False


def test_has_registered_role_false_for_non_member_user(mock_user):
    assert _has_registered_role(mock_user()) is False

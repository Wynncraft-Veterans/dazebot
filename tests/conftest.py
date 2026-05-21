"""Shared pytest fixtures for dazebot tests.

The mock factories here exist to exercise the permission predicates in
``lib/auth.py`` without standing up a real Discord client. They build
``MagicMock`` instances with ``spec=`` set so that ``isinstance`` checks
against ``discord.Member`` / ``discord.User`` resolve correctly.
"""

from __future__ import annotations

from typing import Iterable
from unittest.mock import MagicMock

import discord
import pytest


@pytest.fixture
def mock_member():
    """Factory: build a ``MagicMock(spec=discord.Member)`` with the given
    role ids and administrator flag.

    ``isinstance(result, discord.Member)`` returns True; the lib/auth.py
    predicates rely on this distinction.
    """

    def _build(role_ids: Iterable[int] = (), *, admin: bool = False, user_id: int = 1) -> MagicMock:
        member = MagicMock(spec=discord.Member)
        member.id = user_id
        member.roles = [MagicMock(id=rid) for rid in role_ids]
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = admin
        return member

    return _build


@pytest.fixture
def mock_user():
    """Factory: build a ``MagicMock(spec=discord.User)`` — a non-Member
    user (e.g. DM context). Used to verify predicates that gate on
    ``isinstance(user, discord.Member)``.
    """

    def _build(user_id: int = 1) -> MagicMock:
        user = MagicMock(spec=discord.User)
        user.id = user_id
        return user

    return _build

"""Tests for the ``cogs.events.returns`` subsystem.

Covers the parts that don't need a live Tortoise connection:

* Registry decorators (``@register``, ``@register_manage``) — metadata,
  defaults, duplicate detection, and the autoloaded weeks from the real
  ``week_*.py`` files.
* The :class:`Tier` enum's ordering invariants (REGISTERED < GUILD <
  STAFF < ADMIN < OPERATOR) — these are load-bearing for the tier_allows
  cascade and worth pinning explicitly.
* :func:`tier_allows` matrix — exhaustive: nobody / registered-only /
  guild-role / staff / admin / operator at each tier, plus the
  ``custom_check`` override.
* :func:`manage_tier_allows` — async-custom-check behaviour (skipped when
  tier already fails; treated as deny when it raises).
* :func:`is_persist_context` — DM and the public-log channel persist;
  everything else doesn't.
* :func:`send_help` — the rendered help is filtered to subcommands the
  caller can actually run.

Functions that hit the DB (``lookup_owned_cult`` / ``is_figurehead`` and
every ``@register_manage`` callback) need a Tortoise fixture; that's left
for a follow-up.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from config import CurrConfig
from cogs.events.returns import (
    MANAGE_REGISTRY,
    REGISTRY,
    ManageSubcommand,
    Tier,
    UserHandler,
    register,
    register_manage,
)
from cogs.events.returns._common import (
    PERSIST_CHANNEL_ID,
    is_persist_context,
    manage_tier_allows,
    send_help,
    tier_allows,
)


# ---------------------------------------------------------------------------
# Tier enum invariants
# ---------------------------------------------------------------------------


def test_tier_ordering_matches_auth_hierarchy():
    """REGISTERED < GUILD < STAFF < ADMIN < OPERATOR.

    The relative order — not the absolute values — is what the
    :func:`tier_allows` cascade depends on. Pin it explicitly so a future
    re-numbering can't silently invert a gate.
    """
    assert Tier.REGISTERED < Tier.GUILD < Tier.STAFF < Tier.ADMIN < Tier.OPERATOR


def test_tier_values_are_stable():
    """Pinned values: anything that persists a Tier (DB column, JSON
    config) shouldn't drift on a re-order."""
    assert Tier.REGISTERED.value == 1
    assert Tier.GUILD.value == 2
    assert Tier.STAFF.value == 3
    assert Tier.ADMIN.value == 4
    assert Tier.OPERATOR.value == 5


# ---------------------------------------------------------------------------
# Registry: @register and @register_manage
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_registry(monkeypatch):
    """Swap REGISTRY / MANAGE_REGISTRY for empty dicts so tests don't
    pollute the global state populated by the real ``week_*.py``
    autoload at import time."""
    import cogs.events.returns as r

    fresh_reg: dict = {}
    fresh_manage: dict = {}
    monkeypatch.setattr(r, "REGISTRY", fresh_reg)
    monkeypatch.setattr(r, "MANAGE_REGISTRY", fresh_manage)
    return fresh_reg, fresh_manage


def test_register_populates_user_handler(isolated_registry):
    reg, _ = isolated_registry

    @register(100)
    async def handler(ctx):  # pragma: no cover - body unused
        pass

    assert 100 in reg
    h = reg[100]
    assert isinstance(h, UserHandler)
    assert h.callback is handler
    assert h.tier is Tier.GUILD  # default
    assert h.custom_check is None


def test_register_respects_explicit_tier_and_custom_check(isolated_registry):
    reg, _ = isolated_registry

    def predicate(user):  # pragma: no cover - inspected, not called
        return True

    @register(101, tier=Tier.REGISTERED, custom_check=predicate)
    async def handler(ctx):  # pragma: no cover
        pass

    h = reg[101]
    assert h.tier is Tier.REGISTERED
    assert h.custom_check is predicate


def test_register_rejects_duplicate_week(isolated_registry):
    @register(102)
    async def handler1(ctx):  # pragma: no cover
        pass

    with pytest.raises(RuntimeError, match=r"Duplicate /return handler for week 102"):

        @register(102)
        async def handler2(ctx):  # pragma: no cover
            pass


def test_register_manage_populates_subcommand(isolated_registry):
    _, manage = isolated_registry

    @register_manage(
        200, "doStuff", tier=Tier.ADMIN, help="Does stuff.", usage="<x>",
    )
    async def cb(ctx, args):  # pragma: no cover
        pass

    assert "doStuff" in manage[200]
    sub = manage[200]["doStuff"]
    assert isinstance(sub, ManageSubcommand)
    assert sub.callback is cb
    assert sub.tier is Tier.ADMIN
    assert sub.help == "Does stuff."
    assert sub.usage == "<x>"
    assert sub.custom_check is None


def test_register_manage_rejects_duplicate_name(isolated_registry):
    @register_manage(201, "x", tier=Tier.STAFF)
    async def cb1(ctx, args):  # pragma: no cover
        pass

    with pytest.raises(RuntimeError, match=r"Duplicate manage_return subcommand"):

        @register_manage(201, "x", tier=Tier.STAFF)
        async def cb2(ctx, args):  # pragma: no cover
            pass


def test_autoloaded_weeks_match_expected_set():
    """The three currently-implemented weeks are in REGISTRY at import
    time. A new week showing up means the autoload picked it up; a
    missing one means an ``@register`` regressed.
    """
    assert {0, 1, 73}.issubset(REGISTRY)


def test_autoloaded_manage_subcommands_match_expected_set():
    """``MANAGE_REGISTRY`` likewise mirrors what the week files declare.
    Update this assertion when adding/removing a manage subcommand.
    """
    assert set(MANAGE_REGISTRY[0]) == {
        "createCult",
        "listMembers",
        "broadcastMessage",
        "cultMessage",
        "addressOwnCult",
        "cultDistribution",
        "listOnlineUnaffiliated",
        "force",
    }
    assert set(MANAGE_REGISTRY[73]) == {"showMessage"}


# ---------------------------------------------------------------------------
# tier_allows: full matrix from nobody to operator
# ---------------------------------------------------------------------------


@pytest.fixture
def pinned_roles(monkeypatch):
    """Pin role id config + the captured ``_GUILD_ROLE_IDS`` frozenset
    so the matrix tests don't depend on production values."""
    monkeypatch.setattr(CurrConfig, "ROLE_REGISTERED", 1001)
    monkeypatch.setattr(CurrConfig, "ROLE_WAITLISTED", 1002)
    monkeypatch.setattr(CurrConfig, "ROLE_HONOURARY", 1003)
    monkeypatch.setattr(CurrConfig, "ROLE_HIATUS", 1004)
    monkeypatch.setattr(CurrConfig, "ROLE_MEMBER", 1005)
    monkeypatch.setattr(CurrConfig, "STAFF_ROLE", 1006)
    monkeypatch.setattr(CurrConfig, "ADMINS", {9001})

    import lib.auth as auth

    monkeypatch.setattr(
        auth, "_GUILD_ROLE_IDS", frozenset({1002, 1003, 1004, 1005}),
    )
    return {
        "registered": 1001,
        "member": 1005,
        "staff": 1006,
        "operator_id": 9001,
    }


def test_tier_allows_nobody_fails_every_tier(mock_member, pinned_roles):
    u = mock_member(role_ids=())
    for t in Tier:
        assert tier_allows(u, t) is False, f"expected deny for tier={t.name}"


def test_tier_allows_registered_only_passes_registered_only(mock_member, pinned_roles):
    """ROLE_REGISTERED alone passes REGISTERED but not GUILD — the whole
    reason these two tiers exist as distinct levels."""
    u = mock_member(role_ids=(pinned_roles["registered"],))
    assert tier_allows(u, Tier.REGISTERED) is True
    assert tier_allows(u, Tier.GUILD) is False
    assert tier_allows(u, Tier.STAFF) is False


def test_tier_allows_guild_member_clears_guild_and_registered(mock_member, pinned_roles):
    u = mock_member(role_ids=(pinned_roles["member"],))
    assert tier_allows(u, Tier.REGISTERED) is True
    assert tier_allows(u, Tier.GUILD) is True
    assert tier_allows(u, Tier.STAFF) is False


def test_tier_allows_staff(mock_member, pinned_roles):
    u = mock_member(role_ids=(pinned_roles["staff"],))
    assert tier_allows(u, Tier.GUILD) is True
    assert tier_allows(u, Tier.STAFF) is True
    assert tier_allows(u, Tier.ADMIN) is False


def test_tier_allows_admin(mock_member, pinned_roles):
    u = mock_member(admin=True)
    assert tier_allows(u, Tier.STAFF) is True
    assert tier_allows(u, Tier.ADMIN) is True
    assert tier_allows(u, Tier.OPERATOR) is False


def test_tier_allows_operator_passes_everything(mock_member, pinned_roles):
    u = mock_member(user_id=pinned_roles["operator_id"])
    for t in Tier:
        assert tier_allows(u, t) is True, f"operator should pass tier={t.name}"


def test_tier_allows_non_member_user_is_denied(mock_user, pinned_roles):
    """A non-Member User has no roles, so role-based tiers all deny.
    Operator is purely id-based and still works."""
    plain = mock_user(user_id=42)
    assert tier_allows(plain, Tier.REGISTERED) is False
    assert tier_allows(plain, Tier.GUILD) is False
    op_user = mock_user(user_id=pinned_roles["operator_id"])
    assert tier_allows(op_user, Tier.OPERATOR) is True


def test_tier_allows_custom_check_overrides_failing_tier(mock_member, pinned_roles):
    """``custom_check`` is OR'd onto the tier check — additive override,
    not replacement. Used by Return 73 to admit STORY_ROLE_IDS holders
    who'd otherwise fail the tier."""
    u = mock_member(role_ids=())
    assert tier_allows(u, Tier.GUILD) is False
    assert tier_allows(u, Tier.GUILD, custom_check=lambda _u: True) is True
    assert tier_allows(u, Tier.GUILD, custom_check=lambda _u: False) is False


# ---------------------------------------------------------------------------
# manage_tier_allows
# ---------------------------------------------------------------------------


def _fake_ctx_with_author(author):
    ctx = MagicMock()
    ctx.author = author
    return ctx


async def test_manage_tier_allows_passes_when_tier_satisfied(
    mock_member, pinned_roles,
):
    ctx = _fake_ctx_with_author(mock_member(admin=True))
    sub = ManageSubcommand(callback=AsyncMock(), tier=Tier.ADMIN)
    assert await manage_tier_allows(ctx, sub) is True


async def test_manage_tier_allows_short_circuits_when_tier_fails(
    mock_member, pinned_roles,
):
    """If the tier alone denies, the (potentially expensive, async)
    custom_check must not run."""
    ctx = _fake_ctx_with_author(mock_member(role_ids=()))
    check = AsyncMock(return_value=True)
    sub = ManageSubcommand(callback=AsyncMock(), tier=Tier.ADMIN, custom_check=check)
    assert await manage_tier_allows(ctx, sub) is False
    check.assert_not_awaited()


async def test_manage_tier_allows_runs_custom_check_when_tier_passes(
    mock_member, pinned_roles,
):
    ctx = _fake_ctx_with_author(mock_member(admin=True))
    check = AsyncMock(return_value=False)
    sub = ManageSubcommand(callback=AsyncMock(), tier=Tier.ADMIN, custom_check=check)
    assert await manage_tier_allows(ctx, sub) is False
    check.assert_awaited_once_with(ctx)


async def test_manage_tier_allows_treats_custom_check_exception_as_deny(
    mock_member, pinned_roles,
):
    """A raise inside ``custom_check`` mustn't be interpreted as a pass —
    fail closed."""
    ctx = _fake_ctx_with_author(mock_member(admin=True))
    check = AsyncMock(side_effect=RuntimeError("boom"))
    sub = ManageSubcommand(callback=AsyncMock(), tier=Tier.ADMIN, custom_check=check)
    assert await manage_tier_allows(ctx, sub) is False


# ---------------------------------------------------------------------------
# is_persist_context
# ---------------------------------------------------------------------------


def test_is_persist_context_true_for_dm():
    ctx = MagicMock()
    ctx.channel = MagicMock(spec=discord.DMChannel)
    assert is_persist_context(ctx) is True


def test_is_persist_context_true_for_public_log_channel():
    ctx = MagicMock()
    ctx.channel = MagicMock(spec=discord.TextChannel)
    ctx.channel.id = PERSIST_CHANNEL_ID
    assert is_persist_context(ctx) is True


def test_is_persist_context_false_in_random_channel():
    ctx = MagicMock()
    ctx.channel = MagicMock(spec=discord.TextChannel)
    ctx.channel.id = 99999999
    assert is_persist_context(ctx) is False


# ---------------------------------------------------------------------------
# send_help filters by caller tier
# ---------------------------------------------------------------------------


@pytest.fixture
def help_registry(monkeypatch):
    """Inject a small MANAGE_REGISTRY keyed at week 500 with three tiers
    for the help-filtering tests."""
    fake = {
        500: {
            "anyone_can": ManageSubcommand(
                callback=AsyncMock(),
                tier=Tier.GUILD,
                help="open to guild members.",
                usage="<x>",
            ),
            "admin_only": ManageSubcommand(
                callback=AsyncMock(),
                tier=Tier.ADMIN,
                help="admin-restricted.",
                usage="<y>",
            ),
            "op_only": ManageSubcommand(
                callback=AsyncMock(),
                tier=Tier.OPERATOR,
                help="operator-only.",
                usage="",
            ),
        },
    }
    import cogs.events.returns._common as common

    monkeypatch.setattr(common, "MANAGE_REGISTRY", fake)
    return fake


def _ctx_with_reply(author):
    """Help renderer (persist=True path) uses ctx.reply only."""
    ctx = MagicMock()
    ctx.author = author
    ctx.reply = AsyncMock()
    return ctx


async def test_send_help_filters_to_guild_tier(
    help_registry, mock_member, pinned_roles,
):
    member = mock_member(role_ids=(pinned_roles["member"],))
    ctx = _ctx_with_reply(member)
    await send_help(ctx, 500, persist=True)
    body = ctx.reply.await_args.args[0]
    assert "anyone_can" in body
    assert "admin_only" not in body
    assert "op_only" not in body


async def test_send_help_filters_to_admin_tier(
    help_registry, mock_member, pinned_roles,
):
    ctx = _ctx_with_reply(mock_member(admin=True))
    await send_help(ctx, 500, persist=True)
    body = ctx.reply.await_args.args[0]
    assert "anyone_can" in body
    assert "admin_only" in body
    assert "op_only" not in body


async def test_send_help_for_operator_shows_everything(
    help_registry, mock_member, pinned_roles,
):
    op = mock_member(user_id=pinned_roles["operator_id"])
    ctx = _ctx_with_reply(op)
    await send_help(ctx, 500, persist=True)
    body = ctx.reply.await_args.args[0]
    assert "anyone_can" in body
    assert "admin_only" in body
    assert "op_only" in body


async def test_send_help_for_unknown_week_reports_empty(
    help_registry, mock_member, pinned_roles,
):
    op = mock_member(user_id=pinned_roles["operator_id"])
    ctx = _ctx_with_reply(op)
    await send_help(ctx, 999, persist=True)
    body = ctx.reply.await_args.args[0]
    assert "999" in body


async def test_send_help_when_caller_has_no_access(
    help_registry, mock_member, pinned_roles,
):
    """A registered-only caller can't use any week-500 subcommand (the
    lowest tier there is GUILD). Help should say so rather than render
    an empty list."""
    member = mock_member(role_ids=(pinned_roles["registered"],))
    ctx = _ctx_with_reply(member)
    await send_help(ctx, 500, persist=True)
    body = ctx.reply.await_args.args[0]
    # No subcommand names should leak through.
    assert "anyone_can" not in body
    assert "admin_only" not in body
    assert "op_only" not in body

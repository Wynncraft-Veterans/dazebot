"""Permission tiers for hybrid commands.

Tiers, from least to most privileged:

    PUBLIC      Anyone in the server. (No decorator needed.)
    REGISTERED  Has any membership-state role (Registered, Waitlisted,
                Honourary, Hiatus, or Member).
    GUILD       Has Waitlisted, Honourary, Hiatus, or Member.
    STAFF       Has the configured STAFF_ROLE.
    ADMIN       Has the Discord "Administrator" permission.
    OPERATOR    Listed in CurrConfig.ADMINS (bot owners / devs).

Higher tiers always satisfy lower ones (an OPERATOR can run a STAFF command,
an ADMIN can run a GUILD command, and so on).

Special, non-hierarchical:

    SHOUTER     Has Manage Messages, OR holds a regional alert role
                (USA / Europe / Asia). Used only by /shout.

DMs: the decorators below resolve ``ctx.author`` against
``CurrConfig.GUILD``'s member cache (via :func:`_resolve_member`) before
applying role/perm checks. A user with the required role in the configured
guild therefore passes the same gate whether they invoke from a channel or
from dazebot's DMs. The low-level ``_has_*`` predicates remain ``Member``-only
(they return ``False`` for a bare ``User``); resolution happens at the
decorator/predicate call site.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from config import CurrConfig


# ---------------------------------------------------------------------------
# Low-level membership predicates (sync, take a discord user/member).
# ---------------------------------------------------------------------------


def _is_operator(user: discord.abc.User) -> bool:
    return user.id in CurrConfig.ADMINS


def _has_admin_perm(user: discord.abc.User) -> bool:
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


def _has_staff_role(user: discord.abc.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    return any(r.id == CurrConfig.STAFF_ROLE for r in user.roles)


_GUILD_ROLE_IDS = frozenset(
    {
        CurrConfig.ROLE_WAITLISTED,
        CurrConfig.ROLE_HONOURARY,
        CurrConfig.ROLE_HIATUS,
        CurrConfig.ROLE_MEMBER,
    }
)


def _has_guild_role(user: discord.abc.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    return any(r.id in _GUILD_ROLE_IDS for r in user.roles)


def _has_registered_role(user: discord.abc.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if any(r.id == CurrConfig.ROLE_REGISTERED for r in user.roles):
        return True
    return _has_guild_role(user)


def _resolve_member(
    user: discord.abc.User, client: discord.Client | None = None
) -> discord.Member | None:
    """Return ``user`` as a Member of ``CurrConfig.GUILD``, or ``None``.

    If ``user`` is already a :class:`discord.Member` (channel invocation),
    return it as-is — the bot only targets the configured guild, so any
    Member we see is one we want to authorise against.

    Otherwise (DM context: ``user`` is a bare :class:`discord.User`), look
    them up in the configured guild's member cache. Returns ``None`` if
    they aren't in the guild or if no client was supplied. The members
    intent is enabled in :mod:`bot`, so :meth:`Guild.get_member` will hit
    cache without needing an async fetch.
    """
    if isinstance(user, discord.Member):
        return user
    if client is None:
        return None
    guild = client.get_guild(CurrConfig.GUILD)
    if guild is None:
        return None
    return guild.get_member(user.id)


# ---------------------------------------------------------------------------
# Decorator factories (each enforces "this tier or higher").
# ---------------------------------------------------------------------------


def is_operator():
    """OPERATOR: in CurrConfig.ADMINS."""

    async def predicate(ctx: commands.Context):
        return _is_operator(ctx.author)

    return commands.check(predicate)


def is_admin():
    """ADMIN or higher: Discord Administrator permission, or OPERATOR."""

    async def predicate(ctx: commands.Context):
        if _is_operator(ctx.author):
            return True
        member = _resolve_member(ctx.author, ctx.bot)
        return member is not None and _has_admin_perm(member)

    return commands.check(predicate)


def is_staff():
    """STAFF or higher: STAFF_ROLE, ADMIN, or OPERATOR."""

    async def predicate(ctx: commands.Context):
        if _is_operator(ctx.author):
            return True
        member = _resolve_member(ctx.author, ctx.bot)
        if member is None:
            return False
        return _has_admin_perm(member) or _has_staff_role(member)

    return commands.check(predicate)


def is_guild():
    """GUILD or higher: Waitlisted/Honourary/Hiatus/Member, STAFF, ADMIN, or OPERATOR."""

    async def predicate(ctx: commands.Context):
        if _is_operator(ctx.author):
            return True
        member = _resolve_member(ctx.author, ctx.bot)
        if member is None:
            return False
        return (
            _has_admin_perm(member)
            or _has_staff_role(member)
            or _has_guild_role(member)
        )

    return commands.check(predicate)


def is_registered():
    """REGISTERED or higher: any membership-state role, STAFF, ADMIN, or OPERATOR."""

    async def predicate(ctx: commands.Context):
        if _is_operator(ctx.author):
            return True
        member = _resolve_member(ctx.author, ctx.bot)
        if member is None:
            return False
        return (
            _has_admin_perm(member)
            or _has_staff_role(member)
            or _has_registered_role(member)
        )

    return commands.check(predicate)


def is_shouter(role_ids):
    """SHOUTER (special): Manage Messages perm OR holds one of ``role_ids``.

    Caller passes the alert-role id list explicitly so this module doesn't
    need to know about /shout-specific config. Resolves the invoker against
    ``CurrConfig.GUILD`` so DM invocations work the same as in-server ones.
    """
    role_id_set = frozenset(role_ids)

    async def predicate(ctx: commands.Context):
        member = _resolve_member(ctx.author, ctx.bot)
        if member is None:
            return False
        if member.guild_permissions.manage_messages:
            return True
        return any(r.id in role_id_set for r in member.roles)

    return commands.check(predicate)

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
        return _is_operator(ctx.author) or _has_admin_perm(ctx.author)

    return commands.check(predicate)


def is_staff():
    """STAFF or higher: STAFF_ROLE, ADMIN, or OPERATOR."""

    async def predicate(ctx: commands.Context):
        u = ctx.author
        return _is_operator(u) or _has_admin_perm(u) or _has_staff_role(u)

    return commands.check(predicate)


def is_guild():
    """GUILD or higher: Waitlisted/Honourary/Hiatus/Member, STAFF, ADMIN, or OPERATOR."""

    async def predicate(ctx: commands.Context):
        u = ctx.author
        return (
            _is_operator(u)
            or _has_admin_perm(u)
            or _has_staff_role(u)
            or _has_guild_role(u)
        )

    return commands.check(predicate)


def is_registered():
    """REGISTERED or higher: any membership-state role, STAFF, ADMIN, or OPERATOR."""

    async def predicate(ctx: commands.Context):
        u = ctx.author
        return (
            _is_operator(u)
            or _has_admin_perm(u)
            or _has_staff_role(u)
            or _has_registered_role(u)
        )

    return commands.check(predicate)


def is_shouter(role_ids):
    """SHOUTER (special): Manage Messages perm OR holds one of ``role_ids``.

    Caller passes the alert-role id list explicitly so this module doesn't
    need to know about /shout-specific config.
    """
    return commands.check_any(
        commands.has_permissions(manage_messages=True),
        commands.has_any_role(*role_ids),
    )

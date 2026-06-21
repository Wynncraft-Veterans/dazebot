"""Per-week ``/return`` and ``~manage_return`` handlers.

Each week's implementation lives in ``week_NN.py`` and registers itself via
two decorators:

* ``@register(week, tier=..., custom_check=...)`` — the user-facing
  ``/return <week>`` entry point. Default tier is :data:`Tier.GUILD` (the
  four "guild" roles). A week may opt up (e.g. Return 0 uses
  :data:`Tier.REGISTERED`) or override with a custom predicate (e.g. Return
  73's ``STORY_ROLE_IDS``).
* ``@register_manage(week, name, tier=..., usage=..., help=..., custom_check=...)``
  — a sub-command of ``~manage_return <week> <name> [args...]``. The
  callback receives the raw remaining args as ``list[str]``; conversion
  (members, ints, multi-word messages) is the handler's responsibility.

The Returns cog imports this package, which triggers ``_autoload_weeks()``
and populates :data:`REGISTRY` / :data:`MANAGE_REGISTRY`. Dispatch then
goes through :func:`dispatch_user` and :func:`dispatch_manage`.

To add week N:

  1. Create ``cogs/events/returns/week_<N>.py``.
  2. Define an async ``handle(ctx)`` and decorate with ``@register(<N>)``.
  3. Optionally add manage sub-commands via ``@register_manage``.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Awaitable, Callable, Optional

import discord
from discord.ext import commands

logger = logging.getLogger("dazebot.cogs.events.returns")


class Tier(IntEnum):
    """Permission tier for Returns surfaces.

    Matches the hierarchy documented in :mod:`lib.auth`:
    ``REGISTERED < GUILD < STAFF < ADMIN < OPERATOR``. REGISTERED is the
    *broadest* (lowest bar — includes ROLE_REGISTERED-only users who have
    never been guild members); GUILD is stricter (the 4 task-listed roles
    only); each step up narrows the eligible population. Resolution to a
    concrete predicate happens in :func:`._common.tier_allows`.
    """

    REGISTERED = 1  # ROLE_REGISTERED OR any of the 4 guild roles (+ staff/admin/op)
    GUILD = 2       # DEFAULT for new returns: Waitlisted/Honourary/Hiatus/Member only
    STAFF = 3       # STAFF_ROLE
    ADMIN = 4       # Discord Administrator
    OPERATOR = 5    # CurrConfig.ADMINS


UserCallback = Callable[[commands.Context], Awaitable[None]]
ManageCallback = Callable[[commands.Context, list[str]], Awaitable[None]]
# Custom checks: sync for user handlers (cheap predicate), async for manage
# subcommands (figurehead lookup hits the DB).
#
# The user-handler check receives the resolved Member of CurrConfig.GUILD
# when possible (or the original User in pure-DM operator-only paths) plus
# the client, so the check can do its own guild lookups (e.g. roles in DMs).
UserCustomCheck = Callable[[discord.abc.User, Optional[discord.Client]], bool]
ManageCustomCheck = Callable[[commands.Context], Awaitable[bool]]


@dataclass
class UserHandler:
    callback: UserCallback
    tier: Tier = Tier.GUILD
    custom_check: Optional[UserCustomCheck] = None


@dataclass
class ManageSubcommand:
    callback: ManageCallback
    tier: Tier
    custom_check: Optional[ManageCustomCheck] = None
    help: str = ""
    usage: str = ""


REGISTRY: dict[int, UserHandler] = {}
MANAGE_REGISTRY: dict[int, dict[str, ManageSubcommand]] = {}

# A week may register an async ``tick(bot)`` to be invoked on a shared
# 60-second cadence by the Returns cog (see :mod:`cogs.events.return_cmd`).
# Handlers self-gate on time (e.g. "did the 24h turn deadline elapse?") so
# the dispatcher stays cadence-agnostic.
TickCallback = Callable[[discord.Client], Awaitable[None]]
TICK_REGISTRY: dict[int, TickCallback] = {}


def register(
    week: int,
    *,
    tier: Tier = Tier.GUILD,
    custom_check: Optional[UserCustomCheck] = None,
):
    """Decorator: register the ``/return <week>`` user-facing handler."""

    def decorator(fn: UserCallback) -> UserCallback:
        if week in REGISTRY:
            raise RuntimeError(f"Duplicate /return handler for week {week}")
        REGISTRY[week] = UserHandler(callback=fn, tier=tier, custom_check=custom_check)
        return fn

    return decorator


def register_manage(
    week: int,
    name: str,
    *,
    tier: Tier,
    custom_check: Optional[ManageCustomCheck] = None,
    help: str = "",
    usage: str = "",
):
    """Decorator: register a ``~manage_return <week> <name>`` sub-command."""

    def decorator(fn: ManageCallback) -> ManageCallback:
        bucket = MANAGE_REGISTRY.setdefault(week, {})
        if name in bucket:
            raise RuntimeError(
                f"Duplicate manage_return subcommand for week {week}: {name!r}"
            )
        bucket[name] = ManageSubcommand(
            callback=fn,
            tier=tier,
            custom_check=custom_check,
            help=help,
            usage=usage,
        )
        return fn

    return decorator


def register_tick(week: int):
    """Decorator: register an async ``tick(bot)`` for the shared 60s loop.

    The :class:`cogs.events.return_cmd.Returns` cog runs every registered
    tick once per minute and swallows exceptions per-handler. Handlers
    self-gate on wall-clock time (e.g. "is the 24h turn deadline up?") so
    the dispatcher doesn't need to know any per-week cadence.
    """

    def decorator(fn: TickCallback) -> TickCallback:
        if week in TICK_REGISTRY:
            raise RuntimeError(f"Duplicate tick handler for week {week}")
        TICK_REGISTRY[week] = fn
        return fn

    return decorator


def _autoload_weeks() -> None:
    """Import every ``week_*.py`` sibling so their decorator calls run."""
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("week_"):
            importlib.import_module(f"{__name__}.{mod.name}")


_autoload_weeks()
logger.info(
    "Loaded %d /return user handlers: %s; manage subcommands for weeks: %s; ticks for weeks: %s",
    len(REGISTRY),
    sorted(REGISTRY),
    {w: sorted(subs) for w, subs in MANAGE_REGISTRY.items()},
    sorted(TICK_REGISTRY),
)

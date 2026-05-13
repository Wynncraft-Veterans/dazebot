"""Per-week `/return` handlers.

Each week's implementation lives in `week_NN.py` and registers itself via the
`@register(week)` decorator. The `Returns` cog (in `cogs/return_cmd.py`)
imports this package, which triggers `_autoload_weeks()` and populates the
`REGISTRY` mapping. The cog then dispatches `/return <id> ...` by looking up
`REGISTRY[id]`.

To add week N:
  1. Create `cogs/returns/week_<N>.py`.
  2. Define an async function and decorate it with `@register(<N>)`.
  3. The signature must be `(ctx, *, flag: bool = False, **kwargs)` (extra
     kwargs reserved for future expandable args).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Awaitable, Callable

from discord.ext import commands

logger = logging.getLogger("dazebot.cogs.events.returns")

Handler = Callable[..., Awaitable[None]]
REGISTRY: dict[int, Handler] = {}


def register(week: int):
    def decorator(fn: Handler) -> Handler:
        if week in REGISTRY:
            raise RuntimeError(f"Duplicate /return handler for week {week}")
        REGISTRY[week] = fn
        return fn

    return decorator


def _autoload_weeks() -> None:
    """Import every `week_*.py` sibling so their `@register` calls run."""
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("week_"):
            importlib.import_module(f"{__name__}.{mod.name}")


_autoload_weeks()
logger.info(f"Loaded {len(REGISTRY)} /return handlers: {sorted(REGISTRY)}")


async def dispatch(ctx: commands.Context, week: int, **kwargs) -> None:
    handler = REGISTRY.get(week)
    if handler is None:
        await ctx.send(f"No `/return` handler registered for week {week}.")
        return
    await handler(ctx, **kwargs)

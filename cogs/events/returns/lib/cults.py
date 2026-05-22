"""Helpers shared between Return 0's command surfaces.

Cults are a Return-0 concept, so anything tied to ``Cult`` /
``CultMembership`` lookups lives here rather than in
:mod:`cogs.events.returns._common`. Surfaces that need the helpers:

* ``/return 0`` user UI (figurehead detection, "you're already in X")
* ``~manage_return 0 addressOwnCult`` (custom_check + the body call both
  go through :func:`lookup_owned_cult`)

If another Return ever needs to talk to cults — currently not planned — it
should import from this module, not duplicate the queries.
"""

from __future__ import annotations

from typing import Optional

from discord.ext import commands

from orm import Cult, DiscordAccount


async def is_figurehead(ctx: commands.Context) -> bool:
    """True if ``ctx.author`` owns at least one Cult (via their linked MC).

    Shaped to match the ``ManageCustomCheck`` signature so it plugs
    directly into ``@register_manage(custom_check=is_figurehead)``.
    """
    cult = await lookup_owned_cult(ctx.author.id)
    return cult is not None


async def lookup_owned_cult(user_id: int) -> Optional[Cult]:
    """Return the Cult ``user_id`` is the figurehead of, or ``None``.

    Resolves through ``DiscordAccount.minecraft_account_id`` since cult
    ownership is keyed off the linked MC account, not the Discord user.
    """
    disc = await DiscordAccount.filter(disc_uuid=str(user_id)).first()
    if disc is None or disc.minecraft_account_id is None:
        return None
    return await Cult.filter(owner_id=disc.minecraft_account_id).first()

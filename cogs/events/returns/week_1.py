"""Week 1: pick a random Returners member and print their MC username."""

from __future__ import annotations

import logging
import random

import discord
from discord.ext import commands

from cogs.events.returns import register
from lib.mc.wynn_api.errors import WynnApiError
from lib.mc.wynn_api.guild import get_guild

logger = logging.getLogger("dazebot.cogs.events.returns.week_1")


@register(1)
async def handle(ctx: commands.Context, **kwargs) -> None:
    try:
        guild = await get_guild("Returners")
    except WynnApiError:
        logger.exception("Wynncraft guild fetch failed")
        await ctx.send("Couldn't reach the Wynncraft guild API — try again in a bit.")
        return

    members = list(guild.members.all_members())
    if not members:
        await ctx.send("The guild appears to have no members right now.")
        return

    pick = random.choice(members)
    await ctx.send(pick.username, allowed_mentions=discord.AllowedMentions.none())

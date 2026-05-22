"""Week 1: random Returners-guild member picker.

``/return 1`` returns an ephemeral message with a "🎲 Pick a winner" button.
Each click hits the Wynncraft API for the current Returners roster and
posts the picked username as a public message in the invoking channel.

A 30 s per-user cooldown is enforced in-memory on the View — Discord
ratelimits the slash command itself but doesn't gate button clicks, so the
gate has to live in the callback.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from cogs.events.returns import Tier, register
from lib.mc.wynn_api.errors import WynnApiError
from lib.mc.wynn_api.guild import get_guild

logger = logging.getLogger("dazebot.cogs.events.returns.week_1")


_COOLDOWN = timedelta(seconds=30)


class _PickWinnerView(discord.ui.View):
    """One-button view bound to a single ``/return 1`` invocation.

    Not registered as persistent — each invocation gets a fresh instance.
    The per-user cooldown dict lives on the instance, which is fine: every
    user gets their own freshly-created View (the ephemeral message is
    private to them).
    """

    def __init__(self):
        super().__init__(timeout=180)
        self._last_click: dict[int, datetime] = {}

    @discord.ui.button(label="🎲 Pick a winner", style=discord.ButtonStyle.primary)
    async def pick(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        now = datetime.now(timezone.utc)
        last = self._last_click.get(interaction.user.id)
        if last is not None:
            elapsed = now - last
            if elapsed < _COOLDOWN:
                remaining = int((_COOLDOWN - elapsed).total_seconds()) + 1
                await interaction.response.send_message(
                    f"⏳ Wait {remaining}s before picking again.", ephemeral=True
                )
                return

        await interaction.response.defer(ephemeral=True, thinking=False)

        try:
            guild = await get_guild("Returners")
        except WynnApiError:
            logger.exception("Wynncraft guild fetch failed")
            await interaction.followup.send(
                "Couldn't reach the Wynncraft guild API — try again in a bit.",
                ephemeral=True,
            )
            return

        members = list(guild.members.all_members())
        if not members:
            await interaction.followup.send(
                "The guild appears to have no members right now.", ephemeral=True
            )
            return

        # Commit the cooldown only after we know the pick will succeed —
        # API failure shouldn't burn the caller's 30 s slot.
        self._last_click[interaction.user.id] = now

        pick = random.choice(members)
        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            await interaction.followup.send(pick.username, ephemeral=True)
            return
        await channel.send(
            pick.username, allowed_mentions=discord.AllowedMentions.none()
        )


@register(1, tier=Tier.REGISTERED)
async def handle(ctx: commands.Context) -> None:
    await ctx.reply(
        "Pick a random winner from the Returners guild roster:",
        view=_PickWinnerView(),
        ephemeral=True,
    )

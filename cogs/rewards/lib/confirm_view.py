"""Reusable Yes/No confirmation view for ``~ctp gift`` / ``~ctp glints
bid`` and other commands that present an "Are you sure?" prompt.

Non-persistent (timeout-based) so there's no need to register it in
``bot.setup_hook`` — a stale button just becomes inert after the
timeout. ``interaction_check`` gates clicks to the original invoker so
a bystander can't confirm someone else's gift.
"""

from __future__ import annotations

import discord


class ConfirmView(discord.ui.View):
    """Two-button view exposing ``confirmed: bool | None`` after the user
    clicks or the timeout fires.

    Typical usage from a cog::

        view = ConfirmView(author_id=ctx.author.id)
        msg = await ctx.reply("Are you sure?", view=view)
        await view.wait()
        if view.confirmed:
            ...
        else:
            await msg.edit(content="Cancelled.", view=None)
    """

    def __init__(self, *, author_id: int, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the original invoker can answer this prompt.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.confirmed = False
        await interaction.response.defer()
        self.stop()

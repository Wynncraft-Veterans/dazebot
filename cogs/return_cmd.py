import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import Bot
from cogs import returns
from cogs.returns.week_0 import backfill_cult_threads, do_join_by_name
from orm import Cult

logger = logging.getLogger("dazebot.cogs.return_cmd")


class Returns(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Initialized Returns")

    async def cog_load(self) -> None:
        # Threads need the gateway-cached parent channel; wait for ready.
        self.bot.loop.create_task(self._run_thread_backfill())

    async def _run_thread_backfill(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await backfill_cult_threads(self.bot)
        except Exception:
            logger.exception("cult thread backfill failed")

    @commands.hybrid_command(name="return")
    async def return_cmd(
        self,
        ctx: commands.Context,
        id: commands.Range[int, 0, 32767],
        action: Optional[str] = None,
        cult: Optional[str] = None,
        owner: Optional[str] = None,
        target: Optional[discord.Member] = None,
        flag: bool = False,
        # Future stubs — append optional params with defaults so existing
        # invocations keep working. Forwarded to per-week handlers as kwargs.
        # count: commands.Range[int, 0] = 0,
        # note: str = "",
    ):
        """Dispatch to the week-`id` `/return` handler."""
        await returns.dispatch(
            ctx, id, flag=flag, action=action, cult=cult, owner=owner, target=target,
        )

    @commands.hybrid_command(
        name="joincult",
        description="Join a cult (mutually exclusive). Shortcut for /return 0 join.",
    )
    @app_commands.describe(cult="The cult to join. Pick from the dropdown.")
    async def joincult(self, ctx: commands.Context, cult: str):
        await do_join_by_name(ctx, cult)

    @joincult.autocomplete("cult")
    async def _joincult_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await _cult_choices(current)


async def _cult_choices(current: str) -> list[app_commands.Choice[str]]:
    rows = await Cult.all().limit(50)
    q = current.lower()
    choices: list[app_commands.Choice[str]] = []
    for c in rows:
        if q and q not in c.name:
            continue
        choices.append(app_commands.Choice(name=c.name.capitalize(), value=c.name))
        if len(choices) >= 25:  # Discord caps autocomplete at 25
            break
    return choices


async def setup(bot: Bot):
    await bot.add_cog(Returns(bot))
    logger.info("Loaded Returns")

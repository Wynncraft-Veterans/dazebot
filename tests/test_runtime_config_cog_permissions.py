"""Permission-gate regression tests for ``/config`` and ``/alerts``.

Both groups were open to **any user** until 2026-08-30, while
``COMMAND-PERMISSIONS.md`` documented the whole surface as ADMIN. The cause
is a discord.py behaviour that reads like inheritance and isn't: a
``commands.check`` on a ``HybridGroup`` never runs for its subcommands.
``HybridGroup.__init__`` forces ``invoke_without_command = True``, so
``Group.invoke`` skips ``prepare()`` — and therefore ``can_run()`` — before
dispatching to the child, and ``Command.can_run`` evaluates only
``self.checks`` with no parent walk. On the slash path
``HybridAppCommand._check_can_run`` likewise consults only the
subcommand's own checks plus the parent's ``interaction_check``, which is
the inherited default returning True.

So every subcommand needs its own decorator, and nothing about the source
makes a missing one look wrong. That is what these tests are for: they
walk the real command tree rather than the source, so a subcommand added
without a check fails here instead of shipping open.
"""

from __future__ import annotations

import discord
import pytest
from discord.ext import commands

from cogs.membership.runtime_config_cog import RuntimeConfigCog


@pytest.fixture
async def cog():
    bot = commands.Bot(command_prefix="~", intents=discord.Intents.none())
    c = RuntimeConfigCog(bot)
    await bot.add_cog(c)
    return bot


def _tiers(cmd) -> set[str]:
    """The auth decorators applied directly to ``cmd``, by name."""
    return {
        p.__qualname__.split(".")[0]
        for p in getattr(cmd, "checks", [])
        if "<locals>" in getattr(p, "__qualname__", "")
    }


def _subcommands(bot, group_name: str):
    group = bot.get_command(group_name)
    assert isinstance(group, commands.Group), group_name
    return sorted(group.commands, key=lambda c: c.name)


async def test_a_group_check_really_does_not_reach_subcommands(cog):
    """The premise. If discord.py ever starts inheriting group checks this
    fails, and the per-subcommand decorators become removable."""
    group = cog.get_command("alerts")
    assert group.invoke_without_command is True
    sub = cog.get_command("alerts status")
    # Nothing links the two: the child's checks are its own.
    assert sub.checks is not group.checks


@pytest.mark.parametrize("group_name", ["config", "alerts"])
async def test_every_subcommand_carries_its_own_check(cog, group_name):
    naked = [c.qualified_name for c in _subcommands(cog, group_name) if not _tiers(c)]
    assert not naked, f"ungated subcommand(s): {naked}"


async def test_alerts_is_staff(cog):
    """Steward and up. Muting a noisy alert is duty work — gating it on the
    Discord Administrator permission locked out the people who actually
    watch the alert channel."""
    for cmd in _subcommands(cog, "alerts"):
        assert _tiers(cmd) == {"is_staff"}, cmd.qualified_name
    assert _tiers(cog.get_command("alerts")) == {"is_staff"}


async def test_config_stays_admin(cog):
    """``/config set`` can write any key on ``Config`` — a strictly larger
    surface than silencing one alert, including the alert keys themselves."""
    for cmd in _subcommands(cog, "config"):
        assert _tiers(cmd) == {"is_admin"}, cmd.qualified_name
    assert _tiers(cog.get_command("config")) == {"is_admin"}


async def test_the_alerts_surface_is_the_one_we_think_it_is(cog):
    """Pins the roster so a new subcommand has to be classified here (and
    in COMMAND-PERMISSIONS.md) rather than sliding in unnoticed."""
    assert {c.name for c in _subcommands(cog, "alerts")} == {
        "status", "mute", "unmute", "thresholds",
        "hiatus_mute", "hiatus_unmute",
        "return_dm_on", "return_dm_off", "return_dm_check", "return_dm_clear",
    }

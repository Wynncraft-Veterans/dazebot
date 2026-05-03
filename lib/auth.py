import discord
from discord.ext import commands

from config import CurrConfig


def is_moderator():
    async def predicate(ctx: commands.Context):
        return ctx.author.id in CurrConfig.MODERATORS

    return commands.check(predicate)


def is_admin():
    async def predicate(ctx: commands.Context):
        return ctx.author.id in CurrConfig.ADMINS

    return commands.check(predicate)


def is_staff():
    """Allow anyone with the configured STAFF_ROLE OR the legacy MODERATORS list
    OR the discord 'administrator' permission to run the command.

    See .vscode/instructions1.md §2a.
    """

    async def predicate(ctx: commands.Context):
        if ctx.author.id in CurrConfig.MODERATORS:
            return True
        if isinstance(ctx.author, discord.Member):
            if ctx.author.guild_permissions.administrator:
                return True
            if any(r.id == CurrConfig.STAFF_ROLE for r in ctx.author.roles):
                return True
        return False

    return commands.check(predicate)


def is_server_admin():
    """Server administrator permission OR config ADMINS allowlist."""

    async def predicate(ctx: commands.Context):
        if ctx.author.id in CurrConfig.ADMINS:
            return True
        if isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator:
            return True
        return False

    return commands.check(predicate)

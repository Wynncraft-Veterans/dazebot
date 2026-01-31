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

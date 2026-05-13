"""Custom discord.py converters.

``CaseInsensitiveMember`` matches a member by mention/id first, then
falls back to a case-insensitive sweep across ``name``, ``display_name``,
``global_name``, and ``nick``.
"""

import discord
from discord.ext import commands


def _or[T](ls: list[T | None]) -> T | None:
    for item in ls:
        if item is not None:
            return item
    return None


class CaseInsensitiveMember(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str) -> discord.Member:
        # Try default converter first (handles mentions, IDs, case-sensitive names)
        try:
            return await commands.MemberConverter().convert(ctx, argument)
        except commands.MemberNotFound:
            pass

        if ctx.guild is None:
            raise commands.MemberNotFound(argument)

        # i did it like this so you can change the order of priority
        # "_" is case sensitive, im not 100% if discord checks all of these by default
        # "low" is case insensitive
        # you could also remove the `return await commands.MemberConverter().convert(ctx, argument)`
        #  line above if you want to reorder case sensitive and insensitive checks through eachother
        filters = [
            ("_", lambda m: m.name),
            ("_", lambda m: m.display_name),
            ("_", lambda m: m.global_name),
            ("_", lambda m: m.nick),
            ("low", lambda m: m.name),
            ("low", lambda m: m.display_name),
            ("low", lambda m: m.global_name),
            ("low", lambda m: m.nick),
        ]
        # TODO, maybe just create two lambdas and reuse them rather than "_" and "low"?

        check = (
            lambda argument, case, alias: lambda m: alias(m).lower() == argument.lower()
            if (alias(m) is not None and case == "low")
            else alias(m) == argument
        )

        member = _or([discord.utils.find(check(argument, case, alias), ctx.guild.members) for case, alias in filters])

        if member:
            return member

        raise commands.MemberNotFound(argument)

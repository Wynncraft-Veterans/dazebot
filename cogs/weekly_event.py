import logging
from typing import Annotated, TypedDict
import discord
from discord.ext import commands
from lib.auth import is_admin
from lib.converters import CaseInsensitiveMember
from lib.discord_paginated_embed import Paginator, from_lines
from orm import DiscordAccount, Score, WeeklyEvent as WeeklyEventTable

# logger = Logger()
logger = logging.getLogger("dazebot.cogs.weeklyevent")
from bot import Bot

WeekRange = commands.Range[int, 0]
ValueRange = commands.Range[int, 0]


class WeeklyEvent(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("API cog initialized")

    @commands.hybrid_group(name="score")
    async def score(self, ctx: commands.Context):
        """Score management commands"""
        if ctx.invoked_subcommand is None:
            await ctx.send("Use: add, set, or leaderboard")

    @score.command(name="set")
    @is_admin()
    async def score_set(
        self,
        ctx: commands.Context,
        user: Annotated[discord.Member, CaseInsensitiveMember],
        week: WeekRange,
        value: ValueRange,
    ):
        """Set user's points"""

        logger.debug(f"Setting {user.id=} score in {week=} to {value=}")

        disc_account, _ = await DiscordAccount.get_or_create(disc_uuid=str(user.id))

        event, _ = await WeeklyEventTable.get_or_create(week=week)

        await Score.update_or_create(event=event, discord_account=disc_account, defaults={"score": value})

        await ctx.send(
            f"Set {user.mention}'s score to {value} for week {week}", allowed_mentions=discord.AllowedMentions.none()
        )

    @score.command(name="add")
    @is_admin()
    async def score_add(
        self, ctx, user: Annotated[discord.Member, CaseInsensitiveMember], week: WeekRange, value: ValueRange
    ):
        """Add points to a user"""

        disc_account, _ = await DiscordAccount.get_or_create(disc_uuid=str(user.id))

        event, _ = await WeeklyEventTable.get_or_create(week=week)

        score_obj, created = await Score.get_or_create(
            event=event, discord_account=disc_account, defaults={"score": value}
        )

        if not created:
            score_obj.score += value
            await score_obj.save(update_fields=["score"])

        await ctx.send(
            f"Added {value} points to {user.mention} for week {week}", allowed_mentions=discord.AllowedMentions.none()
        )

    @score.command(name="print")
    async def score_print(self, ctx, user: Annotated[discord.Member, CaseInsensitiveMember], week: WeekRange):
        disc_account, _ = await DiscordAccount.get_or_create(disc_uuid=str(user.id))

        event, _ = await WeeklyEventTable.get_or_create(week=week)

        score = await Score.filter(event=event, discord_account=disc_account).first()

        if score:
            await ctx.send(
                f"{user.mention} has {score.score} points for week {week}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.send(
                f"{user.mention} does not have any points registered for week {week}",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @score.command(name="leaderboard")
    async def score_leaderboard(self, ctx: commands.Context, week: WeekRange, amount: commands.Range[int, 10, 20] = 10):
        event, _ = await WeeklyEventTable.get_or_create(week=week)

        # Get top scores
        scores = (
            await Score.filter(event=event, score__gt=0)
            .order_by("-score")
            .limit(amount)
            .prefetch_related("discord_account")
        )

        if scores:
            text = "\n".join(f"<@{score.discord_account.disc_uuid}> {score.score}" for score in scores)
            await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())
        else:
            await ctx.send(f"No one has any points for week {week}")

    @commands.hybrid_command(name="count")
    async def count_reactions(
        self, ctx: commands.Context, channel: discord.ForumChannel, override_emoji: str | None = None
    ):
        emoji = next(e for e in (channel.default_reaction_emoji, override_emoji, "✅") if e is not None)

        threads = channel.threads
        threads.extend([thread async for thread in channel.archived_threads(limit=None)])
        logger.info(threads)
        if not threads:
            await ctx.send("No posts in this forum channel")
            return

        Post = TypedDict("Post", {"msg": discord.Message, "count": int})
        posts: list[Post] = []
        for thread in threads:
            try:
                msg = await thread.fetch_message(thread.id)

                reactors = []
                for reaction in msg.reactions:
                    if reaction.emoji != emoji:  # TODO this is so scuffed lol
                        continue
                    reactors.extend([u async for u in reaction.users() if u.id != msg.author.id])
                posts.append({"msg": msg, "count": len(reactors)})
            except Exception as e:
                await ctx.send("Something failed, check logs.")
                raise e
        posts.sort(key=lambda e: e["count"], reverse=True)
        logger.info(posts)

        lines = []
        for idx, item in enumerate(posts, start=1):
            rank = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"#{idx}"
            lines.append(f"{rank} {item['msg'].author.mention} {emoji} {item['count']}")

        embeds = from_lines(f"Scoreboard: {channel.name}", lines, 10, logger)
        await ctx.send(embed=embeds[0], view=Paginator(embeds))


async def setup(bot: Bot):
    await bot.add_cog(WeeklyEvent(bot))
    logger.info("API cog loaded successfully")

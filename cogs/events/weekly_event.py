"""Weekly-event cog: ``/score`` group (set/add/print/leaderboard) and
``/count`` for forum-channel reaction tallies."""

import logging
from typing import TypedDict
import discord
from discord.ext import commands
from lib.auth import is_staff
from lib.discord_utils.paginated_embed import Paginator, from_lines
from lib.mc.resolve import resolve_target
from orm import DiscordAccount, Score, WeeklyEvent as WeeklyEventTable

# logger = Logger()
logger = logging.getLogger("dazebot.cogs.events.weekly_event")
from bot import Bot

WeekRange = commands.Range[int, 0]
ValueRange = commands.Range[int, 0]


async def _resolve_score_target(
    ctx: commands.Context, target: str
) -> tuple[str, DiscordAccount] | None:
    """Resolve ``target`` (Discord ping/id/name OR Minecraft username/UUID) to
    ``(mention, DiscordAccount)`` for score-table operations. Replies with a
    user-facing error and returns ``None`` if no DiscordAccount can be
    attached (unknown target, or MC account exists but isn't linked).
    """
    member, mc = await resolve_target(ctx, target)
    if member is not None:
        disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(member.id))
        return member.mention, disc
    if mc is not None:
        disc = await DiscordAccount.filter(minecraft_account_id=mc.id).first()
        if disc is None:
            await ctx.reply(
                f"Minecraft account `{mc.mc_username}` is registered but not "
                "linked to a Discord account."
            )
            return None
        return f"<@{disc.disc_uuid}>", disc
    await ctx.reply(f"Could not find a Discord member or Minecraft account matching `{target}`.")
    return None


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
    @is_staff()
    async def score_set(
        self,
        ctx: commands.Context,
        target: str,
        week: WeekRange,
        value: ValueRange,
    ):
        """Set user's points. ``target`` accepts a Discord ping/id/name or a Minecraft username/UUID."""

        resolved = await _resolve_score_target(ctx, target)
        if resolved is None:
            return
        mention, disc_account = resolved

        logger.debug(f"Setting {disc_account.disc_uuid=} score in {week=} to {value=}")

        event, _ = await WeeklyEventTable.get_or_create(week=week)
        await Score.update_or_create(event=event, discord_account=disc_account, defaults={"score": value})

        await ctx.send(
            f"Set {mention}'s score to {value} for week {week}", allowed_mentions=discord.AllowedMentions.none()
        )

    @score.command(name="add")
    @is_staff()
    async def score_add(
        self, ctx: commands.Context, target: str, week: WeekRange, value: ValueRange
    ):
        """Add points to a user. ``target`` accepts a Discord ping/id/name or a Minecraft username/UUID."""

        resolved = await _resolve_score_target(ctx, target)
        if resolved is None:
            return
        mention, disc_account = resolved

        event, _ = await WeeklyEventTable.get_or_create(week=week)

        score_obj, created = await Score.get_or_create(
            event=event, discord_account=disc_account, defaults={"score": value}
        )

        if not created:
            score_obj.score += value
            await score_obj.save(update_fields=["score"])

        await ctx.send(
            f"Added {value} points to {mention} for week {week}", allowed_mentions=discord.AllowedMentions.none()
        )

    @score.command(name="print")
    async def score_print(self, ctx: commands.Context, target: str, week: WeekRange):
        """Print a user's score. ``target`` accepts a Discord ping/id/name or a Minecraft username/UUID."""
        resolved = await _resolve_score_target(ctx, target)
        if resolved is None:
            return
        mention, disc_account = resolved

        event, _ = await WeeklyEventTable.get_or_create(week=week)

        score = await Score.filter(event=event, discord_account=disc_account).first()

        if score:
            await ctx.send(
                f"{mention} has {score.score} points for week {week}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.send(
                f"{mention} does not have any points registered for week {week}",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @score.command(name="leaderboard")
    async def score_leaderboard(self, ctx: commands.Context, week: WeekRange):
        event, _ = await WeeklyEventTable.get_or_create(week=week)

        scores = (
            await Score.filter(event=event, score__gt=0)
            .order_by("-score")
            .prefetch_related("discord_account")
        )

        if not scores:
            await ctx.send(f"No one has any points for week {week}")
            return

        lines = [f"<@{score.discord_account.disc_uuid}> {score.score}" for score in scores]
        embeds = from_lines(f"Leaderboard — week {week}", lines, 10, logger)
        await ctx.send(embed=embeds[0], view=Paginator(embeds))

    @commands.hybrid_command(name="count")
    @is_staff()
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

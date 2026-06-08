"""The ``~ctp`` (Chore-Torn Palace) command cog.

The cog is intentionally a thin shim layer: every command resolves the
invoker / target into a ``DiscordAccount``, dispatches to a function in
``cogs.rewards.lib``, and renders the result. The point math, the
ledger writes, and the leaderboard eligibility logic all live in the
service modules so they can be exercised without standing up a bot.

Permission tiers (lib/auth.py): ``@is_registered`` for the user-facing
core, ``@is_guild`` for glints, ``@is_staff`` for staff CRUD, and
``@is_admin`` for board + prize catalog management.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from bot import Bot
from cogs.rewards.ctp.lib import balance as balance_svc
from cogs.rewards.ctp.lib import boards as boards_svc
from cogs.rewards.ctp.lib import formatting
from cogs.rewards.ctp.lib import glints as glints_svc
from cogs.rewards.ctp.lib import prizes as prizes_svc
from cogs.rewards.lib.confirm_view import ConfirmView
from config import CurrConfig
from lib.auth import Tier, current_tier, is_admin, is_guild, is_registered, is_staff
from lib.discord_utils.converters import CaseInsensitiveMember
from lib.discord_utils.paginated_embed import Paginator, from_lines
from orm import CTPBoard, DiscordAccount

logger = logging.getLogger("dazebot.cogs.rewards.ctp")


HISTORY_LINES_PER_PAGE = 10
GLINTS_LINES_PER_PAGE = 20
INFO_HISTORY_LIMIT = 15  # how many recent rows ``~ctp info`` shows inline

# Crossing this lifetime-received total (sum of all positive ledger
# deltas, never decremented by spends) grants the role below. Additive
# only — there is no strip path, since you can't un-receive points. The
# threshold fires from every points-add path (reward, gift, admin set)
# and from a one-shot startup backfill in ``on_ready``.
LIFETIME_RECEIVED_ROLE_THRESHOLD = 88
LIFETIME_RECEIVED_ROLE_ID = 1512981177760481300


async def _resolve_target_disc(
    ctx: commands.Context, value: str
) -> tuple[Optional[discord.Member], Optional[DiscordAccount]]:
    """Resolve a ``<user>`` command arg to a ``(member, DiscordAccount)``
    pair. The Discord side may be ``None`` if the input matched only a
    DB row (rare but supported); the DB side may be ``None`` only when
    the input matched neither a member nor an existing account.
    """
    member: Optional[discord.Member] = None
    try:
        member = await CaseInsensitiveMember().convert(ctx, value)
    except commands.MemberNotFound:
        pass

    if member is not None:
        disc = await balance_svc.get_or_create_disc(str(member.id))
        return member, disc

    disc = await DiscordAccount.filter(disc_uuid=value).first()
    if disc is not None:
        return None, disc
    return None, None


async def _board_memberships(
    disc: DiscordAccount, member: Optional[discord.Member]
) -> list[str]:
    """Union of manual (``~ctp assign``-written) board enums and the
    role-derived ones. Role detection is skipped when ``member`` is None
    (target left the server / not resolvable), so manual rows still
    surface in ``~ctp info`` for absent users.
    """
    enums: set[str] = set(await boards_svc.list_manual_enums_for(disc))
    if member is not None:
        boards = await CTPBoard.filter(role_id__isnull=False).all()
        held = {r.id for r in member.roles}
        for b in boards:
            if b.role_id is not None and int(b.role_id) in held:
                enums.add(b.enum)
    return sorted(enums)


class CTPCog(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("CTPCog initialized")

    # --- Group root ---------------------------------------------------

    @commands.hybrid_group(name="ctp", description="Chore-Torn Palace points & prizes.")
    async def ctp_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        tier = await current_tier(ctx)
        sections: list[str] = []
        if tier >= Tier.REGISTERED:
            sections.append(
                "**Registered**\n"
                "- `~ctp status` — your CTP balance & glint rank\n"
                "- `~ctp history` — your full CTP history\n"
                "- `~ctp gift <user> <points>` — gift points to another user\n"
                "- `~ctp prize info` — browse the prize catalog"
            )
        if tier >= Tier.GUILD:
            sections.append(
                "**Guild**\n"
                "- `~ctp glints bids` — glint-investment leaderboard\n"
                "- `~ctp glints bid <n>` — invest in glints (irreversible)"
            )
        if tier >= Tier.STAFF:
            sections.append(
                "**Staff**\n"
                "- `~ctp balance <user>` — show a user's CTP balance\n"
                "- `~ctp info <user>` — full CTP info for a user\n"
                "- `~ctp reward <user> <board> <task#> <points> <comment>` — award points\n"
                "- `~ctp redeem <user> <category> <enum>` — redeem a prize\n"
                "- `~ctp access` — list currently-active redemptions\n"
                "- `~ctp assign <user> <ENUM>` — add a user to a board\n"
                "- `~ctp revoke <user> <ENUM>` — remove a user from a board"
            )
        if tier >= Tier.ADMIN:
            sections.append(
                "**Admin**\n"
                "- `~ctp board <ENUM> <num> [role_id]` — create/move/role-change/delete a board\n"
                "- `~ctp prize add|edit|disable|enable|remove|disclaim ...` — prize catalog CRUD\n"
                "- `~ctp set <user> <value>` — forcibly set a user's balance"
            )
        if not sections:
            await ctx.reply("You don't have access to any `~ctp` subcommands.")
            return
        await ctx.reply("\n\n".join(sections), allowed_mentions=discord.AllowedMentions.none())

    # --- Registered tier ----------------------------------------------

    @ctp_group.command(name="status", description="(Registered) Show your CTP balance and glint rank.")
    @is_registered()
    async def ctp_status(self, ctx: commands.Context) -> None:
        disc = await balance_svc.get_or_create_disc(str(ctx.author.id))
        bal = await balance_svc.compute_balance(disc)
        rank, total, glinted = await glints_svc.rank_of(disc=disc, bot=self.bot)
        member = ctx.author if isinstance(ctx.author, discord.Member) else None
        board_enums = await _board_memberships(disc, member)
        lines = formatting.format_status_lines(
            balance=bal,
            glint_rank=rank,
            is_glinted=glinted,
            glint_total=total,
            board_enums=board_enums,
        )
        await ctx.reply("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @ctp_group.command(name="history", description="(Registered) Show your full CTP history.")
    @is_registered()
    async def ctp_history(self, ctx: commands.Context) -> None:
        disc = await balance_svc.get_or_create_disc(str(ctx.author.id))
        entries = await balance_svc.list_history(disc)
        if not entries:
            await ctx.reply("You have no CTP history yet.")
            return
        lines = formatting.format_history_lines(entries)
        embeds = from_lines("Your CTP history", lines, HISTORY_LINES_PER_PAGE, logger)
        view = Paginator(embeds) if len(embeds) > 1 else None
        await ctx.reply(embed=embeds[0], view=view, allowed_mentions=discord.AllowedMentions.none())

    @ctp_group.command(name="gift", description="(Registered) Gift CTP points to another user.")
    @is_registered()
    async def ctp_gift(
        self, ctx: commands.Context, user: CaseInsensitiveMember, points: int
    ) -> None:
        if points <= 0:
            await ctx.reply("You can only gift a positive number of points.")
            return
        receiver_member: discord.Member = user  # type: ignore[assignment]
        if receiver_member.id == ctx.author.id:
            await ctx.reply("You can't gift points to yourself.")
            return
        sender = await balance_svc.get_or_create_disc(str(ctx.author.id))
        receiver = await balance_svc.get_or_create_disc(str(receiver_member.id))
        sender_balance = await balance_svc.compute_balance(sender)
        if sender_balance < points:
            await ctx.reply(
                f"You only have {sender_balance} points — can't gift {points}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        view = ConfirmView(author_id=ctx.author.id)
        prompt = await ctx.reply(
            f"Are you sure you wish to give **{points}** points to {receiver_member.mention}? "
            f"This will leave you with **{sender_balance - points}** points.",
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await view.wait()
        if not view.confirmed:
            await prompt.edit(content="Cancelled.", view=None)
            return
        await balance_svc.transfer_points(
            sender=sender, receiver=receiver, amount=points, actor_disc_uuid=str(ctx.author.id),
        )
        await self._maybe_grant_lifetime_role(ctx.guild, receiver_member, receiver)
        await prompt.edit(
            content=f"Confirmed! Gifted **{points}** points to {receiver_member.mention}.",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --- Guild tier (glints) ------------------------------------------

    @ctp_group.group(name="glints", description="(Guild) Glint-investment standings.")
    async def glints_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.reply("Use `~ctp glints bids` or `~ctp glints bid <number>`.")

    @glints_group.command(name="bids", description="(Guild) List the top glint bidders.")
    @is_guild()
    async def glints_bids(self, ctx: commands.Context) -> None:
        glinted, standby = await glints_svc.leaderboard(self.bot)
        if not glinted and not standby:
            await ctx.reply("No glint investments yet.")
            return
        lines = formatting.format_glint_leaderboard_lines(glinted, standby)
        embeds = from_lines("Invested Points in Glints", lines, GLINTS_LINES_PER_PAGE, logger)
        view = Paginator(embeds) if len(embeds) > 1 else None
        await ctx.reply(embed=embeds[0], view=view, allowed_mentions=discord.AllowedMentions.none())

    @glints_group.command(name="bid", description="(Guild) Add to your glint investment (irreversible).")
    @is_guild()
    async def glints_bid(self, ctx: commands.Context, number: int) -> None:
        if number <= 0:
            await ctx.reply("You can only invest a positive number of points.")
            return
        disc = await balance_svc.get_or_create_disc(str(ctx.author.id))
        bal = await balance_svc.compute_balance(disc)
        if bal < number:
            await ctx.reply(
                f"You only have {bal} points — can't invest {number}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        view = ConfirmView(author_id=ctx.author.id)
        prompt = await ctx.reply(
            f"Are you sure you would like to spend **{number}** of your **{bal}** points on glints?\n"
            f"Only the top {glints_svc.GLINT_VISIBLE_CUTOFF} active glint investors will be glinted "
            "(current vets members, waitlist, or honourary only).\n"
            "Points invested this way are cumulative and will never expire.",
            view=view,
        )
        await view.wait()
        if not view.confirmed:
            await prompt.edit(content="Cancelled.", view=None)
            return
        _, new_total = await glints_svc.invest(
            disc=disc, amount=number, actor_disc_uuid=str(ctx.author.id),
        )
        rank, _, is_glinted = await glints_svc.rank_of(disc=disc, bot=self.bot)
        if rank is None:
            tail = "You currently aren't eligible for the leaderboard, but your investment is recorded."
        else:
            verb = "is" if is_glinted else "is not"
            tail = (
                f"This puts you in position **{rank}**, which **{verb}** enough to get your glint!"
            )
        await prompt.edit(
            content=(
                f"Confirmed! You have added **{number}** points, bringing your glint investment to **{new_total}**.\n{tail}"
            ),
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --- Staff tier ---------------------------------------------------

    @ctp_group.command(name="balance", description="(Staff) Show a user's CTP balance.")
    @is_staff()
    async def ctp_balance(self, ctx: commands.Context, *, user: str) -> None:
        member, disc = await _resolve_target_disc(ctx, user)
        if disc is None:
            await ctx.reply(f"No CTP account found for `{user}`.")
            return
        bal = await balance_svc.compute_balance(disc)
        who = member.mention if member is not None else f"<@{disc.disc_uuid}>"
        await ctx.reply(f"{who} has **{bal}** CTP points.", allowed_mentions=discord.AllowedMentions.none())

    @ctp_group.command(name="info", description="(Staff) Show full CTP info for a user.")
    @is_staff()
    async def ctp_info(self, ctx: commands.Context, *, user: str) -> None:
        member, disc = await _resolve_target_disc(ctx, user)
        if disc is None:
            await ctx.reply(f"No CTP account found for `{user}`.")
            return
        bal = await balance_svc.compute_balance(disc)
        history = await balance_svc.list_history(disc, limit=INFO_HISTORY_LIMIT)
        board_enums = await _board_memberships(disc, member)
        header = [
            f"<@{disc.disc_uuid}> has **{bal}** CTP points.",
            f"Boards: {', '.join(board_enums) if board_enums else '(none)'}",
            "History:" if history else "History: (empty)",
        ]
        lines = header + formatting.format_info_history_lines(history)
        embeds = from_lines(f"CTP info — {member.display_name if member else disc.disc_uuid}", lines, HISTORY_LINES_PER_PAGE, logger)
        view = Paginator(embeds) if len(embeds) > 1 else None
        await ctx.reply(embed=embeds[0], view=view, allowed_mentions=discord.AllowedMentions.none())

    @ctp_group.command(
        name="reward",
        description="(Staff) Award CTP points for a completed task.",
    )
    @is_staff()
    async def ctp_reward(
        self,
        ctx: commands.Context,
        user: CaseInsensitiveMember,
        board_enum: str,
        task_number: int,
        points: int,
        *,
        comment: str,
    ) -> None:
        target_member: discord.Member = user  # type: ignore[assignment]
        target_disc = await balance_svc.get_or_create_disc(str(target_member.id))
        board: Optional[CTPBoard] = None
        if board_enum.upper() not in {"N/A", "NONE", "-"}:
            board = await boards_svc.find_by_enum(board_enum)
            if board is None:
                await ctx.reply(
                    f"No board `{board_enum.upper()}` — use `~ctp board {board_enum.upper()} <id> <role_id>` to create it, "
                    "or pass `N/A` to award without a board.",
                )
                return
        await balance_svc.award(
            target=target_disc,
            board=board,
            task_number=task_number,
            points=points,
            comment=comment,
            actor_disc_uuid=str(ctx.author.id),
        )
        await self._maybe_grant_lifetime_role(ctx.guild, target_member, target_disc)
        if board is not None and task_number > 0:
            tail = (
                f" on [board {board.enum}]({boards_svc.format_board_url(board)}) "
                f"for [task {task_number}]({boards_svc.format_task_url(task_number)})"
            )
        elif board is not None:
            tail = f" on [board {board.enum}]({boards_svc.format_board_url(board)})"
        else:
            tail = ""
        await ctx.reply(
            f"Awarded **{points}** points to {target_member.mention}{tail}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ctp_group.command(
        name="redeem",
        description="(Staff) Redeem a prize on behalf of a user.",
    )
    @is_staff()
    async def ctp_redeem(
        self,
        ctx: commands.Context,
        user: CaseInsensitiveMember,
        category: str,
        *,
        prize_enum: str,
    ) -> None:
        target_member: discord.Member = user  # type: ignore[assignment]
        target_disc = await balance_svc.get_or_create_disc(str(target_member.id))
        prize = await prizes_svc.resolve_loose(category, prize_enum)
        if prize is None:
            await ctx.reply(
                f"No prize matches `{category} {prize_enum}`. "
                "Use `~ctp prize info` to see what's available.",
            )
            return
        result = await prizes_svc.redeem(
            target=target_disc, prize=prize, actor_disc_uuid=str(ctx.author.id),
        )
        if not result.ok:
            await ctx.reply(result.message)
            return
        bits = [
            f"Subtracted **{prize.cost}** points from {target_member.mention} for `{prize.display}`."
        ]
        if prize.duration_seconds:
            bits.append(
                f"Reminder, this prize should last for {formatting.format_duration(prize.duration_seconds)}."
            )
        if prize.disclaimer:
            bits.append(f"*{prize.disclaimer}*")
        await ctx.reply("\n".join(bits), allowed_mentions=discord.AllowedMentions.none())

    @ctp_group.command(
        name="assign",
        description="(Staff) Add a user to a CTP board (writes the membership + grants the board's role).",
    )
    @is_staff()
    async def ctp_assign(
        self, ctx: commands.Context, user: CaseInsensitiveMember, board_enum: str,
    ) -> None:
        target_member: discord.Member = user  # type: ignore[assignment]
        board = await boards_svc.find_by_enum(board_enum)
        if board is None:
            await ctx.reply(
                f"No board `{board_enum.upper()}` — use "
                f"`~ctp board {board_enum.upper()} <board_number> <role_id>` to create it.",
            )
            return
        target_disc = await balance_svc.get_or_create_disc(str(target_member.id))
        created = await boards_svc.assign_membership(
            disc=target_disc, board=board, actor_disc_uuid=str(ctx.author.id),
        )
        role_note = await self._grant_board_role(ctx, target_member, board)
        head = (
            f"Assigned {target_member.mention} to board `{board.enum}`."
            if created
            else f"{target_member.mention} was already assigned to board `{board.enum}`."
        )
        await ctx.reply(f"{head}{role_note}", allowed_mentions=discord.AllowedMentions.none())

    @ctp_group.command(
        name="revoke",
        description="(Staff) Remove a user from a CTP board (deletes the membership + strips the board's role).",
    )
    @is_staff()
    async def ctp_revoke(
        self, ctx: commands.Context, user: CaseInsensitiveMember, board_enum: str,
    ) -> None:
        target_member: discord.Member = user  # type: ignore[assignment]
        board = await boards_svc.find_by_enum(board_enum)
        if board is None:
            await ctx.reply(f"No board `{board_enum.upper()}`.")
            return
        target_disc = await balance_svc.get_or_create_disc(str(target_member.id))
        deleted = await boards_svc.revoke_membership(disc=target_disc, board=board)
        role_note = await self._strip_board_role(ctx, target_member, board)
        head = (
            f"Revoked {target_member.mention} from board `{board.enum}`."
            if deleted
            else f"{target_member.mention} was not assigned to board `{board.enum}`."
        )
        await ctx.reply(f"{head}{role_note}", allowed_mentions=discord.AllowedMentions.none())

    async def _grant_board_role(
        self, ctx: commands.Context, member: discord.Member, board: CTPBoard,
    ) -> str:
        """One-shot role grant on assign. Returns a trailing-space-prefixed
        note for the staff reply (empty if there's nothing to say). The
        board membership is the source of truth — failure here is logged
        but does not roll back the DB write.
        """
        if board.role_id is None:
            return ""
        try:
            role_id_int = int(board.role_id)
        except ValueError:
            logger.warning("ctp assign: board %s has non-int role_id %r", board.enum, board.role_id)
            return f" (board has malformed role_id `{board.role_id}`; no role grant attempted)"
        role = ctx.guild.get_role(role_id_int) if ctx.guild is not None else None
        if role is None:
            return f" (role <@&{board.role_id}> not found in this guild; no role grant)"
        if role in member.roles:
            return f" Already had role {role.mention}."
        try:
            await member.add_roles(role, reason=f"~ctp assign by {ctx.author}")
        except discord.Forbidden:
            logger.warning("ctp assign: forbidden granting role %s to %s", role.id, member.id)
            return f" (failed to grant {role.mention}: missing permissions)"
        except discord.HTTPException as e:
            logger.warning("ctp assign: HTTPException granting role %s to %s: %s", role.id, member.id, e)
            return f" (failed to grant {role.mention}: {e})"
        return f" Granted role {role.mention}."

    async def _maybe_grant_lifetime_role(
        self,
        guild: Optional[discord.Guild],
        member: Optional[discord.Member],
        disc: DiscordAccount,
    ) -> None:
        """Grant ``LIFETIME_RECEIVED_ROLE_ID`` if ``disc``'s lifetime
        received has crossed the threshold. Safe to call on every
        points-add: no-op if under threshold, role missing, member
        absent, or already holds the role. Failures are logged but
        never re-raised — a role-grant blip must not roll back the
        ledger write that prompted it.
        """
        if guild is None or member is None:
            return
        lifetime = await balance_svc.compute_lifetime_received(disc)
        if lifetime < LIFETIME_RECEIVED_ROLE_THRESHOLD:
            return
        role = guild.get_role(LIFETIME_RECEIVED_ROLE_ID)
        if role is None:
            logger.warning(
                "lifetime-received role %s not found in guild %s",
                LIFETIME_RECEIVED_ROLE_ID, guild.id,
            )
            return
        if role in member.roles:
            return
        try:
            await member.add_roles(
                role,
                reason=f"lifetime CTP received >= {LIFETIME_RECEIVED_ROLE_THRESHOLD}",
            )
        except discord.Forbidden:
            logger.warning(
                "lifetime-received role: forbidden granting %s to %s", role.id, member.id,
            )
        except discord.HTTPException as e:
            logger.warning(
                "lifetime-received role: HTTPException granting %s to %s: %s",
                role.id, member.id, e,
            )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """One-shot backfill: catch up any account whose lifetime received
        already crossed the threshold before this rule existed (or before
        the bot last saw them). Iterates every account with at least one
        positive ledger row, so we don't load the entire DiscordAccount
        table when most users have never touched CTP.
        """
        guild = self.bot.get_guild(self.bot.config.GUILD)
        if guild is None:
            logger.error("ctp lifetime-received backfill: guild %s not found", self.bot.config.GUILD)
            return
        await guild.chunk()
        granted = 0
        skipped_absent = 0
        seen_ids: set[int] = set()
        async for row in CTPLedger.filter(amount_delta__gt=0).select_related("discord_account"):
            disc = row.discord_account
            if disc.id in seen_ids:
                continue
            seen_ids.add(disc.id)
            try:
                disc_uuid_int = int(disc.disc_uuid)
            except (TypeError, ValueError):
                continue
            member = guild.get_member(disc_uuid_int)
            if member is None:
                skipped_absent += 1
                continue
            before = LIFETIME_RECEIVED_ROLE_ID in {r.id for r in member.roles}
            await self._maybe_grant_lifetime_role(guild, member, disc)
            if not before and LIFETIME_RECEIVED_ROLE_ID in {r.id for r in member.roles}:
                granted += 1
        logger.info(
            "ctp lifetime-received backfill: scanned %d accounts, granted %d, skipped %d (absent from guild)",
            len(seen_ids), granted, skipped_absent,
        )

    async def _strip_board_role(
        self, ctx: commands.Context, member: discord.Member, board: CTPBoard,
    ) -> str:
        """One-shot role strip on revoke. Symmetric to ``_grant_board_role``."""
        if board.role_id is None:
            return ""
        try:
            role_id_int = int(board.role_id)
        except ValueError:
            logger.warning("ctp revoke: board %s has non-int role_id %r", board.enum, board.role_id)
            return f" (board has malformed role_id `{board.role_id}`; no role strip attempted)"
        role = ctx.guild.get_role(role_id_int) if ctx.guild is not None else None
        if role is None:
            return f" (role <@&{board.role_id}> not found in this guild; no role strip)"
        if role not in member.roles:
            return f" They did not have role {role.mention}."
        try:
            await member.remove_roles(role, reason=f"~ctp revoke by {ctx.author}")
        except discord.Forbidden:
            logger.warning("ctp revoke: forbidden removing role %s from %s", role.id, member.id)
            return f" (failed to remove {role.mention}: missing permissions)"
        except discord.HTTPException as e:
            logger.warning("ctp revoke: HTTPException removing role %s from %s: %s", role.id, member.id, e)
            return f" (failed to remove {role.mention}: {e})"
        return f" Removed role {role.mention}."

    @ctp_group.command(name="access", description="(Staff) List users with currently-active access prizes.")
    @is_staff()
    async def ctp_access(self, ctx: commands.Context) -> None:
        rows = await prizes_svc.active_redemptions()
        if not rows:
            await ctx.reply("No active redemptions.")
            return
        guild = self.bot.get_guild(CurrConfig.GUILD)
        members: dict[str, discord.Member] = {}
        if guild is not None:
            for r in rows:
                uuid = r.discord_account.disc_uuid
                if uuid in members:
                    continue
                try:
                    m = guild.get_member(int(uuid))
                except (TypeError, ValueError):
                    m = None
                if m is not None:
                    members[uuid] = m
        lines = formatting.format_active_redemptions(rows, members)
        embeds = from_lines("Active CTP redemptions", lines, HISTORY_LINES_PER_PAGE, logger)
        view = Paginator(embeds) if len(embeds) > 1 else None
        await ctx.reply(embed=embeds[0], view=view, allowed_mentions=discord.AllowedMentions.none())

    # --- Prize subgroup -----------------------------------------------

    @ctp_group.group(name="prize", description="(Anyone) Prize info; admin subcommands gated.")
    async def prize_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.reply(
                "Use `~ctp prize info` (registered) or "
                "`~ctp prize add/edit/enable/disable/remove/disclaim` (admin)."
            )

    @prize_group.command(name="info", description="(Registered) Show the available CTP prizes.")
    @is_registered()
    async def prize_info(self, ctx: commands.Context) -> None:
        await self._render_prize_info(ctx)

    async def _render_prize_info(self, ctx: commands.Context) -> None:
        prizes = await prizes_svc.all_visible()
        embeds = formatting.build_prize_info_embeds(prizes, CurrConfig.CTP_PRIZE_ARTWORK_URL)
        view = Paginator(embeds) if len(embeds) > 1 else None
        await ctx.reply(embed=embeds[0], view=view, allowed_mentions=discord.AllowedMentions.none())

    @prize_group.command(
        name="add",
        description="(Admin) Add a prize: <category> <enum> <cost> <duration_seconds> <display>",
    )
    @is_admin()
    async def prize_add(
        self,
        ctx: commands.Context,
        category: str,
        enum_name: str,
        cost: int,
        duration_seconds: int,
        *,
        display: str,
    ) -> None:
        if prizes_svc.normalize_category(category) is None:
            await ctx.reply(f"Unknown category `{category}`. Expected one of: {', '.join(prizes_svc.CATEGORIES)}.")
            return
        duration = None if duration_seconds == 0 else duration_seconds
        prize = await prizes_svc.create(
            category=category, enum_name=enum_name, cost=cost,
            duration_seconds=duration, display=display,
        )
        if prize is None:
            await ctx.reply(f"Prize `{category.upper()}.{enum_name.upper()}` already exists.")
            return
        await ctx.reply(
            f"Added `{prize.category}.{prize.enum_name}` (`{prize.cost}` points, "
            f"{formatting.format_duration(prize.duration_seconds)})."
        )

    @prize_group.command(name="edit", description="(Admin) Edit a prize: <category.enum> <field> <value>")
    @is_admin()
    async def prize_edit(self, ctx: commands.Context, category_enum: str, field: str, *, value: str) -> None:
        parsed = prizes_svc.parse_dotted_key(category_enum)
        if parsed is None:
            await ctx.reply(f"Could not parse `{category_enum}` — expected `CATEGORY.ENUM`.")
            return
        cat, enum_name = parsed
        prize = await prizes_svc.find(cat, enum_name)
        if prize is None:
            await ctx.reply(f"No prize `{cat}.{enum_name}`.")
            return
        ok, msg = await prizes_svc.edit_field(prize, field, value)
        await ctx.reply(msg)
        if not ok:
            logger.info("prize edit refused: %s.%s field=%s value=%s", cat, enum_name, field, value)

    @prize_group.command(name="disable", description="(Admin) Disable a prize (hides it from `~ctp prize info`).")
    @is_admin()
    async def prize_disable(self, ctx: commands.Context, *, category_enum: str) -> None:
        await self._prize_set_disabled(ctx, category_enum, True)

    @prize_group.command(name="enable", description="(Admin) Re-enable a previously disabled prize.")
    @is_admin()
    async def prize_enable(self, ctx: commands.Context, *, category_enum: str) -> None:
        await self._prize_set_disabled(ctx, category_enum, False)

    async def _prize_set_disabled(self, ctx: commands.Context, category_enum: str, value: bool) -> None:
        parsed = prizes_svc.parse_dotted_key(category_enum)
        if parsed is None:
            await ctx.reply(f"Could not parse `{category_enum}` — expected `CATEGORY.ENUM`.")
            return
        cat, enum_name = parsed
        prize = await prizes_svc.find(cat, enum_name)
        if prize is None:
            await ctx.reply(f"No prize `{cat}.{enum_name}`.")
            return
        await prizes_svc.set_disabled(prize, value)
        verb = "Disabled" if value else "Re-enabled"
        await ctx.reply(f"{verb} `{cat}.{enum_name}`.")

    @prize_group.command(name="remove", description="(Admin) Delete a prize from the catalog.")
    @is_admin()
    async def prize_remove(self, ctx: commands.Context, *, category_enum: str) -> None:
        parsed = prizes_svc.parse_dotted_key(category_enum)
        if parsed is None:
            await ctx.reply(f"Could not parse `{category_enum}` — expected `CATEGORY.ENUM`.")
            return
        cat, enum_name = parsed
        prize = await prizes_svc.find(cat, enum_name)
        if prize is None:
            await ctx.reply(f"No prize `{cat}.{enum_name}`.")
            return
        await prizes_svc.delete(prize)
        await ctx.reply(f"Deleted `{cat}.{enum_name}`. Existing ledger snapshots are preserved.")

    @prize_group.command(name="disclaim", description="(Admin) Attach a disclaimer to a prize.")
    @is_admin()
    async def prize_disclaim(self, ctx: commands.Context, category_enum: str, *, msg: str) -> None:
        parsed = prizes_svc.parse_dotted_key(category_enum)
        if parsed is None:
            await ctx.reply(f"Could not parse `{category_enum}` — expected `CATEGORY.ENUM`.")
            return
        cat, enum_name = parsed
        prize = await prizes_svc.find(cat, enum_name)
        if prize is None:
            await ctx.reply(f"No prize `{cat}.{enum_name}`.")
            return
        cleared = msg.strip().lower() in {"none", "clear", "off"}
        await prizes_svc.set_disclaimer(prize, None if cleared else msg)
        await ctx.reply(
            f"{'Cleared' if cleared else 'Updated'} disclaimer for `{cat}.{enum_name}`."
        )

    # --- Admin tier (boards + set) ------------------------------------

    @ctp_group.command(
        name="board",
        description="(Admin) Create/move/role-change/delete a board.",
    )
    @is_admin()
    async def ctp_board(
        self,
        ctx: commands.Context,
        enum: str,
        value: str,
        role_id: Optional[str] = None,
    ) -> None:
        # Two-arg form (``enum value``) handles move/role/delete; three-arg
        # form (``enum value role_id``) creates or replaces the board.
        # See ``boards.apply_board_command`` for the dispatch table.
        args = [value] if role_id is None else [value, role_id]
        result = await boards_svc.apply_board_command(enum, args)
        await ctx.reply(result.message, allowed_mentions=discord.AllowedMentions.none())
        logger.info(
            "ctp board: enum=%s args=%s -> %s by %s", enum, args, result.action, ctx.author,
        )

    @ctp_group.command(name="set", description="(Admin) Forcibly set a user's CTP balance.")
    @is_admin()
    async def ctp_set(
        self,
        ctx: commands.Context,
        user: CaseInsensitiveMember,
        value: int,
    ) -> None:
        target_member: discord.Member = user  # type: ignore[assignment]
        target = await balance_svc.get_or_create_disc(str(target_member.id))
        previous = await balance_svc.compute_balance(target)
        await balance_svc.set_balance(
            target=target, new_value=value, actor_disc_uuid=str(ctx.author.id),
        )
        await self._maybe_grant_lifetime_role(ctx.guild, target_member, target)
        await ctx.reply(
            f"Set {target_member.mention}'s CTP balance to **{value}** "
            f"(was {previous}, written as a corrective ledger row).",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(CTPCog(bot))
    logger.info("CTPCog loaded successfully")

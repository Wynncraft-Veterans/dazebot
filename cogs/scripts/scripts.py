"""Operator-only ``~script`` group: prefix-only one-off maintenance scripts.

These commands are intentionally kept out of the slash picker (no
``hybrid_*`` decorators) so they don't clutter operator UIs and don't
auto-discover into the global command tree. They're throwaway recovery /
backfill helpers — each subcommand is expected to be invoked rarely (often
once) and removed once its purpose is served.

Lives in its own cog (rather than parked under another domain) so that
adding or retiring a one-off doesn't touch any real feature code.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands

from bot import Bot
from lib.auth import is_operator

logger = logging.getLogger("dazebot.cogs.scripts.scripts")


# Module-level handle so a second ~script invocation can cancel the prior
# watcher instead of racing it — otherwise both would successfully spawn
# a "team 15" and we'd end up with two duplicate pool teams.
_r81_team14_watcher: Optional[asyncio.Task] = None


async def _r81_spawn_next_pool_team(
    bot: Bot,
    declined_ids: list[str],
    invoker_id: int,
) -> None:
    """Post a fresh pool invite for the declined-invitees of a filled team.

    Used by :func:`_r81_watch_and_spawn_next` when team 14 (or later)
    fills — the auto-declined losers get bundled into a new pool team.
    """
    from cogs.events.returns.week_81 import (
        INVITE_THREAD_ID,
        POOL_TEAM_SIZE,
        WEEK,
        BingoPoolInviteConfirmView,
        _POOL_SENTINEL_PREFIX,
        _current_team_for,
        _next_team_number,
        _resolve_thread,
    )
    from config import CurrConfig
    from orm_returns import BingoInvite, BingoTeam

    guild = bot.get_guild(CurrConfig.GUILD)
    if guild is None:
        logger.error(
            "r81 recovery: can't reach guild %s to resolve declined-"
            "invitees for next-team spawn",
            CurrConfig.GUILD,
        )
        return

    members: list[discord.Member] = []
    for did in declined_ids:
        try:
            uid = int(did)
        except ValueError:
            logger.warning("r81 recovery: non-numeric invitee id %r", did)
            continue
        m = guild.get_member(uid)
        if m is None:
            try:
                m = await guild.fetch_member(uid)
            except (discord.NotFound, discord.HTTPException):
                logger.warning(
                    "r81 recovery: can't fetch member %s for next-team spawn",
                    uid,
                )
                continue
        other = await _current_team_for(str(m.id))
        if other is not None and other.state != "disbanded":
            logger.warning(
                "r81 recovery: %s is already on team %s (state=%s), "
                "skipping from next-team spawn",
                m.id, other.team_number, other.state,
            )
            continue
        members.append(m)

    if not members:
        logger.error(
            "r81 recovery: no eligible members for next-team spawn; aborting"
        )
        return

    invite_thread = await _resolve_thread(bot, INVITE_THREAD_ID)
    if invite_thread is None:
        logger.error(
            "r81 recovery: can't reach invite thread %s for next-team spawn",
            INVITE_THREAD_ID,
        )
        return

    team_number = await _next_team_number()
    new_team = await BingoTeam.create(
        week=WEEK,
        team_number=team_number,
        creator_disc_uuid=f"{_POOL_SENTINEL_PREFIX}{invoker_id}",
        state="pending",
    )
    for m in members:
        await BingoInvite.create(
            team=new_team,
            invitee_disc_uuid=str(m.id),
            state="pending",
        )

    mentions_line = " ".join(m.mention for m in members)
    try:
        invite_msg = await invite_thread.send(
            content=(
                f"🎯 **r81 pool invite** — first {POOL_TEAM_SIZE} of you "
                f"to click **Accept** form Team {team_number}.\n"
                f"Invited: {mentions_line}"
            ),
            view=BingoPoolInviteConfirmView(),
            allowed_mentions=discord.AllowedMentions(
                users=list(members), roles=False, everyone=False
            ),
        )
    except discord.HTTPException:
        logger.exception(
            "r81 recovery: failed to post next-team pool invite"
        )
        new_team.state = "disbanded"
        await new_team.save(update_fields=["state"])
        return

    new_team.pending_invite_msg_id = invite_msg.id
    await new_team.save(update_fields=["pending_invite_msg_id"])
    logger.info(
        "r81 recovery: team %d pool invite posted (%d invitees, "
        "team.id=%s, msg=%s)",
        team_number, len(members), new_team.id, invite_msg.id,
    )


async def _r81_watch_and_spawn_next(
    bot: Bot, team_pk, invoker_id: int
) -> None:
    """Poll a pending BingoTeam until it advances, then spawn a follow-up
    pool team from its auto-declined losers.

    Ticks every 10 s, capped at 2 h. Cancellation-safe: raises through
    the ``asyncio.CancelledError`` when a subsequent ``~script`` cancels
    us in favour of a fresh watcher.
    """
    from orm_returns import BingoInvite, BingoTeam

    for _ in range(720):  # 720 * 10 s = 2 h
        await asyncio.sleep(10)
        t = await BingoTeam.get_or_none(id=team_pk)
        if t is None:
            logger.warning(
                "r81 recovery: watched team row vanished (id=%s); "
                "stopping watcher",
                team_pk,
            )
            return
        if t.state == "pending":
            continue
        if t.state == "disbanded":
            logger.warning(
                "r81 recovery: watched team %s was disbanded, not filled; "
                "not spawning follow-up",
                t.team_number,
            )
            return
        declined_ids: list[str] = list(
            await BingoInvite.filter(team=t, state="declined")
            .values_list("invitee_disc_uuid", flat=True)
        )
        if not declined_ids:
            logger.info(
                "r81 recovery: team %s filled with no losers — no "
                "follow-up needed",
                t.team_number,
            )
            return
        await _r81_spawn_next_pool_team(bot, declined_ids, invoker_id)
        return
    logger.warning(
        "r81 recovery: team %s fill watcher timed out after 2 h",
        team_pk,
    )


def _start_r81_team14_watcher(bot: Bot, team_pk, invoker_id: int) -> None:
    """Spawn (or replace) the singleton team-14 watcher task."""
    global _r81_team14_watcher
    if _r81_team14_watcher is not None and not _r81_team14_watcher.done():
        _r81_team14_watcher.cancel()
        logger.info(
            "r81 recovery: cancelled prior team-14 watcher before starting "
            "a new one"
        )
    _r81_team14_watcher = bot.loop.create_task(
        _r81_watch_and_spawn_next(bot, team_pk, invoker_id)
    )


class Scripts(commands.Cog):
    """``~script <subcommand>`` — operator-only one-off maintenance."""

    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("Scripts cog initialized")

    @commands.group(
        name="script",
        description="(Operator) One-off maintenance scripts.",
    )
    @is_operator()
    async def script_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply(
                "Available scripts: `~script edit_welcome <channel> <message_id>`, "
                "`~script edit_link_promo <channel>`, "
                "`~script rename_cult`, `~script install_intercult`, "
                "`~script install_recruitment`, `~script install_recruitment_deercult`, "
                "`~script extract_anni_timestamps`, "
                "`~script r81_recover_stuck_picker <team_number> [<line>]`."
            )

    @script_group.command(
        name="edit_welcome",
        description="(Operator) Rewrite an onboarding message to the simplified copy, keeping the link button.",
    )
    @is_operator()
    async def script_edit_welcome(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        message_id: str,
    ):
        from lib.discord_utils.first_install_view import FirstInstallView

        await ctx.defer(ephemeral=True)
        try:
            mid = int(message_id)
        except ValueError:
            await ctx.reply("`message_id` must be numeric.", ephemeral=True)
            return

        try:
            msg = await channel.fetch_message(mid)
        except (discord.NotFound, discord.Forbidden) as e:
            await ctx.reply(f"❌ Couldn't fetch that message: {e}", ephemeral=True)
            return

        if msg.author.id != self.bot.user.id:
            await ctx.reply(
                "❌ That message wasn't posted by me, so I can't edit it.",
                ephemeral=True,
            )
            return

        new_content = (
            "# Link your Wynn!\n"
            "## This will unlock some features and channels, especially "
            "if you are an in-game vets guild member!"
        )
        try:
            await msg.edit(content=new_content, embed=None, view=FirstInstallView())
        except discord.HTTPException as e:
            await ctx.reply(f"❌ Edit failed: {e}", ephemeral=True)
            return

        await ctx.reply(f"✅ Edited {msg.jump_url}.", ephemeral=True)

    @script_group.command(
        name="edit_link_promo",
        description=(
            "(Operator) Rewrite the vets-link promo post to the "
            "member/non-member Q&A copy, keeping the link button."
        ),
    )
    @is_operator()
    async def script_edit_link_promo(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
    ):
        """Update the pinned vets-link promo post to its new copy.

        Hard-codes the target message id (the promo is a single known
        post); the operator supplies the channel to avoid a bot-wide
        message scan.
        """
        from lib.discord_utils.first_install_view import FirstInstallView

        PROMO_MESSAGE_ID = 1500611826621091970

        await ctx.defer(ephemeral=True)
        try:
            msg = await channel.fetch_message(PROMO_MESSAGE_ID)
        except (discord.NotFound, discord.Forbidden) as e:
            await ctx.reply(f"❌ Couldn't fetch that message: {e}", ephemeral=True)
            return

        if msg.author.id != self.bot.user.id:
            await ctx.reply(
                "❌ That message wasn't posted by me, so I can't edit it.",
                ephemeral=True,
            )
            return

        new_content = (
            "# Are you an in-game VETS member?\n"
            "Use this to unlock guild-specific channels!\n"
            "\n"
            "# You're not?\n"
            "This thing will still give you a fancy year-based name colour!"
        )
        try:
            await msg.edit(content=new_content, embed=None, view=FirstInstallView())
        except discord.HTTPException as e:
            await ctx.reply(f"❌ Edit failed: {e}", ephemeral=True)
            return

        await ctx.reply(f"✅ Edited {msg.jump_url}.", ephemeral=True)

    @script_group.command(
        name="rename_cult",
        description="(Operator) One-off: rename the `dazecult` row to `deercult`.",
    )
    @is_operator()
    async def script_rename_cult(self, ctx: commands.Context):
        from orm import Cult

        await ctx.defer(ephemeral=True)
        old, new = "dazecult", "deercult"
        if await Cult.filter(name=new).exists():
            await ctx.reply(f"❌ `{new}` already exists; aborting.", ephemeral=True)
            return
        cult = await Cult.filter(name=old).first()
        if cult is None:
            await ctx.reply(f"❌ No cult named `{old}` to rename.", ephemeral=True)
            return
        cult.name = new
        await cult.save(update_fields=["name"])
        await ctx.reply(
            f"✅ Renamed `{old}` → `{new}`. (Thread mapping lives on the row "
            "itself in `Cult.thread_id`; no code change needed.)",
            ephemeral=True,
        )

    @script_group.command(
        name="install_intercult",
        description="(Operator) Reinstall the intercult button in every cult thread (idempotent).",
    )
    @is_operator()
    async def script_install_intercult(self, ctx: commands.Context):
        """Bulk reinstall of the cross-cult messaging button.

        Routine cult creation already installs the button via
        ``~manage_return 0 createCult``; this command is the one-off
        recovery path for re-pinning after the original message was
        deleted, or for bootstrapping a backfilled DB.
        """
        from cogs.events.returns.lib.views.intercult_view import install_intercult_button

        await ctx.defer(ephemeral=True)
        posted, skipped, failed, unresolved = await install_intercult_button(self.bot)
        await ctx.reply(
            f"✅ intercult install: posted={posted} skipped={skipped} "
            f"failed={failed} unresolved={unresolved}",
            ephemeral=True,
        )

    @script_group.command(
        name="install_recruitment",
        description="(Operator) Install the recruitment button in every cult thread except deercult (idempotent).",
    )
    @is_operator()
    async def script_install_recruitment(self, ctx: commands.Context):
        """Bulk install of the recruitment button across non-deercult cults.

        Deercult is excluded by default (membership-balance reasons);
        opt it in separately with ``~script install_recruitment_deercult``.
        Routine cult creation installs this button automatically (also
        skipping deercult); this command exists for the initial bootstrap
        and for re-pinning if the message was deleted.
        """
        from cogs.events.returns.lib.views.recruitment_view import install_recruitment_button

        await ctx.defer(ephemeral=True)
        posted, skipped, failed, unresolved, excluded = await install_recruitment_button(self.bot)
        await ctx.reply(
            f"✅ recruitment install: posted={posted} skipped={skipped} "
            f"failed={failed} unresolved={unresolved} excluded_deercult={excluded}",
            ephemeral=True,
        )

    @script_group.command(
        name="install_recruitment_deercult",
        description="(Operator) One-shot: install the recruitment button in deercult's thread.",
    )
    @is_operator()
    async def script_install_recruitment_deercult(self, ctx: commands.Context):
        """Install the recruitment button in deercult, normally excluded.

        Call once membership rebalances make it appropriate. Idempotent —
        safe to run twice (the second run reports ``skipped``).
        """
        from cogs.events.returns.lib.views.recruitment_view import install_recruitment_in_deercult

        await ctx.defer(ephemeral=True)
        result = await install_recruitment_in_deercult(self.bot)
        note = {
            "posted": "✅ Recruitment button installed and pinned in deercult.",
            "skipped": "ℹ️ Recruitment button was already pinned in deercult.",
            "failed": "❌ Recruitment button install failed; check logs.",
            "unresolved": "❌ deercult has no resolvable `thread_id`; aborting.",
        }[result]
        await ctx.reply(note, ephemeral=True)

    # Stamp / anni-announcement channel — magbot posts the
    # @Prelude to Annihilation alerts here. Hardcoded because the
    # AnniConfig wrapper was removed when the role-ping cog moved to
    # fishbot; this operator dump script is the last consumer.
    _ANNI_DUMP_CHANNEL_ID = 1339393368672702567

    @script_group.command(
        name="extract_anni_timestamps",
        description="(Operator) Dump 3 years of anni-channel messages as gzipped JSONL for offline debug.",
    )
    @is_operator()
    async def script_extract_anni_timestamps(self, ctx: commands.Context):
        """Dump raw history of the anni-announcement channel to JSONL.gz.

        Iterates ``channel.history(limit=None, after=utcnow-3y,
        oldest_first=True)`` and writes one JSON object per message with
        the metadata needed to debug filter decisions (``webhook_id``,
        ``interaction_metadata``, ``flags``, ``author.bot``, embeds, …).

        Replaces the prior CSV timestamp-extraction flow, which had
        become brittle against multiple Magbot/announcement-follow embed
        shapes; the new output is meant for offline analysis (jq /
        sqlite) before any filter is re-derived.

        Output streams into rotating gzip parts capped at the guild's
        ``filesize_limit`` (minus a safety margin), so a single dump may
        arrive as several ``anni_messages.partNN.jsonl.gz`` attachments.
        """
        import datetime
        import gzip
        import io
        import json
        import time

        HISTORY_WINDOW = datetime.timedelta(days=3 * 365)
        EDIT_INTERVAL_S = 5.0
        CHUNK_SAFETY_MARGIN = 1 * 1024 * 1024  # 1 MiB headroom under filesize_limit

        if ctx.guild is None:
            await ctx.reply("❌ Must be run in a guild context.")
            return

        channel = ctx.guild.get_channel(self._ANNI_DUMP_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            await ctx.reply(
                f"❌ Anni dump channel ({self._ANNI_DUMP_CHANNEL_ID}) "
                "is not a readable text channel in this guild."
            )
            return

        after = discord.utils.utcnow() - HISTORY_WINDOW
        filesize_limit = ctx.guild.filesize_limit
        chunk_target = max(filesize_limit - CHUNK_SAFETY_MARGIN, 1 * 1024 * 1024)

        def embed_dict(emb: discord.Embed) -> dict:
            return {
                "title": emb.title,
                "description": emb.description,
                "author_name": emb.author.name if emb.author else None,
                "footer_text": emb.footer.text if emb.footer else None,
                "fields": [{"name": f.name, "value": f.value} for f in emb.fields],
            }

        def msg_dict(msg: discord.Message) -> dict:
            im = msg.interaction_metadata
            return {
                "id": msg.id,
                "created_at": msg.created_at.isoformat(),
                "type": msg.type.name if msg.type else None,
                "content": msg.content,
                "clean_content": msg.clean_content,
                "author": {
                    "id": msg.author.id,
                    "name": msg.author.name,
                    "bot": msg.author.bot,
                    "system": getattr(msg.author, "system", False),
                },
                "webhook_id": msg.webhook_id,
                "application_id": msg.application_id,
                "interaction_metadata": (
                    {
                        "id": im.id,
                        "type": im.type.name if im.type else None,
                        "user_id": im.user.id if im.user else None,
                    }
                    if im is not None
                    else None
                ),
                "flags": {
                    "value": msg.flags.value,
                    "crossposted": msg.flags.crossposted,
                    "is_crossposted": msg.flags.is_crossposted,
                    "suppress_embeds": msg.flags.suppress_embeds,
                    "ephemeral": msg.flags.ephemeral,
                    "loading": msg.flags.loading,
                },
                "role_mention_names": [r.name for r in msg.role_mentions],
                "embeds": [embed_dict(e) for e in msg.embeds],
                "attachment_count": len(msg.attachments),
                "reaction_count": sum(r.count for r in msg.reactions),
            }

        status = await ctx.reply(
            f"Starting dump of #{channel.name} since {after.date().isoformat()} "
            "(no message cap)..."
        )
        started = time.monotonic()
        last_edit = started
        messages_scanned = 0
        parts: list[bytes] = []
        buf = io.BytesIO()
        gz = gzip.GzipFile(fileobj=buf, mode="wb")

        def rotate_part() -> None:
            nonlocal buf, gz
            gz.close()
            parts.append(buf.getvalue())
            buf = io.BytesIO()
            gz = gzip.GzipFile(fileobj=buf, mode="wb")

        async def push_status():
            nonlocal last_edit
            last_edit = time.monotonic()
            elapsed = last_edit - started
            text = (
                f"Dumping #{channel.name} since {after.date().isoformat()}...\n"
                f"messages scanned: {messages_scanned:,}\n"
                f"parts so far: {len(parts)}\n"
                f"elapsed: {elapsed:.0f}s"
            )
            try:
                await status.edit(content=text)
            except discord.HTTPException:
                pass
            logger.info(
                "extract_anni_timestamps progress: %d msgs scanned, %d parts, %.0fs elapsed",
                messages_scanned, len(parts), elapsed,
            )

        try:
            async for msg in channel.history(
                limit=None, after=after, oldest_first=True
            ):
                messages_scanned += 1
                gz.write(
                    (
                        json.dumps(msg_dict(msg), ensure_ascii=False, default=str)
                        + "\n"
                    ).encode("utf-8")
                )
                if buf.tell() >= chunk_target:
                    rotate_part()
                if time.monotonic() - last_edit >= EDIT_INTERVAL_S:
                    await push_status()
        except (discord.Forbidden, discord.HTTPException) as e:
            try:
                await status.edit(content=f"❌ History read failed: {e}")
            except discord.HTTPException:
                pass
            return

        gz.close()
        if buf.tell() > 0 or not parts:
            parts.append(buf.getvalue())

        elapsed = time.monotonic() - started
        total_bytes = sum(len(p) for p in parts)
        try:
            await status.edit(
                content=(
                    f"Dump complete in {elapsed:.0f}s — "
                    f"{messages_scanned:,} messages, {len(parts)} part"
                    f"{'s' if len(parts) > 1 else ''}, "
                    f"{total_bytes / 1_048_576:.1f} MiB gzipped."
                )
            )
        except discord.HTTPException:
            pass

        def part_file(idx: int, data: bytes) -> discord.File:
            name = (
                f"anni_messages.part{idx:02d}.jsonl.gz"
                if len(parts) > 1
                else "anni_messages.jsonl.gz"
            )
            return discord.File(io.BytesIO(data), filename=name)

        files = [part_file(i + 1, p) for i, p in enumerate(parts)]
        summary = (
            f"Done. {messages_scanned:,} messages from #{channel.name} "
            f"since {after.date().isoformat()} "
            f"({len(parts)} part{'s' if len(parts) > 1 else ''}, "
            f"{total_bytes / 1_048_576:.1f} MiB gzipped)."
        )

        BATCH = 10
        for i in range(0, len(files), BATCH):
            batch = files[i : i + BATCH]
            if i == 0:
                await ctx.reply(content=summary, files=batch)
            else:
                await ctx.send(files=batch)

    @script_group.command(
        name="r81_recover_team_10_into_pool_14",
        description=(
            "(Operator) One-off: silently disband r81 team 10, funnel "
            "Psycho/Jelles/Aviko into team 14's still-pending pool, then "
            "spawn a team 15 pool invite from the three who don't make it."
        ),
    )
    @is_operator()
    async def script_r81_recover_team_10_into_pool_14(
        self, ctx: commands.Context
    ):
        """Rebuild after two overlapping r81 team formations.

        Baseline (see conversation on 2026-07-14): team 10 is a 3-person
        team (Psycho, Jelles, Aviko) formed via the original /return 81
        two-invite flow; team 14 is a still-pending pool invite where
        only Box has clicked Accept out of {Box, Ely, Gatze}. The goal is
        to consolidate the 6 unattached players (Box + Ely + Gatze +
        Psycho + Jelles + Aviko) into two 3-person teams by:

        1. Disbanding team 10 in the DB only — no thread removal, no
           archive, no chat notification.
        2. Adding Psycho/Jelles/Aviko as pending BingoInvite rows on
           team 14. They can now click Accept on the existing invite
           post.
        3. Confirming Box's acceptance is still recorded (he's already
           team 14's creator per the pool-invite promotion flow — this is
           a sanity check, no DB touch needed).
        4. Watching team 14 in the background until it advances out of
           ``pending`` (i.e. 3 people accepted), then posting a new pool
           invite for team 15 with the 3 losers so they can form the
           second team.
        """
        from cogs.events.returns.week_81 import WEEK
        from orm_returns import BingoInvite, BingoTeam

        PSYCHO_ID = 333977111515889674
        JELLES_ID = 269151037946986500
        AVIKO_ID = 560930769049092119
        BOX_ID = 262800022758752257
        NEW_INVITEES = (PSYCHO_ID, JELLES_ID, AVIKO_ID)

        # Interaction path (slash) needs a defer; prefix path is fine
        # replying directly. Keep response ephemeral — the whole point
        # of the script is that this is a silent recovery.
        try:
            await ctx.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        team10 = await BingoTeam.filter(week=WEEK, team_number=10).first()
        if team10 is None:
            await ctx.reply("❌ Team 10 not found.", ephemeral=True)
            return
        team14 = await BingoTeam.filter(week=WEEK, team_number=14).first()
        if team14 is None:
            await ctx.reply("❌ Team 14 not found.", ephemeral=True)
            return
        if team14.state != "pending":
            await ctx.reply(
                f"❌ Team 14 is in state `{team14.state}`, not `pending`. "
                "Nothing to do; aborting so the script isn't destructive.",
                ephemeral=True,
            )
            return
        if team14.creator_disc_uuid != str(BOX_ID):
            # If Box isn't the creator, either he never clicked Accept
            # (his sentinel is still there) or someone else beat him to
            # it. In neither case do we want to silently overwrite.
            await ctx.reply(
                f"❌ Team 14 creator is `{team14.creator_disc_uuid}`, "
                f"expected Box (`{BOX_ID}`). Aborting; confirm state "
                "before re-running.",
                ephemeral=True,
            )
            return

        # Step 1: silent DB-only disband of team 10.
        if team10.state != "disbanded":
            team10.state = "disbanded"
            await team10.save(update_fields=["state"])
        # Deliberately do NOT touch team10.thread — no remove_user, no
        # archive. The thread stays visible; only the DB knows it's dead.
        # `_current_team_for` filters on state != "disbanded", so
        # Psycho/Jelles/Aviko are now free to join team 14.

        # Step 2: add invites to team 14 (idempotent — the BingoInvite
        # unique_together on (team, invitee_disc_uuid) would otherwise
        # raise). If a row already exists in a non-pending state, revert
        # it to "pending" so the invitee can click Accept.
        added_new = 0
        rewound = 0
        for uid in NEW_INVITEES:
            existing = await BingoInvite.filter(
                team=team14, invitee_disc_uuid=str(uid)
            ).first()
            if existing is None:
                await BingoInvite.create(
                    team=team14,
                    invitee_disc_uuid=str(uid),
                    state="pending",
                )
                added_new += 1
            elif existing.state != "pending":
                existing.state = "pending"
                await existing.save(update_fields=["state"])
                rewound += 1

        # Step 3 is inherently a no-op — Box is already the creator per
        # the check above, and the pool-invite promotion flow deletes
        # the creator's BingoInvite row (so his acceptance is stored in
        # team.creator_disc_uuid, not in a BingoInvite row that needs
        # touching here).

        await ctx.reply(
            f"✅ Team 10 disbanded (silent). Team 14 invitees: "
            f"{added_new} new, {rewound} rewound to pending. "
            f"Box remains creator ({BOX_ID}). Watching team 14 for "
            f"fill; when it advances I'll post the team 15 pool invite.",
            ephemeral=True,
        )
        logger.info(
            "r81 recovery: team 10 disbanded; team 14 got %d new + %d "
            "rewound invites for %s",
            added_new, rewound, NEW_INVITEES,
        )

        _start_r81_team14_watcher(self.bot, team14.id, ctx.author.id)

    @script_group.command(
        name="r81_refresh_team_14_pool_invite",
        description=(
            "(Operator) One-off: delete team 14's stale pool-invite "
            "message and post a fresh one that names every current "
            "pending invitee, then restart the fill-watcher."
        ),
    )
    @is_operator()
    async def script_r81_refresh_team_14_pool_invite(
        self, ctx: commands.Context
    ):
        """Replace team 14's pool-invite post.

        After ``r81_recover_team_10_into_pool_14`` silently added
        Psycho/Jelles/Aviko as BingoInvite rows on team 14, the original
        pool post still reads "Invited: gatze / Elyux / Box" — so the
        newly-added invitees have no visible cue to click Accept. This
        script deletes the old post, generates a fresh one from the
        live DB (mentioning every current pending invitee with a ping,
        naming already-accepted members without pinging them), rewires
        ``team.pending_invite_msg_id`` to the new post so
        ``_handle_pool_invite_click`` still resolves, and restarts the
        fill-watcher (cancelling any prior one).
        """
        from cogs.events.returns.week_81 import (
            INVITE_THREAD_ID,
            POOL_TEAM_SIZE,
            WEEK,
            BingoPoolInviteConfirmView,
            _POOL_SENTINEL_PREFIX,
            _resolve_thread,
        )
        from orm_returns import BingoInvite, BingoTeam

        try:
            await ctx.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        team14 = await BingoTeam.filter(week=WEEK, team_number=14).first()
        if team14 is None:
            await ctx.reply("❌ Team 14 not found.", ephemeral=True)
            return
        if team14.state != "pending":
            await ctx.reply(
                f"❌ Team 14 is in state `{team14.state}`, not `pending`. "
                "Refresh only makes sense on a still-open pool.",
                ephemeral=True,
            )
            return

        invite_thread = await _resolve_thread(self.bot, INVITE_THREAD_ID)
        if invite_thread is None:
            await ctx.reply(
                f"❌ Can't reach invite thread ({INVITE_THREAD_ID}).",
                ephemeral=True,
            )
            return

        # Delete the stale post. Best-effort: if it's already gone or the
        # bot lacks perms, log and continue — the new post is what matters.
        if team14.pending_invite_msg_id:
            try:
                old_msg = await invite_thread.fetch_message(
                    team14.pending_invite_msg_id
                )
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                logger.info(
                    "r81 refresh: prior team 14 message %s already gone/"
                    "inaccessible; continuing",
                    team14.pending_invite_msg_id,
                )
            except discord.HTTPException:
                logger.exception(
                    "r81 refresh: unexpected error deleting old team 14 "
                    "message %s; continuing",
                    team14.pending_invite_msg_id,
                )

        # Rebuild the roster from the DB. Creator is always "accepted"
        # (his BingoInvite row was consumed at promotion); accepted invites
        # were the extra clickers before us; pending invites are who we
        # still need.
        from config import CurrConfig

        guild_id = ctx.guild.id if ctx.guild is not None else CurrConfig.GUILD
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await ctx.reply(
                f"❌ Couldn't reach guild {guild_id}.", ephemeral=True
            )
            return

        def _resolve(disc_uuid: str) -> Optional[discord.Member]:
            try:
                return guild.get_member(int(disc_uuid))
            except (ValueError, TypeError):
                return None

        accepted_members: list[discord.Member] = []
        # Creator, if it's a real user (skip the "pool:" sentinel — should
        # never appear on a mid-flight team but guard anyway).
        if not team14.creator_disc_uuid.startswith(_POOL_SENTINEL_PREFIX):
            creator = _resolve(team14.creator_disc_uuid)
            if creator is not None:
                accepted_members.append(creator)

        accepted_invites = await BingoInvite.filter(
            team=team14, state="accepted"
        )
        for inv in accepted_invites:
            m = _resolve(inv.invitee_disc_uuid)
            if m is not None:
                accepted_members.append(m)

        pending_invites = await BingoInvite.filter(
            team=team14, state="pending"
        )
        pending_members: list[discord.Member] = []
        for inv in pending_invites:
            m = _resolve(inv.invitee_disc_uuid)
            if m is not None:
                pending_members.append(m)

        if not pending_members:
            await ctx.reply(
                "❌ Team 14 has no pending invitees — nothing left to prompt. "
                "Not posting a new message.",
                ephemeral=True,
            )
            return

        seats_left = POOL_TEAM_SIZE - len(accepted_members)
        seats_left = max(seats_left, 0)

        accepted_line = (
            ", ".join(m.mention for m in accepted_members)
            if accepted_members
            else "(nobody yet)"
        )
        pending_line = " ".join(m.mention for m in pending_members)

        seats_phrase = (
            "1 more of you needs"
            if seats_left == 1
            else f"{seats_left} more of you need"
        )
        content = (
            f"🎯 **r81 pool invite (refreshed)** — {seats_phrase} to click "
            f"**Accept** to fill Team {team14.team_number}.\n"
            f"Already accepted: {accepted_line}.\n"
            f"Still needed (any {seats_left} of): {pending_line}"
        )

        # Ping only the still-pending invitees. Already-accepted members
        # shouldn't get re-pinged just because we regenerated the post.
        try:
            new_msg = await invite_thread.send(
                content=content,
                view=BingoPoolInviteConfirmView(),
                allowed_mentions=discord.AllowedMentions(
                    users=list(pending_members), roles=False, everyone=False
                ),
            )
        except discord.HTTPException as e:
            await ctx.reply(
                f"❌ Failed to post refreshed invite: {e}. Leaving DB "
                "unchanged so the original post (if still there) still "
                "dispatches.",
                ephemeral=True,
            )
            return

        prior_msg_id = team14.pending_invite_msg_id
        team14.pending_invite_msg_id = new_msg.id
        await team14.save(update_fields=["pending_invite_msg_id"])

        await ctx.reply(
            f"✅ Team 14 refreshed. New post: {new_msg.jump_url} "
            f"(accepted={len(accepted_members)}, pending="
            f"{len(pending_members)}, seats left={seats_left}). "
            "Watcher (re)started.",
            ephemeral=True,
        )
        logger.info(
            "r81 refresh: team 14 pending_invite_msg_id %s -> %s; "
            "accepted=%d pending=%d",
            prior_msg_id, new_msg.id,
            len(accepted_members), len(pending_members),
        )

        _start_r81_team14_watcher(self.bot, team14.id, ctx.author.id)

    @script_group.command(
        name="r81_repair_team_14_and_spawn_15",
        description=(
            "(Operator) One-off: purge the three declined pool-invitees "
            "who were wrongly added to team 14 by the pre-fix "
            "_advance_to_picking, then spawn team 15 for them."
        ),
    )
    @is_operator()
    async def script_r81_repair_team_14_and_spawn_15(
        self, ctx: commands.Context
    ):
        """Recover from the ``_advance_to_picking`` iterate-all-invites bug.

        The pre-fix ``_advance_to_picking`` at week_81.py fetched
        ``BingoInvite.filter(team=team)`` — every invite regardless of
        state — and added each invitee to the team's private thread AND
        wrote a ``BingoTeamMember`` row for them. That was harmless in
        the original two-invitee flow (which only reaches advance when
        all invites are accepted) but wrong for pool teams, where the
        declined losers ended up as ghost members. ``_current_team_for``
        then reported those losers as "already on team 14 (picking)",
        so the team-15 watcher aborted with no eligible members.

        This script identifies the team-14 members whose ``BingoInvite``
        row is ``state="declined"`` (they were auto-declined before
        advance and shouldn't be members), removes them from team 14's
        thread, deletes their ``BingoTeamMember`` rows, and hands them
        off to :func:`_r81_spawn_next_pool_team` to form team 15.
        """
        from cogs.events.returns.week_81 import (
            WEEK,
            _resolve_thread,
        )
        from orm_returns import BingoInvite, BingoTeam, BingoTeamMember

        try:
            await ctx.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        team14 = await BingoTeam.filter(week=WEEK, team_number=14).first()
        if team14 is None:
            await ctx.reply("❌ Team 14 not found.", ephemeral=True)
            return
        if team14.state not in ("picking", "playing"):
            await ctx.reply(
                f"❌ Team 14 is in state `{team14.state}` — expected "
                "`picking` or `playing`. Nothing to repair.",
                ephemeral=True,
            )
            return

        # Ghost members: BingoTeamMember rows on team 14 whose matching
        # BingoInvite row is declined. (Real accepted members have their
        # BingoInvite row still marked "accepted", or in the creator's
        # case the invite row was deleted at promotion time.)
        declined_uuids: list[str] = list(
            await BingoInvite.filter(team=team14, state="declined")
            .values_list("invitee_disc_uuid", flat=True)
        )
        if not declined_uuids:
            await ctx.reply(
                "❌ No declined invites on team 14 — nothing to purge. "
                "Aborting.",
                ephemeral=True,
            )
            return

        thread = await _resolve_thread(self.bot, team14.thread_id)
        removed_from_thread = 0
        removed_from_db = 0
        for uuid in declined_uuids:
            # DB row cleanup.
            deleted = await BingoTeamMember.filter(
                team=team14, disc_uuid=uuid
            ).delete()
            if deleted:
                removed_from_db += 1
            # Thread membership cleanup (best-effort).
            if thread is not None:
                try:
                    await thread.remove_user(discord.Object(id=int(uuid)))
                    removed_from_thread += 1
                except ValueError:
                    logger.warning(
                        "r81 repair: non-numeric disc_uuid %r", uuid
                    )
                except (discord.NotFound, discord.HTTPException) as e:
                    logger.warning(
                        "r81 repair: remove_user(%s) on thread %s: %s",
                        uuid, team14.thread_id, e,
                    )

        # Now spawn team 15 with the same three via the shared helper.
        # It re-checks _current_team_for per member and re-does eligibility;
        # with the ghost BingoTeamMember rows gone, they'll pass.
        await _r81_spawn_next_pool_team(
            self.bot, declined_uuids, ctx.author.id
        )

        await ctx.reply(
            f"✅ Team 14 repair: removed {removed_from_db} "
            f"BingoTeamMember row(s), {removed_from_thread} thread "
            f"member(s). Attempted team 15 spawn for "
            f"{len(declined_uuids)} invitee(s); check the invite "
            "thread and the bot logs for the outcome.",
            ephemeral=True,
        )
        logger.info(
            "r81 repair: team 14 purged %d ghosts (%s); team 15 spawn "
            "attempted",
            len(declined_uuids), declined_uuids,
        )


    @script_group.command(
        name="r81_recover_stuck_picker",
        description=(
            "(Operator) One-off: clear a team's stuck picker_msg_id and, "
            "if a bingo line is given, post a fresh extra-teammate picker "
            "for it (bypasses _announce_bingo's idempotency guard)."
        ),
    )
    @is_operator()
    async def script_r81_recover_stuck_picker(
        self,
        ctx: commands.Context,
        team_number: int,
        line: Optional[str] = None,
    ):
        """Recover from a mid-flight ``_handle_picker_click`` crash.

        Symptom: an earlier picker click added the teammate to the thread
        and created the ``BingoTeamMember`` row, but the bot died before
        the final ``team.picker_msg_id = None`` save at
        ``_handle_picker_click:1443-1445``. Subsequent bingos on that team
        then trip the ``picker_msg_id is not None`` guard in
        ``_announce_bingo:1872`` and post "picker already active" instead
        of a fresh extra-teammate picker. Because ``_announce_bingo`` has
        already written the ``BingoBingoEvent`` row for the skipped
        bingo (line 1864, before the guard), it is now idempotent-locked
        and can't be re-invoked — recovery has to inline the picker-post
        block itself.

        Args:
            team_number: The r81 team whose picker state is stuck.
            line: Optional cells like ``A6-B6-C6-D6`` (also accepts commas
                or spaces). If supplied, a recovery picker is posted for
                that line; if omitted, only the stale DB state is cleared.
        """
        import json

        from cogs.events.returns.week_81 import (
            EXTRA_MEMBER_PICKER_SIZE,
            POINTS_PER_BINGO_LINE,
            WEEK,
            _line_key,
            _picker_intro,
            _render_picker_view,
            _resolve_thread,
            _team_exp,
            _wildcard_candidates_excluding_team,
            bingo_cells,
        )
        from orm_returns import BingoBingoEvent, BingoTeam

        try:
            await ctx.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        team = await BingoTeam.filter(
            week=WEEK, team_number=team_number
        ).first()
        if team is None:
            await ctx.reply(
                f"❌ Team {team_number} (week {WEEK}) not found.",
                ephemeral=True,
            )
            return
        if team.state != "playing":
            await ctx.reply(
                f"❌ Team {team_number} is in state `{team.state}`, "
                "not `playing`. A stuck-picker recovery only applies to "
                "live teams; use the wildcard-specific reroll path if the "
                "team is still `picking`.",
                ephemeral=True,
            )
            return

        thread = await _resolve_thread(self.bot, team.thread_id)

        prior_picker_msg_id = team.picker_msg_id
        deleted_stale = False
        if prior_picker_msg_id is not None and thread is not None:
            try:
                stale = await thread.fetch_message(prior_picker_msg_id)
                await stale.delete()
                deleted_stale = True
            except (discord.NotFound, discord.Forbidden):
                pass
            except discord.HTTPException:
                logger.exception(
                    "r81 recover: unexpected error deleting stale picker "
                    "message %s in thread %s; continuing",
                    prior_picker_msg_id, team.thread_id,
                )

        cleared_db = False
        if team.picker_msg_id is not None or team.picker_candidates_json is not None:
            team.picker_msg_id = None
            team.picker_candidates_json = None
            await team.save(
                update_fields=["picker_msg_id", "picker_candidates_json"]
            )
            cleared_db = True

        if line is None:
            await ctx.reply(
                f"✅ Team {team_number} picker state cleared "
                f"(deleted_stale_msg={deleted_stale}, cleared_db={cleared_db}, "
                f"prior_msg_id={prior_picker_msg_id}). "
                "No line given, so no recovery picker posted.",
                ephemeral=True,
            )
            logger.info(
                "r81 recover: team %s picker cleared "
                "(prior_msg_id=%s, deleted_stale=%s, cleared_db=%s); "
                "no recovery picker requested",
                team_number, prior_picker_msg_id, deleted_stale, cleared_db,
            )
            return

        cells = tuple(
            c.strip().upper()
            for c in line.replace(",", "-").replace(" ", "-").split("-")
            if c.strip()
        )
        valid = set(bingo_cells(_team_exp(team)))
        bad = [c for c in cells if c not in valid]
        if bad:
            await ctx.reply(
                f"❌ Line `{line}` contains invalid cells for the current "
                f"board: {bad}. Valid cells: {sorted(valid)}.",
                ephemeral=True,
            )
            return

        line_key = _line_key(cells)
        recorded = await BingoBingoEvent.filter(
            team=team, line_key=line_key
        ).first()
        if recorded is None:
            existing_keys = list(
                await BingoBingoEvent.filter(team=team)
                .values_list("line_key", flat=True)
            )
            await ctx.reply(
                f"❌ No `BingoBingoEvent` row on team {team_number} for "
                f"`{line_key}`. Recorded lines: {existing_keys}. "
                "Refusing to mint a picker for a bingo that was never "
                "recorded.",
                ephemeral=True,
            )
            return

        if thread is None:
            await ctx.reply(
                f"❌ Team {team_number}'s thread ({team.thread_id}) is not "
                "resolvable — can't post a recovery picker.",
                ephemeral=True,
            )
            return

        header = (
            f"🎉 **Bingo!** Line: `{' → '.join(cells)}` "
            f"— `+{POINTS_PER_BINGO_LINE}` to each teammate. "
            "(recovery re-post)"
        )

        candidates = await _wildcard_candidates_excluding_team(self.bot, team)
        candidates = candidates[:EXTRA_MEMBER_PICKER_SIZE]
        if not candidates:
            await thread.send(
                f"{header}\n"
                "No eligible extra teammates are available right now — "
                "team keeps the points but doesn't gain a member for this "
                "bingo."
            )
            await ctx.reply(
                f"✅ Team {team_number} picker state cleared "
                f"(deleted_stale_msg={deleted_stale}, cleared_db={cleared_db}). "
                "Recovery header posted but no candidates were available, "
                "so no picker view was attached.",
                ephemeral=True,
            )
            return

        picker_msg = await thread.send(
            content=f"{header}\n\n" + _picker_intro(candidates),
            view=_render_picker_view(candidates),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        team.picker_msg_id = picker_msg.id
        team.picker_candidates_json = json.dumps(
            [str(member.id) for member, _mc in candidates]
        )
        await team.save(
            update_fields=["picker_msg_id", "picker_candidates_json"]
        )

        await ctx.reply(
            f"✅ Team {team_number} recovered: "
            f"deleted_stale_msg={deleted_stale}, cleared_db={cleared_db}, "
            f"new picker msg={picker_msg.id} ({len(candidates)} candidates) "
            f"for line `{line_key}`.",
            ephemeral=True,
        )
        logger.info(
            "r81 recover: team %s picker re-posted for line %s "
            "(prior_msg_id=%s, new_msg_id=%s, candidates=%d)",
            team_number, line_key, prior_picker_msg_id, picker_msg.id,
            len(candidates),
        )


async def setup(bot: Bot):
    await bot.add_cog(Scripts(bot))
    logger.info("Scripts cog loaded successfully")

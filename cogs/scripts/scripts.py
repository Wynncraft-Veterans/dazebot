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

import logging

import discord
from discord.ext import commands

from bot import Bot
from lib.auth import is_operator

logger = logging.getLogger("dazebot.cogs.scripts.scripts")


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
                "`~script rename_cult`, `~script install_intercult`, "
                "`~script install_recruitment`, `~script install_recruitment_deercult`, "
                "`~script extract_anni_timestamps`."
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
        import asyncio

        from cogs.events.returns.week_81 import (
            WEEK,
            INVITE_THREAD_ID,
            POOL_TEAM_SIZE,
            BingoPoolInviteConfirmView,
            _POOL_SENTINEL_PREFIX,
            _current_team_for,
            _next_team_number,
            _resolve_thread,
        )
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

        # Step 4: background watcher. Polls team 14 every 10s; on the
        # first tick where state != "pending" we form team 15 from the
        # invites that got auto-declined by _handle_pool_invite_click.
        # Bounded at 2h so a stuck team doesn't leak a task forever.
        team14_pk = team14.id
        invoker_id = ctx.author.id

        async def _watcher():
            for _ in range(720):  # 720 * 10s = 2h
                await asyncio.sleep(10)
                t = await BingoTeam.get_or_none(id=team14_pk)
                if t is None:
                    logger.warning(
                        "r81 recovery: team 14 row vanished; stopping watcher"
                    )
                    return
                if t.state == "pending":
                    continue
                if t.state == "disbanded":
                    logger.warning(
                        "r81 recovery: team 14 was disbanded, not filled; "
                        "not spawning team 15"
                    )
                    return
                # Team 14 filled (state is picking/playing). Losers are
                # the auto-declined invites from
                # _handle_pool_invite_click's cleanup loop.
                declined_ids: list[str] = (
                    await BingoInvite.filter(team=t, state="declined")
                    .values_list("invitee_disc_uuid", flat=True)
                )
                if not declined_ids:
                    logger.info(
                        "r81 recovery: team 14 filled with no losers "
                        "(unexpected — no team 15 needed)"
                    )
                    return
                await _spawn_team_15(declined_ids)
                return
            logger.warning(
                "r81 recovery: team 14 fill watcher timed out after 2h "
                "without team 14 advancing"
            )

        async def _spawn_team_15(declined_ids: list[str]) -> None:
            from config import CurrConfig

            guild = self.bot.get_guild(CurrConfig.GUILD)
            if guild is None:
                logger.error(
                    "r81 recovery: can't reach guild %s to resolve "
                    "declined-invitees for team 15",
                    CurrConfig.GUILD,
                )
                return

            members: list[discord.Member] = []
            for did in declined_ids:
                try:
                    uid = int(did)
                except ValueError:
                    logger.warning(
                        "r81 recovery: non-numeric invitee id %r", did
                    )
                    continue
                m = guild.get_member(uid)
                if m is None:
                    try:
                        m = await guild.fetch_member(uid)
                    except (discord.NotFound, discord.HTTPException):
                        logger.warning(
                            "r81 recovery: can't fetch member %s for "
                            "team 15", uid,
                        )
                        continue
                other = await _current_team_for(str(m.id))
                if other is not None and other.state != "disbanded":
                    logger.warning(
                        "r81 recovery: %s is already on team %s "
                        "(state=%s), skipping from team 15",
                        m.id, other.team_number, other.state,
                    )
                    continue
                members.append(m)

            if not members:
                logger.error(
                    "r81 recovery: no eligible members for team 15; "
                    "aborting spawn"
                )
                return

            invite_thread = await _resolve_thread(
                self.bot, INVITE_THREAD_ID
            )
            if invite_thread is None:
                logger.error(
                    "r81 recovery: can't reach invite thread %s for "
                    "team 15 spawn",
                    INVITE_THREAD_ID,
                )
                return

            team_number = await _next_team_number()
            team15 = await BingoTeam.create(
                week=WEEK,
                team_number=team_number,
                creator_disc_uuid=f"{_POOL_SENTINEL_PREFIX}{invoker_id}",
                state="pending",
            )
            for m in members:
                await BingoInvite.create(
                    team=team15,
                    invitee_disc_uuid=str(m.id),
                    state="pending",
                )

            mentions_line = " ".join(m.mention for m in members)
            try:
                invite_msg = await invite_thread.send(
                    content=(
                        f"🎯 **r81 pool invite** — first "
                        f"{POOL_TEAM_SIZE} of you to click **Accept** "
                        f"form Team {team_number}.\n"
                        f"Invited: {mentions_line}"
                    ),
                    view=BingoPoolInviteConfirmView(),
                    allowed_mentions=discord.AllowedMentions(
                        users=list(members), roles=False, everyone=False
                    ),
                )
            except discord.HTTPException:
                logger.exception(
                    "r81 recovery: failed to post team 15 pool invite"
                )
                team15.state = "disbanded"
                await team15.save(update_fields=["state"])
                return

            team15.pending_invite_msg_id = invite_msg.id
            await team15.save(update_fields=["pending_invite_msg_id"])
            logger.info(
                "r81 recovery: team 15 pool invite posted (%d invitees, "
                "team.id=%s, msg=%s)",
                len(members), team15.id, invite_msg.id,
            )

        self.bot.loop.create_task(_watcher())


async def setup(bot: Bot):
    await bot.add_cog(Scripts(bot))
    logger.info("Scripts cog loaded successfully")

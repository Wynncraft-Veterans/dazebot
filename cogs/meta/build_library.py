"""``/promote`` and ``/demote`` — move forum threads between #build-workshop
and #build-library by recreating them.

Discord forbids moving a thread between channels, so each command clones
the source thread into the destination forum via a webhook on the dest
(impersonating each author by display name + avatar). See
:mod:`lib.thread_clone` for the cloning primitive.

The two flows are deliberately asymmetric:

* /promote (workshop -> library) **preserves** the workshop thread: it
  locks it and pins a link back to the new library post. Audit history
  stays where staff originally vetted the build.

* /demote (library -> workshop) **deletes** the library thread once its
  contents are synced back. If a ``BuildPromotion`` row records the
  original workshop thread, demotion unlocks it and only syncs library
  messages added *after* the promotion (everything before is the bot's
  own webhook-reposted copy of the workshop's original content). If no
  row exists (rare — the build was created directly in the library),
  demotion creates a fresh workshop thread with the full library
  content.

Concurrent /promote or /demote on the same thread is serialized by an
in-process asyncio lock keyed on the thread id; if a command crashes
mid-clone, no recovery is automated — staff manually clean up the partial
dest thread and the stale source state.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

from bot import Bot
from config import CurrConfig
from lib.auth import is_staff
from lib.discord_utils.thread_clone import (
    CloneStats,
    clone_into_new_thread,
    clone_messages,
    get_clone_webhook,
)
from orm import BuildPromotion

logger = logging.getLogger("dazebot.cogs.meta.build_library")

# Pinned in the source workshop thread on /promote so users browsing the
# workshop can find the promoted version. Destination threads carry no
# bot-authored header — their starter message is the original first
# message of the source, webhook-impersonated.
_WORKSHOP_MARKER_PREFIX = "🏛️ Promoted to"
_WORKSHOP_MARKER_FMT = _WORKSHOP_MARKER_PREFIX + " {dest_url}"


class BuildLibrary(commands.Cog):
    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        # Per-thread serialization: stops a second /promote or /demote on
        # the same source from racing the first.
        self._thread_locks: dict[int, asyncio.Lock] = {}
        logger.info("BuildLibrary cog initialized")

    # -- forum helpers --------------------------------------------------

    def _config(self) -> tuple[Optional[int], Optional[int]]:
        cfg = CurrConfig.BuildLibraryConfig
        return cfg.WORKSHOP_FORUM_ID, cfg.LIBRARY_FORUM_ID

    async def _resolve_forum(self, forum_id: int) -> Optional[discord.ForumChannel]:
        channel = self.bot.get_channel(forum_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(forum_id)
            except discord.HTTPException:
                logger.exception("forum fetch failed id=%s", forum_id)
                return None
        if not isinstance(channel, discord.ForumChannel):
            logger.error("configured channel %s is not a forum", forum_id)
            return None
        return channel

    def _lock_for(self, thread_id: int) -> asyncio.Lock:
        lock = self._thread_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._thread_locks[thread_id] = lock
        return lock

    # -- /promote -------------------------------------------------------

    @commands.hybrid_command(
        name="promote",
        description="(Staff) Promote the current workshop thread to the build library.",
    )
    @is_staff()
    async def promote(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)

        workshop_id, library_id = self._config()
        if workshop_id is None or library_id is None:
            await ctx.reply(
                "The build-library forums aren't configured for this deployment.",
                ephemeral=True,
            )
            return

        thread = ctx.channel
        if not isinstance(thread, discord.Thread) or thread.parent_id != workshop_id:
            await ctx.reply(
                f"Run `/promote` inside a thread in <#{workshop_id}>.",
                ephemeral=True,
            )
            return

        library_forum = await self._resolve_forum(library_id)
        if library_forum is None:
            await ctx.reply(
                "Couldn't access the build-library forum (check bot permissions).",
                ephemeral=True,
            )
            return

        async with self._lock_for(thread.id):
            # Clean up any stale row whose library thread was manually deleted
            # — otherwise unique(workshop_thread_id) would block re-promotion.
            existing = await BuildPromotion.filter(workshop_thread_id=str(thread.id)).first()
            if existing is not None:
                still_live = await self._library_thread_exists(int(existing.library_thread_id))
                if still_live:
                    await ctx.reply(
                        f"This build is already promoted: "
                        f"https://discord.com/channels/{thread.guild.id}/{existing.library_thread_id}",
                        ephemeral=True,
                    )
                    return
                await existing.delete()

            try:
                await thread.edit(locked=True, reason="promoted to build-library")
            except discord.HTTPException:
                logger.exception("failed to lock source thread %s", thread.id)
                await ctx.reply(
                    "Couldn't lock the workshop thread (check bot permissions).",
                    ephemeral=True,
                )
                return

            try:
                webhook = await get_clone_webhook(library_forum)
            except discord.HTTPException:
                logger.exception("webhook acquire failed forum=%s", library_forum.id)
                await ctx.reply(
                    "Couldn't acquire the cloning webhook on the library forum.",
                    ephemeral=True,
                )
                return

            await ctx.reply(
                "📚 Cloning to the build library — this may take a moment.",
                ephemeral=True,
            )

            async def report(n: int) -> None:
                try:
                    await ctx.interaction.edit_original_response(
                        content=f"📚 Cloning to the build library — {n} messages copied…"
                    )
                except (discord.HTTPException, AttributeError):
                    pass

            dest_thread, stats = await clone_into_new_thread(
                thread,
                forum=library_forum,
                webhook=webhook,
                new_thread_name=thread.name,
                progress_cb=report,
            )
            if dest_thread is None:
                await ctx.reply(
                    "Couldn't clone — the source thread had no copyable messages.",
                    ephemeral=True,
                )
                return

            sync_complete_at = datetime.now(timezone.utc)
            await BuildPromotion.create(
                library_thread_id=str(dest_thread.id),
                workshop_thread_id=str(thread.id),
                promoted_by_disc_uuid=str(ctx.author.id),
                sync_complete_at=sync_complete_at,
            )

            await self._pin_marker_in_source(
                thread,
                _WORKSHOP_MARKER_FMT.format(dest_url=dest_thread.jump_url),
            )

            await self._report_final(ctx, dest_thread, stats, action="Promoted")

    # -- /demote --------------------------------------------------------

    @commands.hybrid_command(
        name="demote",
        description="(Staff) Demote the current library thread back to the build workshop.",
    )
    @is_staff()
    async def demote(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)

        workshop_id, library_id = self._config()
        if workshop_id is None or library_id is None:
            await ctx.reply(
                "The build-library forums aren't configured for this deployment.",
                ephemeral=True,
            )
            return

        thread = ctx.channel
        if not isinstance(thread, discord.Thread) or thread.parent_id != library_id:
            await ctx.reply(
                f"Run `/demote` inside a thread in <#{library_id}>.",
                ephemeral=True,
            )
            return

        workshop_forum = await self._resolve_forum(workshop_id)
        if workshop_forum is None:
            await ctx.reply(
                "Couldn't access the build-workshop forum (check bot permissions).",
                ephemeral=True,
            )
            return

        async with self._lock_for(thread.id):
            promotion = await BuildPromotion.filter(library_thread_id=str(thread.id)).first()

            original_thread: Optional[discord.Thread] = None
            if promotion is not None:
                original_thread = await self._fetch_thread(int(promotion.workshop_thread_id))

            try:
                await thread.edit(locked=True, reason="demotion in progress")
            except discord.HTTPException:
                logger.exception("failed to lock library thread %s", thread.id)
                await ctx.reply(
                    "Couldn't lock the library thread (check bot permissions).",
                    ephemeral=True,
                )
                return

            try:
                webhook = await get_clone_webhook(workshop_forum)
            except discord.HTTPException:
                logger.exception("webhook acquire failed forum=%s", workshop_forum.id)
                await ctx.reply(
                    "Couldn't acquire the cloning webhook on the workshop forum.",
                    ephemeral=True,
                )
                return

            if original_thread is not None and promotion is not None:
                stats = await self._demote_into_existing(
                    ctx, thread, original_thread, webhook, promotion, workshop_forum
                )
                if stats is None:
                    return
                dest_thread = original_thread
                action = "Demoted (synced into original workshop thread)"
            else:
                stats, dest_thread = await self._demote_into_fresh(
                    ctx, thread, workshop_forum, webhook
                )
                if dest_thread is None:
                    return
                action = "Demoted (created new workshop thread)"

            # Deliver the final summary BEFORE deleting the library thread —
            # the interaction's original response lives inside the library
            # thread, so once we delete it edit_original_response/followup
            # both 404 with Unknown Message / Unknown Channel.
            await self._report_final(ctx, dest_thread, stats, action=action)

            try:
                await thread.delete(reason=f"demoted by {ctx.author.id}")
            except discord.HTTPException:
                logger.exception("library thread delete failed id=%s", thread.id)
                try:
                    await ctx.interaction.followup.send(
                        f"⚠️ Couldn't delete the library thread — please remove it manually.",
                        ephemeral=True,
                    )
                except (discord.HTTPException, AttributeError):
                    logger.exception("delete-failed followup send failed")

            if promotion is not None:
                await promotion.delete()

    async def _demote_into_existing(
        self,
        ctx: commands.Context,
        library_thread: discord.Thread,
        original_thread: discord.Thread,
        webhook: discord.Webhook,
        promotion: BuildPromotion,
        workshop_forum: discord.ForumChannel,
    ) -> Optional[CloneStats]:
        try:
            await original_thread.edit(locked=False, reason="demotion: reopening original")
        except discord.HTTPException:
            logger.exception("failed to unlock original workshop thread %s", original_thread.id)
            await ctx.reply(
                "Couldn't unlock the original workshop thread (check bot permissions).",
                ephemeral=True,
            )
            return None

        await ctx.reply(
            f"↩️ Syncing new messages into {original_thread.jump_url} — this may take a moment.",
            ephemeral=True,
        )

        async def report(n: int) -> None:
            try:
                await ctx.interaction.edit_original_response(
                    content=f"↩️ Syncing into {original_thread.jump_url} — {n} messages copied…"
                )
            except (discord.HTTPException, AttributeError):
                pass

        return await clone_messages(
            library_thread,
            dest_thread=original_thread,
            webhook=webhook,
            after=promotion.sync_complete_at,
            progress_cb=report,
        )

    async def _demote_into_fresh(
        self,
        ctx: commands.Context,
        library_thread: discord.Thread,
        workshop_forum: discord.ForumChannel,
        webhook: discord.Webhook,
    ) -> tuple[CloneStats, Optional[discord.Thread]]:
        await ctx.reply(
            "↩️ Cloning to a new workshop thread — this may take a moment.",
            ephemeral=True,
        )

        async def report(n: int) -> None:
            try:
                await ctx.interaction.edit_original_response(
                    content=f"↩️ Cloning to the workshop — {n} messages copied…"
                )
            except (discord.HTTPException, AttributeError):
                pass

        dest_thread, stats = await clone_into_new_thread(
            library_thread,
            forum=workshop_forum,
            webhook=webhook,
            new_thread_name=library_thread.name,
            progress_cb=report,
        )
        if dest_thread is None:
            await ctx.reply(
                "Couldn't clone — the source thread had no copyable messages.",
                ephemeral=True,
            )
        return stats, dest_thread

    # -- shared utilities -----------------------------------------------

    async def _library_thread_exists(self, thread_id: int) -> bool:
        try:
            ch = await self.bot.fetch_channel(thread_id)
        except (discord.NotFound, discord.HTTPException):
            return False
        return isinstance(ch, discord.Thread)

    async def _fetch_thread(self, thread_id: int) -> Optional[discord.Thread]:
        cached = self.bot.get_channel(thread_id)
        if isinstance(cached, discord.Thread):
            return cached
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except (discord.NotFound, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    async def _pin_marker_in_source(self, thread: discord.Thread, body: str) -> None:
        # Tear down any prior promotion markers so only one ever exists at a
        # time — re-promote/re-demote cycles otherwise pile them up in the
        # workshop's pin list, including stale ones pointing at deleted
        # library threads.
        me = self.bot.user
        if me is not None:
            try:
                async for pin in thread.pins():
                    if pin.author.id == me.id and pin.content.startswith(
                        _WORKSHOP_MARKER_PREFIX
                    ):
                        try:
                            await pin.delete()
                        except discord.HTTPException:
                            logger.exception(
                                "failed to delete prior promotion marker %s", pin.id
                            )
            except discord.HTTPException:
                logger.exception("failed to enumerate pins in %s", thread.id)

        try:
            msg = await thread.send(body, allowed_mentions=discord.AllowedMentions.none())
            await msg.pin(reason="promotion marker")
        except discord.HTTPException:
            logger.exception("failed to post/pin promotion marker in %s", thread.id)

    async def _report_final(
        self,
        ctx: commands.Context,
        dest_thread: discord.Thread,
        stats: CloneStats,
        *,
        action: str,
    ) -> None:
        oversize_note = ""
        if stats.oversize_skipped:
            oversize_note = (
                f"\n⚠️ {stats.oversize_skipped} oversized attachment(s) were inlined "
                f"as links to the source CDN. If the source thread is later deleted, "
                f"those links will stop working."
            )
        pin_note = ""
        if stats.pin_failures:
            pin_note = f"\n⚠️ {stats.pin_failures} pin(s) failed (check bot Manage Messages perm)."
        summary = (
            f"✅ {action}: {dest_thread.jump_url}\n"
            f"• {stats.messages_copied} messages copied\n"
            f"• {stats.pinned} pin(s) mirrored"
            f"{pin_note}"
            f"{oversize_note}"
        )
        try:
            await ctx.interaction.edit_original_response(content=summary)
        except (discord.HTTPException, AttributeError):
            try:
                await ctx.reply(summary, ephemeral=True)
            except discord.HTTPException:
                logger.exception("failed to deliver final summary")


async def setup(bot: Bot):
    await bot.add_cog(BuildLibrary(bot))
    logger.info("BuildLibrary cog loaded successfully")

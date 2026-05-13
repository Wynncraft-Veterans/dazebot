"""Forum-thread cloning primitive used by /promote and /demote.

Discord doesn't let you move a thread between channels. To "move" one, we
recreate it: read the source's history oldest-first and re-post every
message into a destination thread via a webhook on the destination forum,
impersonating each original author by their display name + avatar.

This module is intentionally cog-agnostic — it knows how to clone messages
but not about the workshop/library forums, the BuildPromotion table, or
locks/deletes. The cog orchestrates those.

Things this preserves: text content, embeds, attachments under the
upload-size limit, reply-context (as a quoted prefix line, since webhook
posts can't ``reference`` cross-thread), pinned status.

Things this drops: reactions, stickers, exact timestamps (Discord has no
API to backdate a message), and bot/system messages from the source.

Attachments larger than the upload limit are inlined as their original CDN
URL with a ``[oversized attachment]`` note. The link survives as long as the
source thread isn't deleted — /promote leaves the source locked so the
links stay live, but /demote deletes the source library thread, so on
demote staff get a warning if any oversize files were dropped.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional

import discord

logger = logging.getLogger("dazebot.lib.discord_utils.thread_clone")

# Level 3 boost = 100 MB upload limit. Conservative to leave headroom for
# multipart overhead.
_FILE_SIZE_LIMIT = 95 * 1024 * 1024

_WEBHOOK_NAME = "dazebot-thread-clone"

_REPLY_SNIPPET_MAX = 80

# Webhook usernames can't contain these strings (Discord rejects them).
_FORBIDDEN_USERNAME_FRAGMENTS = ("discord", "clyde", "```", "@everyone", "@here")


@dataclass
class CloneStats:
    messages_copied: int = 0
    pinned: int = 0
    pin_failures: int = 0
    oversize_skipped: int = 0
    oversize_urls: list[str] = field(default_factory=list)


def _sanitize_username(name: str) -> str:
    """Coerce a Discord display name into a valid webhook username."""
    cleaned = name.strip() or "user"
    lowered = cleaned.lower()
    for bad in _FORBIDDEN_USERNAME_FRAGMENTS:
        if bad in lowered:
            cleaned = re.sub(re.escape(bad), "*" * len(bad), cleaned, flags=re.IGNORECASE)
            lowered = cleaned.lower()
    return cleaned[:80] or "user"


async def get_clone_webhook(forum: discord.ForumChannel) -> discord.Webhook:
    """Return our named webhook on this forum, creating one if needed.

    Requires Manage Webhooks on the forum channel.
    """
    me = forum.guild.me
    for w in await forum.webhooks():
        if w.name == _WEBHOOK_NAME and (w.user is None or me is None or w.user.id == me.id):
            return w
    return await forum.create_webhook(name=_WEBHOOK_NAME, reason="build thread clone")


def _should_skip_message(message: discord.Message) -> bool:
    """Filter out system messages and the bot's own non-content posts.

    Webhook-authored messages from a *previous* clone are NOT skipped — those
    carry impersonated human content (display name + avatar) and are valid
    content from the source thread's perspective. Within a single clone
    operation the cog uses ``after=`` to avoid re-cloning the webhook messages
    we just wrote.

    Regular bot-authored messages (e.g. the workshop "Promoted to …"
    marker pin) ARE skipped so they don't propagate into the destination
    on re-promote / re-demote cycles.
    """
    if message.type not in (
        discord.MessageType.default,
        discord.MessageType.reply,
    ):
        return True
    if message.author.bot and message.webhook_id is None:
        return True
    return False


async def _materialize_attachments(
    attachments: list[discord.Attachment],
) -> tuple[list[discord.File], list[str]]:
    """Download attachments into in-memory ``discord.File`` objects.

    Returns ``(files, oversize_urls)`` — oversize_urls contains the source
    CDN URLs for any file that exceeded the upload limit (or failed to
    download). Caller is expected to inline them into the message content.
    """
    files: list[discord.File] = []
    oversize_urls: list[str] = []
    for a in attachments:
        if a.size > _FILE_SIZE_LIMIT:
            oversize_urls.append(a.url)
            continue
        try:
            data = await a.read()
        except (discord.HTTPException, discord.NotFound):
            logger.exception("attachment download failed url=%s", a.url)
            oversize_urls.append(a.url)
            continue
        files.append(
            discord.File(io.BytesIO(data), filename=a.filename, spoiler=a.is_spoiler())
        )
    return files, oversize_urls


def _format_reply_prefix(message: discord.Message) -> Optional[str]:
    ref = message.reference
    if ref is None or not isinstance(ref.resolved, discord.Message):
        return None
    ref_msg = ref.resolved
    ref_author = ref_msg.author.display_name if ref_msg.author else "unknown"
    snippet = (ref_msg.content or "[attachment/embed]").strip().replace("\n", " ")
    if len(snippet) > _REPLY_SNIPPET_MAX:
        snippet = snippet[: _REPLY_SNIPPET_MAX - 1] + "…"
    return f"↪ **{ref_author}**: {snippet}"


def _compose_content(message: discord.Message, oversize_urls: list[str]) -> str:
    parts: list[str] = []
    reply = _format_reply_prefix(message)
    if reply is not None:
        parts.append(reply)
    if message.content:
        parts.append(message.content)
    if oversize_urls:
        parts.append(
            "\n".join(f"[oversized attachment]({u})" for u in oversize_urls)
        )
    # Webhook send() requires content OR embeds OR files. A genuinely empty
    # message (e.g. a sticker-only post we've stripped) would 400 — guard with
    # a zero-width space when we have nothing else.
    if not parts and not message.embeds:
        return "​"
    return "\n".join(parts)


async def clone_into_new_thread(
    src: discord.Thread,
    *,
    forum: discord.ForumChannel,
    webhook: discord.Webhook,
    new_thread_name: str,
    progress_cb: Optional[Callable[[int], Awaitable[None]]] = None,
    progress_every: int = 10,
) -> tuple[Optional[discord.Thread], CloneStats]:
    """Create a new forum post whose starter message is the source's first
    content message (webhook-impersonated), then clone the rest.

    Forum posts always need a starter message; ``forum.create_thread()``
    would make that starter bot-authored. Using ``webhook.send(thread_name=...)``
    lets the starter carry the original author's display name and avatar
    instead. Returns ``(thread, stats)`` — ``thread`` is ``None`` if the
    source had no copyable content messages.
    """
    first_msg: Optional[discord.Message] = None
    async for m in src.history(limit=None, oldest_first=True):
        if not _should_skip_message(m):
            first_msg = m
            break

    if first_msg is None:
        return None, CloneStats()

    files, oversize = await _materialize_attachments(list(first_msg.attachments))
    try:
        posted = await webhook.send(
            thread_name=new_thread_name,
            content=_compose_content(first_msg, oversize),
            username=_sanitize_username(first_msg.author.display_name),
            avatar_url=first_msg.author.display_avatar.url,
            embeds=list(first_msg.embeds),
            files=files,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        logger.exception("webhook thread-create failed src=%s", src.id)
        return None, CloneStats()

    new_thread_id = posted.channel.id
    new_thread = forum.guild.get_thread(new_thread_id)
    if new_thread is None:
        try:
            fetched = await forum.guild.fetch_channel(new_thread_id)
        except (discord.NotFound, discord.HTTPException):
            logger.exception("post-create thread fetch failed id=%s", new_thread_id)
            return None, CloneStats()
        if not isinstance(fetched, discord.Thread):
            logger.error("created channel %s is not a thread", new_thread_id)
            return None, CloneStats()
        new_thread = fetched

    stats = CloneStats(messages_copied=1, oversize_skipped=len(oversize))
    stats.oversize_urls.extend(oversize)
    if first_msg.pinned:
        try:
            await posted.pin(reason="mirror source-thread pin")
            stats.pinned += 1
        except discord.HTTPException:
            stats.pin_failures += 1
            logger.exception("pin failed on starter message=%s", posted.id)
        await asyncio.sleep(0.5)

    rest = await clone_messages(
        src,
        dest_thread=new_thread,
        webhook=webhook,
        after=first_msg,
        progress_cb=progress_cb,
        progress_every=progress_every,
    )
    stats.messages_copied += rest.messages_copied
    stats.pinned += rest.pinned
    stats.pin_failures += rest.pin_failures
    stats.oversize_skipped += rest.oversize_skipped
    stats.oversize_urls.extend(rest.oversize_urls)

    return new_thread, stats


async def clone_messages(
    src: discord.Thread,
    *,
    dest_thread: discord.Thread,
    webhook: discord.Webhook,
    after: Optional[discord.abc.Snowflake | datetime] = None,
    progress_cb: Optional[Callable[[int], Awaitable[None]]] = None,
    progress_every: int = 10,
) -> CloneStats:
    """Stream messages from ``src`` (oldest-first) into ``dest_thread`` via ``webhook``.

    ``after`` (if given) limits to messages strictly after that timestamp —
    used by /demote to copy only the messages added to the library post-
    promotion.

    ``progress_cb`` is invoked with the running count every
    ``progress_every`` messages so callers can edit the interaction reply.

    Pins on ``src`` are mirrored onto the corresponding new messages in
    ``dest_thread`` after the copy loop, best-effort.
    """
    stats = CloneStats()
    pin_pairs: list[tuple[int, discord.WebhookMessage]] = []

    history_kwargs: dict = {"limit": None, "oldest_first": True}
    if after is not None:
        history_kwargs["after"] = after

    async for message in src.history(**history_kwargs):
        if _should_skip_message(message):
            continue

        files, oversize = await _materialize_attachments(list(message.attachments))
        stats.oversize_skipped += len(oversize)
        stats.oversize_urls.extend(oversize)

        try:
            posted = await webhook.send(
                content=_compose_content(message, oversize),
                username=_sanitize_username(message.author.display_name),
                avatar_url=message.author.display_avatar.url,
                embeds=list(message.embeds),
                files=files,
                thread=dest_thread,
                wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "webhook send failed src_thread=%s message=%s",
                src.id, message.id,
            )
            continue

        stats.messages_copied += 1
        if message.pinned:
            pin_pairs.append((message.id, posted))

        if progress_cb is not None and stats.messages_copied % progress_every == 0:
            try:
                await progress_cb(stats.messages_copied)
            except Exception:
                logger.exception("progress callback failed")

    for _src_id, posted in pin_pairs:
        try:
            await posted.pin(reason="mirror source-thread pin")
            stats.pinned += 1
        except discord.HTTPException:
            stats.pin_failures += 1
            logger.exception("pin failed in dest thread message=%s", posted.id)
        # Discord rate-limits pin operations more strictly than sends; small
        # delay keeps us out of the global bucket on multi-pin threads.
        await asyncio.sleep(0.5)

    return stats

"""Week 90: The Bulletin — an announcement whose image is replaced by faxes.

Players fax PDFs to the number on a business card. Everything about the faxes
— intake, encryption, the moderation queue, the one-hour hold, the archive —
lives in the external **r90-bulletin** app (``temporary-static-projects``
branch ``r90-bulletin``, vets-deploy stack ``r90-bulletin``). This module is
the Discord half, and it only *mirrors*:

* every 60 s it pulls ``GET /bot/state`` from r90 over the ``proxy`` Docker
  network,
* posts the staff pings r90 has queued (and acks each one),
* keeps the announcement's text showing how many faxes are waiting and when
  one sent now would go up,
* makes the announcement's attachments match what r90 says is wanted, and
* tells r90 once they do (``POST /bot/displayed``) — the hold starts at that
  ack, so an image is never "up for an hour" that nobody could see.

Pull rather than push, so dazebot grows no new inbound surface, and a restart
on either side self-heals on the next tick. No fax content is ever stored
here; :class:`orm_returns.Return90Post` holds pointers and flags only.

Every path that touches the message runs under :data:`_lock`, so the tick, a
staff ``sync`` and an emergency ``takedown`` can never interleave into a
double ping or an edit that undoes a takedown.

Env: ``R90_BASE_URL`` (``http://r90-bulletin:8001``), ``R90_BOT_SECRET``
(matches the r90 stack), ``R90_PUBLIC_BASE_URL`` (the admin link in pings).
With the first two unset the tick is a no-op, which is the normal state of
every deployment outside the event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from io import BytesIO
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

from cogs.events.returns import Tier, register_manage, register_tick
from cogs.events.returns._common import is_persist_context, send_feedback
from orm_returns import Return90Post

logger = logging.getLogger("dazebot.cogs.events.returns.week_90")

WEEK = 90

# Consecutive failures to put the same wanted item up before giving up on it.
# For a fax, r90 then takes it out of the queue so the next one can go up.
MAX_DISPLAY_FAILURES = 3

# Posted by `post`, above the card, with the live queue line under it.
# Changing it also updates a live announcement: the first reconcile after a
# restart edits the message's content to match (see :func:`_sync_body`).
_ANNOUNCEMENT_HEAD: str = (
    "# Retirement Home Bulletin\n"
    "*Anything you send here will be displayed for at least 1h!*\n"
    "\n"
    "# `+1 807 500 VETS`\n"
    "\n"
    "-# Fax us your best vets memes, stories, art, sketches, and any other silliness!"
)


def _slot_text(state: dict) -> str:
    """When a fax approved now would go up, as a Discord relative timestamp.

    r90 sends the slot as an absolute time that holds still while the queue
    and the current hold do, so Discord counts it down client-side and we
    never edit the message just because time passed. Rounded to a whole
    minute so a stray second cannot cost an edit either.
    """
    try:
        at = float(state["next_slot_at"])
    except (KeyError, TypeError, ValueError):
        return "once the fax before it has had its hour"
    if at - time.time() <= 90:
        return "at the next check, within a minute or two"
    return f"<t:{int(at // 60 + 1) * 60}:R>"


def _queue_line(state: dict) -> str:
    """The one line under the blurb: how many faxes are waiting, and when one
    sent now would go up. Both only move when the queue does."""
    if state.get("intake_paused"):
        return "-# The fax line is closed right now - anything sent to it is not read."
    depth = int(state.get("queue_depth") or 0)
    waiting = ("Nothing is waiting to go up" if depth == 0
               else "1 fax is waiting to go up" if depth == 1
               else f"{depth} faxes are waiting to go up")
    return f"-# {waiting} - a fax approved now goes up {_slot_text(state)}."


def _body(state: dict) -> str:
    return "\n".join((_ANNOUNCEMENT_HEAD, _queue_line(state)))


_lock = asyncio.Lock()
_session: aiohttp.ClientSession | None = None
_failures: dict[tuple, int] = {}
_last_unreachable_log = 0.0
# The text we last put on the announcement, so a tick that changes nothing
# costs neither a message fetch nor an edit. None until the first reconcile
# of the process, which fetches the message once to see what is really there.
_body_shown: Optional[str] = None

_ID_RE = re.compile(r"^<?[#@&!]*(\d{15,21})>?$")


class R90Unavailable(Exception):
    """r90 could not be reached, or refused our secret. Nothing was changed."""


class _Gone(Exception):
    """The wanted fax was retracted between the state pull and the fetch."""


class _FetchFailed(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP to r90
# ---------------------------------------------------------------------------


def _base_url() -> str:
    return os.environ.get("R90_BASE_URL", "").strip().rstrip("/")


def _secret() -> str:
    return os.environ.get("R90_BOT_SECRET", "").strip()


def _public_base() -> str:
    return os.environ.get("R90_PUBLIC_BASE_URL", "https://return.wynnvets.org").strip().rstrip("/")


def _configured() -> bool:
    return bool(_base_url() and _secret())


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={"User-Agent": "dazebot/1.0 (return-90-bulletin)"},
            timeout=aiohttp.ClientTimeout(total=10),
        )
    return _session


async def _call(method: str, path: str, *, json_body: Optional[dict] = None) -> tuple[int, bytes]:
    """One request to r90. Raises :class:`R90Unavailable` on transport errors,
    5xx, and 401/503 (a secret mismatch is as good as down for our purposes)."""
    try:
        session = await _get_session()
        async with session.request(
            method, _base_url() + path, json=json_body, headers={"X-R90-Secret": _secret()}
        ) as res:
            body = await res.read()
            status = res.status
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise R90Unavailable(type(exc).__name__) from exc
    if status >= 500 or status in (401, 403):
        raise R90Unavailable(f"HTTP {status}")
    return status, body


def _json(body: bytes) -> dict:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _fetch_state() -> dict:
    status, body = await _call("GET", "/bot/state")
    data = _json(body)
    if status != 200 or "wanted" not in data:
        raise R90Unavailable(f"bad state response (HTTP {status})")
    return data


async def _ack(epoch: int, kind: str, sub_id: Optional[int]) -> None:
    try:
        status, _ = await _call(
            "POST", "/bot/displayed", json_body={"kind": kind, "id": sub_id, "epoch": epoch}
        )
    except R90Unavailable as exc:
        # The next tick sees shown == wanted but current != wanted and re-acks.
        logger.warning("r90 ack for %s/%s not delivered: %s", kind, sub_id, exc)
        return
    if status != 200:
        logger.warning("r90 refused ack for %s/%s: HTTP %d", kind, sub_id, status)


async def _fetch_files(wanted: dict) -> list[discord.File]:
    version = time.time_ns()
    if wanted.get("kind") == "card":
        status, body = await _call("GET", "/bot/card")
        if status != 200:
            raise _FetchFailed(f"card HTTP {status}")
        return [discord.File(BytesIO(body), filename=f"r90_card_v{version}.png")]
    sub_id = int(wanted["id"])
    files = []
    for n in range(1, int(wanted.get("pages") or 0) + 1):
        status, body = await _call("GET", f"/bot/sub/{sub_id}/display/{n}")
        if status == 410:
            raise _Gone()
        if status != 200:
            raise _FetchFailed(f"page {n} HTTP {status}")
        # A fresh filename per edit gives fresh CDN URLs, which is what busts
        # client-side image caches (week_81 does the same).
        files.append(discord.File(BytesIO(body), filename=f"r90_s{sub_id}_p{n}_v{version}.png"))
    if not files:
        raise _FetchFailed("no pages")
    return files


# ---------------------------------------------------------------------------
# Discord helpers
# ---------------------------------------------------------------------------


def _parse_id(raw: str) -> Optional[int]:
    m = _ID_RE.match(raw.strip())
    return int(m.group(1)) if m else None


async def _resolve_channel(bot: discord.Client, channel_id: Optional[int]):
    if not channel_id:
        return None
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.warning("r90 channel %s not fetchable: %s", channel_id, e)
            return None
    return ch if hasattr(ch, "send") and hasattr(ch, "fetch_message") else None


async def _fetch_announcement(bot: discord.Client, post: Return90Post) -> discord.Message:
    """Raises discord.NotFound if the channel or the message is gone."""
    ch = bot.get_channel(post.channel_id) or await bot.fetch_channel(post.channel_id)
    return await ch.fetch_message(post.message_id)


def _link(bot: discord.Client, channel_id: Optional[int], message_id: Optional[int]) -> str:
    if not channel_id or not message_id:
        return "not posted"
    ch = bot.get_channel(channel_id)
    guild = getattr(getattr(ch, "guild", None), "id", None) or "@me"
    return f"https://discord.com/channels/{guild}/{channel_id}/{message_id}"


def _label(kind: Optional[str], sub_id: Optional[int]) -> str:
    if kind == "card":
        return "the card"
    if kind == "sub":
        return f"fax #{sub_id}"
    if kind == "cleared":
        return f"nothing (fax #{sub_id} taken down)"
    return str(kind)


async def _staff_note(bot: discord.Client, post: Return90Post, text: str) -> None:
    channel = await _resolve_channel(bot, post.staff_channel_id)
    if channel is None:
        logger.warning("r90 staff note undeliverable: %s", text)
        return
    try:
        await channel.send(f"📠 {text}", allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        logger.warning("r90 staff note failed: %s", e)


async def _deliver_notifications(bot: discord.Client, post: Return90Post, notes: list[dict]) -> int:
    """Post r90's queued notices to the staff channel, acking each as it lands.

    Only a new submission pings, and only the configured role: the mention is
    restricted in ``allowed_mentions`` (the ``lib/staff/rank_alerts.py``
    pattern), so nothing in a note can ping anyone else.
    """
    if not notes or not post.staff_channel_id:
        return 0
    channel = await _resolve_channel(bot, post.staff_channel_id)
    if channel is None:
        return 0
    sent = 0
    for note in notes:
        if note.get("kind") == "new_submission":
            pages = int(note.get("pages") or 0)
            prefix = f"<@&{post.ping_role_id}> " if post.ping_role_id else ""
            content = (
                f"{prefix}New fax #{note.get('submission_id')} ({pages} page"
                f"{'' if pages == 1 else 's'}) awaiting review → {_public_base()}/admin"
            )
            allowed = discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=[discord.Object(id=post.ping_role_id)] if post.ping_role_id else False,
            )
        else:
            content = f"📠 {note.get('text', '')}"
            allowed = discord.AllowedMentions.none()
        try:
            await channel.send(content, allowed_mentions=allowed)
        except discord.HTTPException as e:
            logger.warning("r90 notification send failed: %s", e)
            return sent
        sent += 1
        try:
            await _call("POST", "/bot/notifications/ack", json_body={"ids": [note["id"]]})
        except R90Unavailable:
            return sent
    return sent


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


async def _load_post() -> Optional[Return90Post]:
    return await Return90Post.filter(id=1).first()


async def _reconcile(bot: discord.Client) -> str:
    async with _lock:
        return await _reconcile_locked(bot)


async def _reconcile_locked(bot: discord.Client) -> str:
    """Converge the announcement toward r90's wanted state. Caller holds the lock.

    Returns a one-line summary for ``sync``. Raises :class:`R90Unavailable`.
    """
    if not _configured():
        return "R90_BASE_URL / R90_BOT_SECRET are not set."
    post = await _load_post()
    if post is None:
        return "Nothing set up yet — run `notify` and `post`."
    if post.ended:
        return "Ended — the tick is a no-op."
    state = await _fetch_state()
    delivered = await _deliver_notifications(bot, post, state.get("notifications") or [])
    prefix = f"Delivered {delivered} notification(s). " if delivered else ""
    if not post.message_id:
        return prefix + "No announcement posted yet."
    if not await _sync_body(bot, post, state):
        return prefix + "The announcement message is gone."
    if post.frozen:
        return prefix + "Frozen after a takedown — run `resume` once r90 is back."
    return prefix + await _converge(bot, post, state)


async def _sync_body(bot: discord.Client, post: Return90Post, state: dict) -> bool:
    """Make the announcement's text match :func:`_body`.

    Only ``content`` is edited, so attachments (and a takedown) are untouched.
    The text is the blurb plus a queue line whose two moving parts - the count
    and an absolute timestamp - change only when the queue itself does, so
    this is one edit per queue change and nothing on a quiet tick.

    Returns False if the message is gone. Other failures retry next tick.
    """
    global _body_shown
    body = _body(state)
    if _body_shown == body:
        return True
    try:
        msg = await _fetch_announcement(bot, post)
        if (msg.content or None) != body:
            await msg.edit(content=body)
    except discord.NotFound:
        await _message_gone(bot, post)
        return False
    except discord.HTTPException as exc:
        logger.warning("r90 announcement text not updated: %s", exc)
        return True
    _body_shown = body
    return True


async def _converge(bot: discord.Client, post: Return90Post, state: dict) -> str:
    epoch = int(state.get("epoch") or 0)
    wanted = state["wanted"]
    current = state.get("current") or {}
    want = (wanted.get("kind"), wanted.get("id"))

    if post.shown_epoch == epoch and (post.shown_kind, post.shown_id) == want:
        _failures.clear()
        if (current.get("kind"), current.get("id")) != want:
            # We put it up but r90 never heard: the ack was lost. Re-send.
            await _ack(epoch, *want)
            return f"Showing {_label(*want)}; re-sent the ack."
        return f"In sync: showing {_label(*want)}."

    key = (epoch, *want)
    try:
        files = await _fetch_files(wanted)
    except _Gone:
        return f"{_label(*want)} was retracted mid-sync; next tick picks up the new state."
    except _FetchFailed as exc:
        return await _count_failure(bot, post, key, wanted, str(exc))

    try:
        msg = await _fetch_announcement(bot, post)
    except discord.NotFound:
        await _message_gone(bot, post)
        return "The announcement message is gone."
    except discord.HTTPException as exc:
        return await _count_failure(bot, post, key, wanted, f"fetch message HTTP {exc.status}")

    try:
        # Content is left alone; attachments= replaces the file list wholesale.
        await msg.edit(attachments=files)
    except discord.HTTPException as exc:
        return await _count_failure(bot, post, key, wanted, f"edit HTTP {exc.status}")

    post.shown_kind, post.shown_id, post.shown_epoch = want[0], want[1], epoch
    await post.save(update_fields=["shown_kind", "shown_id", "shown_epoch", "updated_at"])
    _failures.clear()
    await _ack(epoch, *want)
    return f"Now showing {_label(*want)}."


async def _count_failure(bot, post: Return90Post, key: tuple, wanted: dict, reason: str) -> str:
    if key not in _failures:
        _failures.clear()
    _failures[key] = _failures.get(key, 0) + 1
    count = _failures[key]
    logger.warning("r90 display of %s failed (%d/%d): %s", key, count, MAX_DISPLAY_FAILURES, reason)
    if count < MAX_DISPLAY_FAILURES:
        return f"Couldn't show {_label(wanted.get('kind'), wanted.get('id'))} ({count}/{MAX_DISPLAY_FAILURES}): {reason}"
    _failures.clear()
    label = _label(wanted.get("kind"), wanted.get("id"))
    if wanted.get("kind") == "sub":
        try:
            await _call("POST", "/bot/display_failed", json_body={"id": wanted["id"], "reason": reason[:60]})
        except R90Unavailable:
            pass
        await _staff_note(bot, post, f"Couldn't put {label} on the announcement after "
                                     f"{MAX_DISPLAY_FAILURES} tries ({reason}). It's out of the queue; "
                                     "re-queue or retract it in /admin.")
    else:
        await _staff_note(bot, post, f"Couldn't put the card back on the announcement after "
                                     f"{MAX_DISPLAY_FAILURES} tries ({reason}).")
    return f"Gave up on {label}: {reason}"


async def _message_gone(bot: discord.Client, post: Return90Post) -> None:
    """Alert once: clearing ``message_id`` is what stops this repeating."""
    post.message_id = None
    await post.save(update_fields=["message_id", "updated_at"])
    await _staff_note(bot, post, "The r90 announcement message was deleted or can't be reached. "
                                 "Repost with `~manage_return 90 post <channel> --force`.")


async def _clear_attachments(bot: discord.Client, post: Return90Post, *, freeze: bool) -> bool:
    try:
        msg = await _fetch_announcement(bot, post)
        await msg.edit(attachments=[])
    except discord.HTTPException as exc:
        logger.error("r90 takedown could not clear the announcement: %s", exc)
        return False
    post.shown_kind = "cleared"
    post.frozen = post.frozen or freeze
    await post.save(update_fields=["shown_kind", "frozen", "updated_at"])
    return True


@register_tick(WEEK)
async def tick(bot: discord.Client) -> None:
    global _last_unreachable_log
    try:
        await _reconcile(bot)
    except R90Unavailable as exc:
        # Every minute while r90 is down would drown the log; once per 10 min.
        if time.monotonic() - _last_unreachable_log > 600:
            _last_unreachable_log = time.monotonic()
            logger.warning("r90 unreachable: %s", exc)


# ---------------------------------------------------------------------------
# ~manage_return 90
# ---------------------------------------------------------------------------


@register_manage(
    WEEK, "notify", tier=Tier.ADMIN,
    help="Set the staff channel for fax pings and the role to ping.",
    usage="<staff_channel_id> <role_id>",
)
async def _manage_notify(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if len(args) != 2 or _parse_id(args[0]) is None or _parse_id(args[1]) is None:
        await send_feedback(ctx, "Usage: `~manage_return 90 notify <staff_channel_id> <role_id>`",
                            persist=persist)
        return
    channel_id, role_id = _parse_id(args[0]), _parse_id(args[1])
    if await _resolve_channel(ctx.bot, channel_id) is None:
        await send_feedback(ctx, f"I can't see a text channel with id `{channel_id}`.", persist=persist)
        return
    async with _lock:
        post, _ = await Return90Post.get_or_create(id=1)
        post.staff_channel_id = channel_id
        post.ping_role_id = role_id
        await post.save(update_fields=["staff_channel_id", "ping_role_id", "updated_at"])
    await send_feedback(ctx, f"✅ Fax pings go to channel `{channel_id}`, pinging role `{role_id}`.",
                        persist=persist)


@register_manage(
    WEEK, "post", tier=Tier.OPERATOR,
    help="Post the announcement with the card. Refuses unless r90 has the card wanted and "
         "current with an empty queue. `--force` replaces an existing announcement.",
    usage="<channel_id> [--force]",
)
async def _manage_post(ctx: commands.Context, args: list[str]) -> None:
    global _body_shown
    persist = is_persist_context(ctx)
    force = "--force" in args
    rest = [a for a in args if a != "--force"]
    if len(rest) != 1 or _parse_id(rest[0]) is None:
        await send_feedback(ctx, "Usage: `~manage_return 90 post <channel_id> [--force]`", persist=persist)
        return
    if not _configured():
        await send_feedback(ctx, "R90_BASE_URL / R90_BOT_SECRET are not set.", persist=persist)
        return
    async with _lock:
        try:
            state = await _fetch_state()
        except R90Unavailable as exc:
            await send_feedback(ctx, f"r90 is unreachable ({exc}).", persist=persist)
            return
        wanted, current = state["wanted"], state.get("current") or {}
        if wanted.get("kind") != "card" or current.get("kind") != "card" or state.get("queue_depth"):
            await send_feedback(
                ctx,
                "Refusing: r90 must have the card both wanted and current with an empty queue "
                f"(wanted {_label(wanted.get('kind'), wanted.get('id'))}, "
                f"current {_label(current.get('kind'), current.get('id'))}, "
                f"queue {state.get('queue_depth')}). Use /admin → Settings → Reset first.",
                persist=persist,
            )
            return
        post = await _load_post()
        if post is not None and post.message_id and not force:
            await send_feedback(
                ctx,
                f"An announcement already exists: {_link(ctx.bot, post.channel_id, post.message_id)}\n"
                "Pass `--force` to post a new one (the old message is left as it is).",
                persist=persist,
            )
            return
        channel = await _resolve_channel(ctx.bot, _parse_id(rest[0]))
        if channel is None:
            await send_feedback(ctx, f"I can't post in channel `{rest[0]}`.", persist=persist)
            return
        try:
            files = await _fetch_files({"kind": "card"})
        except (_FetchFailed, R90Unavailable) as exc:
            await send_feedback(ctx, f"Couldn't fetch the card from r90 ({exc}).", persist=persist)
            return
        body = _body(state)
        msg = await channel.send(
            body, files=files, allowed_mentions=discord.AllowedMentions.none()
        )
        if post is None:
            post = await Return90Post.create(id=1)
        post.channel_id = channel.id
        post.message_id = msg.id
        post.shown_kind, post.shown_id, post.shown_epoch = "card", None, int(state.get("epoch") or 0)
        post.frozen = False
        post.ended = False
        # Deliberately not touching staff_channel_id / ping_role_id: `notify`
        # owns those, and a forced repost must not silence the pings.
        await post.save()
        _failures.clear()
        _body_shown = body
    await send_feedback(ctx, f"✅ Posted: {msg.jump_url}", persist=persist)


@register_manage(
    WEEK, "status", tier=Tier.STAFF,
    help="Show the announcement, what dazebot is showing, and r90's state.",
)
async def _manage_status(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    post = await _load_post()
    lines = ["**Return 90 — The Bulletin**"]
    if not _configured():
        lines.append("⚠️ R90_BASE_URL / R90_BOT_SECRET are not set.")
    if post is None:
        lines.append("Not set up — run `notify` and `post`.")
    else:
        lines.append(f"Announcement: {_link(ctx.bot, post.channel_id, post.message_id)}")
        lines.append(f"dazebot is showing: {_label(post.shown_kind, post.shown_id)} (epoch {post.shown_epoch})")
        lines.append(f"Staff channel `{post.staff_channel_id}` · ping role `{post.ping_role_id}`")
        flags = [name for name, on in (("FROZEN", post.frozen), ("ENDED", post.ended)) if on]
        if flags:
            lines.append("Flags: " + ", ".join(flags))
    if _configured():
        try:
            state = await _fetch_state()
            w, c = state["wanted"], state.get("current") or {}
            lines.append(
                f"r90: reachable · epoch {state.get('epoch')} · wanted {_label(w.get('kind'), w.get('id'))} · "
                f"current {_label(c.get('kind'), c.get('id'))} · queue {state.get('queue_depth')} · "
                f"intake {'PAUSED' if state.get('intake_paused') else 'open'} · "
                f"{len(state.get('notifications') or [])} notification(s) waiting"
            )
        except R90Unavailable as exc:
            lines.append(f"r90: **unreachable** ({exc})")
    await send_feedback(ctx, "\n".join(lines), persist=persist)


@register_manage(WEEK, "sync", tier=Tier.STAFF, help="Reconcile the announcement with r90 now.")
async def _manage_sync(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    try:
        summary = await _reconcile(ctx.bot)
    except R90Unavailable as exc:
        summary = f"r90 is unreachable ({exc})."
    await send_feedback(ctx, summary, persist=persist)


@register_manage(
    WEEK, "takedown", tier=Tier.STAFF,
    help="Emergency: retract whatever fax is on the announcement right now, skipping the hold. "
         "Works even if r90 is down (clears the image and freezes syncing).",
)
async def _manage_takedown(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    async with _lock:
        post = await _load_post()
        if post is None or not post.message_id:
            await send_feedback(ctx, "No announcement is posted.", persist=persist)
            return
        if post.shown_kind == "cleared":
            await send_feedback(ctx, "The announcement image is already cleared.", persist=persist)
            return
        shown_kind, shown_id = post.shown_kind, post.shown_id
        try:
            status, body = await _call(
                "POST", "/bot/takedown", json_body={"shown_kind": shown_kind, "shown_id": shown_id}
            )
        except R90Unavailable as exc:
            cleared = await _clear_attachments(ctx.bot, post, freeze=True)
            await send_feedback(
                ctx,
                (f"⚠️ r90 is unreachable ({exc}). " if cleared else
                 f"🚨 r90 is unreachable ({exc}) AND clearing the image failed — delete the message by hand. ")
                + (f"Removed {_label(shown_kind, shown_id)} from the announcement and froze syncing. "
                   f"Once r90 is back: retract fax #{shown_id} in /admin, then `~manage_return 90 resume`."
                   if cleared else ""),
                persist=persist,
            )
            return
        if status == 409:
            await send_feedback(ctx, "The card is showing — nothing to take down.", persist=persist)
            return
        retracted = _json(body).get("retracted")
        try:
            summary = await _reconcile_locked(ctx.bot)
        except R90Unavailable as exc:
            summary = f"r90 went away mid-takedown ({exc})."
        post = await _load_post()
        if post is not None and (post.shown_kind, post.shown_id) == (shown_kind, shown_id):
            # The next item could not be put up, so the retracted one is still
            # visible. Clear it now; the next tick fills the slot.
            if await _clear_attachments(ctx.bot, post, freeze=False):
                summary += " Cleared the image while the next one loads."
            else:
                summary += " 🚨 Could not clear the image — delete the message by hand."
    what = f"Retracted fax #{retracted}." if retracted else "Nothing was retracted (it was no longer queued or up)."
    await send_feedback(ctx, f"✅ {what} {summary}", persist=persist)


@register_manage(
    WEEK, "resume", tier=Tier.STAFF,
    help="Unfreeze syncing after an offline takedown. Refuses if r90 still wants the taken-down fax "
         "(retract it in /admin first); `--force` overrides.",
    usage="[--force]",
)
async def _manage_resume(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    async with _lock:
        post = await _load_post()
        if post is None:
            await send_feedback(ctx, "Not set up.", persist=persist)
            return
        try:
            state = await _fetch_state()
        except R90Unavailable as exc:
            await send_feedback(ctx, f"r90 is still unreachable ({exc}); staying frozen.", persist=persist)
            return
        w = state["wanted"]
        if (post.shown_kind == "cleared" and w.get("kind") == "sub" and w.get("id") == post.shown_id
                and "--force" not in args):
            await send_feedback(
                ctx,
                f"r90 still wants fax #{post.shown_id} up — the one that was taken down. Retract it in "
                "/admin first, or pass `--force` to put it back.",
                persist=persist,
            )
            return
        post.frozen = False
        await post.save(update_fields=["frozen", "updated_at"])
        try:
            summary = await _reconcile_locked(ctx.bot)
        except R90Unavailable as exc:
            summary = f"r90 went away again ({exc})."
    await send_feedback(ctx, f"✅ Unfrozen. {summary}", persist=persist)


async def _set_intake(ctx: commands.Context, paused: bool) -> None:
    persist = is_persist_context(ctx)
    try:
        status, _ = await _call("POST", "/bot/intake", json_body={"paused": paused})
    except R90Unavailable as exc:
        await send_feedback(ctx, f"r90 is unreachable ({exc}).", persist=persist)
        return
    word = "paused — new faxes are deleted unread" if paused else "open"
    await send_feedback(ctx, f"✅ Intake {word}." if status == 200 else f"r90 said HTTP {status}.",
                        persist=persist)


@register_manage(WEEK, "pause", tier=Tier.STAFF, help="Pause fax intake (arriving faxes are deleted unread).")
async def _manage_pause(ctx: commands.Context, args: list[str]) -> None:
    await _set_intake(ctx, True)


@register_manage(WEEK, "resume_intake", tier=Tier.STAFF, help="Resume fax intake.")
async def _manage_resume_intake(ctx: commands.Context, args: list[str]) -> None:
    await _set_intake(ctx, False)


@register_manage(
    WEEK, "end", tier=Tier.OPERATOR,
    help="Stop syncing for good (teardown). The announcement is left as it is.",
)
async def _manage_end(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    async with _lock:
        post, _ = await Return90Post.get_or_create(id=1)
        post.ended = True
        await post.save(update_fields=["ended", "updated_at"])
    await send_feedback(ctx, "✅ Return 90 ended; the tick is now a no-op. Continue the teardown in the "
                             "r90-bulletin stack README.", persist=persist)

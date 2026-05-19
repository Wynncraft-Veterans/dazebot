"""Week 73: collaborative story.

A single shared story is built one fragment at a time. Each contributor sees
where the story currently stands, then adds the next little bit.

Commands (all routed through ``/return 73 ...`` by the generic Returns cog):

* ``/return 73``                 — show the most recent segment (the last
                                   complete sentence + any in-progress partial)
                                   and how to add. This is the add-flow entry.
* ``/return 73 message:<text>``  — submit the next fragment.
* ``/return 73 action:full``     — ADMIN: dump the entire story (paginated).

Contribution is gated to the three event roles in ``STORY_ROLE_IDS`` (admins
and operators always pass). Rules enforced on a submission:

* A fragment is capped at **125 characters**. Any punctuation may appear
  anywhere — there is no terminating-punctuation restriction.
* No near-back-to-back additions: you can't add if you wrote either of the
  most recent two segments — two other people have to go between your turns.

Each accepted fragment is appended as a ``StorySegment`` row and awards the
author **+1** week-73 ``/score`` point (same increment path as
``/score add``). The only public output is the non-ephemeral
``"<user> has added to the return 73 story!"`` line (no story text).

Anything that reveals story text (the tail view, the submit confirmation,
the admin full dump, error messages) goes back **privately**: an ephemeral
reply for slash invocation, or a DM for the prefix form (``~return 73``),
which has no ephemeral channel — in that case the command message is also
deleted, since ``~return 73 message:<text>`` would itself spoil the story.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from cogs.events.returns import register
from lib.auth import _has_admin_perm, _is_operator
from lib.discord_utils.paginated_embed import Paginator
from orm import DiscordAccount, Score, StorySegment, WeeklyEvent

logger = logging.getLogger("dazebot.cogs.events.returns.week_73")

WEEK = 73

# Roles allowed to contribute (admins/operators always pass).
STORY_ROLE_IDS = frozenset(
    {1407078065137254563, 1407078148440592444, 1407078577450520637}
)

# Sentence terminators — used ONLY by the tail view to find the last
# complete sentence to display. Submissions are not restricted by these;
# a fragment is capped purely by MAX_LEN.
TERMINATORS = ".!?"
# Punctuation that "hugs" the preceding word when fragments are joined.
HUG_PUNCT = ",;:.!?"

MAX_LEN = 125

# Action keywords that mean "dump the whole story" (admin only).
FULL_ACTIONS = {"full", "story", "all", "view", "dump"}

_FULL_EMBED_CHUNK = 1000  # chars per page in the admin full-story view


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------


def _is_admin_or_higher(user: discord.abc.User) -> bool:
    return _is_operator(user) or _has_admin_perm(user)


def _can_contribute(user: discord.abc.User) -> bool:
    if _is_admin_or_higher(user):
        return True
    return isinstance(user, discord.Member) and any(
        r.id in STORY_ROLE_IDS for r in user.roles
    )


# ---------------------------------------------------------------------------
# Story (re)construction
# ---------------------------------------------------------------------------


def _join(prev: str, nxt: str) -> str:
    """Concatenate two fragments naturally.

    Stored fragments are pre-stripped, so a single space is inserted between
    them — except punctuation hugs the preceding word (``cat`` + ``,`` ->
    ``cat,``) and any author-supplied edge whitespace is respected.
    """
    if not prev:
        return nxt
    if prev[-1].isspace() or nxt[:1].isspace() or nxt[:1] in HUG_PUNCT:
        return prev + nxt
    return prev + " " + nxt


def _assemble(contents: list[str]) -> str:
    full = ""
    for c in contents:
        full = _join(full, c)
    return full


def _tail(full: str) -> tuple[str, str]:
    """Split ``full`` into (last complete sentence, in-progress partial).

    The complete sentence runs from just after the second-to-last terminator
    up to and including the last terminator; the partial is everything after
    the last terminator. Either may be empty.
    """
    term_idx = [i for i, c in enumerate(full) if c in TERMINATORS]
    if not term_idx:
        return "", full.strip()
    last = term_idx[-1]
    prev = term_idx[-2] if len(term_idx) >= 2 else -1
    return full[prev + 1 : last + 1].strip(), full[last + 1 :].strip()


def _chunk(text: str, size: int = _FULL_EMBED_CHUNK) -> list[str]:
    """Split ``text`` into <=``size`` pieces on word boundaries."""
    out: list[str] = []
    cur = ""
    for word in text.split(" "):
        add = word if not cur else " " + word
        if cur and len(cur) + len(add) > size:
            out.append(cur)
            cur = word
        else:
            cur += add
    if cur:
        out.append(cur)
    return out or [text[:size]]


def _validate(text: str) -> tuple[bool, str]:
    """Return ``(True, cleaned)`` or ``(False, error_message)``."""
    s = text.strip()
    if not s:
        return False, "Your addition is empty."
    if len(s) > MAX_LEN:
        return (
            False,
            f"Too long — {len(s)}/{MAX_LEN} characters. A fragment is capped "
            f"at {MAX_LEN} characters.",
        )
    return True, s


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _render_tail(full: str, *, blocked_self: bool) -> str:
    lines: list[str] = []
    if not full.strip():
        lines.append("📖 **Return 73** — the story is empty. You write the opening line!")
    else:
        complete, partial = _tail(full)
        lines.append("📖 **Return 73 — the story so far:**")
        # The previous full sentence plus the fragment being formed, shown
        # continuously the way it reads (e.g. "...this. And this in progress,").
        lines.append(f"> {_join(complete, partial)}")
        if not partial and complete:
            lines.append("*(the last sentence just ended — start the next one)*")
    lines.append("")
    lines.append(
        f"Add up to **{MAX_LEN} characters** (any punctuation allowed):\n"
        "`/return 73 message:<your text>`"
    )
    if blocked_self:
        lines.append(
            "\n⚠️ You wrote one of the last two segments — two other people "
            "have to add before you can go again."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sub-handlers
# ---------------------------------------------------------------------------


async def _segments() -> list[StorySegment]:
    return (
        await StorySegment.filter(week=WEEK)
        .order_by("created_at")
        .prefetch_related("discord_account")
    )


def _authored_recent(segs: list[StorySegment], author_id: int) -> bool:
    """True if ``author_id`` wrote either of the last two segments.

    Enforces the "two other people must go between your turns" rule. With 0
    or 1 existing segments this just checks whatever is there.
    """
    uid = str(author_id)
    return any(s.discord_account.disc_uuid == uid for s in segs[-2:])


async def _private(
    ctx: commands.Context,
    content: Optional[str] = None,
    *,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
) -> None:
    """Send a response only the invoking user should see.

    Slash invocation -> ephemeral reply. A prefix invocation (``~return 73``)
    has no ephemeral channel, and posting the story tail / fragment text
    publicly would spoil it — so DM the user instead and delete the
    (possibly spoiler-bearing) command message. Falls back to a non-spoiler
    public nudge if the user's DMs are closed.
    """
    kw: dict = {}
    if content is not None:
        kw["content"] = content
    if embed is not None:
        kw["embed"] = embed
    if view is not None:
        kw["view"] = view

    if ctx.interaction is not None:
        await ctx.reply(ephemeral=True, **kw)
        return

    try:
        await ctx.author.send(**kw)
    except discord.Forbidden:
        await ctx.reply(
            "I couldn't DM you. Open your DMs or use the **slash** command "
            "`/return 73` — posting this here would spoil the story.",
            mention_author=False,
        )
        return
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound, AttributeError):
        pass


async def _show_tail(ctx: commands.Context) -> None:
    if not _can_contribute(ctx.author):
        await _private(
            ctx,
            "You don't have a role that can take part in the Return 73 story.",
        )
        return
    segs = await _segments()
    full = _assemble([s.content for s in segs])
    await _private(
        ctx, _render_tail(full, blocked_self=_authored_recent(segs, ctx.author.id))
    )


async def _submit(ctx: commands.Context, message: str) -> None:
    if not _can_contribute(ctx.author):
        await _private(
            ctx,
            "You don't have a role that can take part in the Return 73 story.",
        )
        return

    ok, result = _validate(message)
    if not ok:
        await _private(ctx, result)
        return

    segs = await _segments()
    if _authored_recent(segs, ctx.author.id):
        await _private(
            ctx,
            "You wrote one of the last two segments — wait for two other "
            "people to add before you contribute again.",
        )
        return

    disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(ctx.author.id))
    await StorySegment.create(week=WEEK, discord_account=disc, content=result)

    # +1 week-73 score — same increment path as /score add.
    event, _ = await WeeklyEvent.get_or_create(week=WEEK)
    score_obj, created = await Score.get_or_create(
        event=event, discord_account=disc, defaults={"score": 1}
    )
    if not created:
        score_obj.score += 1
        await score_obj.save(update_fields=["score"])

    await ctx.send(
        f"{ctx.author.mention} has added to the return 73 story!",
        allowed_mentions=discord.AllowedMentions.none(),
    )

    full = _join(_assemble([s.content for s in segs]), result)
    complete, partial = _tail(full)
    confirm = ["✅ Added to the story (+1 point for week 73)."]
    if partial:
        confirm.append(f"**In progress:** {partial}")
    elif complete:
        confirm.append(f"…{complete}")
    await _private(ctx, "\n".join(confirm))


async def _show_full(ctx: commands.Context) -> None:
    if not _is_admin_or_higher(ctx.author):
        await _private(ctx, "Administrator is required to view the entire story.")
        return

    if ctx.interaction is not None:
        await ctx.defer(ephemeral=True)
    segs = await StorySegment.filter(week=WEEK).order_by("created_at")
    full = _assemble([s.content for s in segs])
    if not full.strip():
        await _private(ctx, "The Return 73 story is empty.")
        return

    chunks = _chunk(full)
    total = len(chunks)
    embeds = []
    for i, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(title="Return 73 — Full Story", description=chunk)
        embed.set_footer(text=f"Page {i}/{total} · {len(segs)} segment(s)")
        embeds.append(embed)

    if total == 1:
        await _private(ctx, embed=embeds[0])
    else:
        await _private(ctx, embed=embeds[0], view=Paginator(embeds))


# ---------------------------------------------------------------------------
# Dispatch entry-point
# ---------------------------------------------------------------------------


@register(73)
async def handle(
    ctx: commands.Context,
    *,
    flag: bool = False,
    action: Optional[str] = None,
    cult: Optional[str] = None,
    owner: Optional[str] = None,
    target: Optional[discord.Member] = None,
    message: Optional[str] = None,
    **kwargs,
) -> None:
    if action is not None and action.lower() in FULL_ACTIONS:
        await _show_full(ctx)
        return
    if message:
        await _submit(ctx, message)
        return
    await _show_tail(ctx)

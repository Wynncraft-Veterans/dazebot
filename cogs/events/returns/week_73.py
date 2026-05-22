"""Week 73: collaborative story.

A single shared story is built one fragment at a time. Each contributor sees
the most recent segment, then clicks a button to add the next little bit.

Surfaces:

* ``/return 73``                       — show the most recent segment in
                                         an ephemeral reply with an **Add
                                         to the story** button. The button
                                         opens a Discord Modal where the
                                         fragment is typed; the text never
                                         appears in the channel UI.
* ``~manage_return 73 showMessage``    — ADMIN: dump the entire story
                                         (paginated). Replaces the legacy
                                         ``/return 73 action:full`` slash
                                         path, which is gone with the
                                         shared-param overhaul.

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
reply for slash invocation, or a DM for the prefix form (``~return 73``).
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from cogs.events.returns import Tier, register, register_manage
from lib.auth import _has_admin_perm, _is_operator
from lib.discord_utils.paginated_embed import Paginator
from orm import DiscordAccount, Score, StorySegment, WeeklyEvent

logger = logging.getLogger("dazebot.cogs.events.returns.week_73")

WEEK = 73

# Roles allowed to contribute (admins/operators always pass).
STORY_ROLE_IDS = frozenset(
    {1407078065137254563, 1407078148440592444, 1407078577450520637}
)

# Punctuation that "hugs" the preceding word when fragments are joined.
HUG_PUNCT = ",;:.!?"

MAX_LEN = 125

# How many trailing segments the tail view (and post-submit confirmation) show.
TAIL_SEGMENTS = 1

_FULL_EMBED_CHUNK = 1000  # chars per page in the admin full-story view

# How long the ephemeral tail view's button stays clickable. Leaves a few
# minutes of buffer before the ephemeral interaction token (~15 min) expires.
_VIEW_TIMEOUT = 600


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


def _render_tail(contents: list[str], *, blocked_self: bool) -> str:
    lines: list[str] = []
    if not contents:
        lines.append(
            "📖 **Return 73** — the story is empty. You'd be writing the opening line!"
        )
    elif len(contents) <= TAIL_SEGMENTS:
        lines.append("📖 **Return 73 — the story so far:**")
        lines.append(f"> {_assemble(contents[-TAIL_SEGMENTS:])}")
    else:
        header = (
            "📖 **Return 73 — the most recent segment:**"
            if TAIL_SEGMENTS == 1
            else f"📖 **Return 73 — the last {TAIL_SEGMENTS} segments:**"
        )
        lines.append(header)
        lines.append(f"> {_assemble(contents[-TAIL_SEGMENTS:])}")
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


# ---------------------------------------------------------------------------
# Button + Modal (keeps fragment text out of channel UI entirely)
# ---------------------------------------------------------------------------


async def _persist_segment(user: discord.abc.User, text: str) -> None:
    """Append a fragment and award the +1 week-73 score point."""
    disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(user.id))
    await StorySegment.create(week=WEEK, discord_account=disc, content=text)
    event, _ = await WeeklyEvent.get_or_create(week=WEEK)
    score_obj, created = await Score.get_or_create(
        event=event, discord_account=disc, defaults={"score": 1}
    )
    if not created:
        score_obj.score += 1
        await score_obj.save(update_fields=["score"])


class _StoryContributeModal(discord.ui.Modal):
    """Discord modal for typing the next fragment. Never visible in channel."""

    fragment = discord.ui.TextInput(
        label="The next bit of the story",
        placeholder="Pick up where the last segment left off.",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=MAX_LEN,
        required=True,
    )

    def __init__(self, *, public_channel_id: int):
        super().__init__(title="Return 73 — add to the story")
        self.public_channel_id = public_channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not _can_contribute(interaction.user):
            await interaction.followup.send(
                "You don't have a role that can take part in the Return 73 story.",
                ephemeral=True,
            )
            return

        ok, result = _validate(str(self.fragment.value))
        if not ok:
            await interaction.followup.send(result, ephemeral=True)
            return

        segs = await _segments()
        if _authored_recent(segs, interaction.user.id):
            await interaction.followup.send(
                "You wrote one of the last two segments — wait for two other "
                "people to add before you contribute again.",
                ephemeral=True,
            )
            return

        await _persist_segment(interaction.user, result)

        channel = interaction.client.get_channel(self.public_channel_id)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(self.public_channel_id)
            except discord.HTTPException:
                logger.exception(
                    "could not fetch public channel %s", self.public_channel_id
                )
                channel = None
        if isinstance(channel, discord.abc.Messageable):
            await channel.send(
                f"{interaction.user.mention} has added to the return 73 story!",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        prev = segs[-(TAIL_SEGMENTS - 1):] if TAIL_SEGMENTS > 1 else []
        tail_contents = [s.content for s in prev] + [result]
        await interaction.followup.send(
            "\n".join(
                [
                    "✅ Added to the story (+1 point for week 73).",
                    f"…{_assemble(tail_contents)}",
                ]
            ),
            ephemeral=True,
        )


class _StoryView(discord.ui.View):
    """One-button view: opens the modal. Ephemeral / DM-bound, not persistent."""

    def __init__(self, *, blocked_self: bool, public_channel_id: int):
        super().__init__(timeout=_VIEW_TIMEOUT)
        self.public_channel_id = public_channel_id
        btn = discord.ui.Button(
            label=(
                "Wait — you wrote one of the last two"
                if blocked_self
                else "✍️ Add to the story"
            ),
            style=discord.ButtonStyle.primary,
            disabled=blocked_self,
        )
        btn.callback = self._on_click
        self.add_item(btn)

    async def _on_click(self, interaction: discord.Interaction) -> None:
        if not _can_contribute(interaction.user):
            await interaction.response.send_message(
                "You don't have a role that can take part in the Return 73 story.",
                ephemeral=True,
            )
            return
        segs = await _segments()
        if _authored_recent(segs, interaction.user.id):
            await interaction.response.send_message(
                "You wrote one of the last two segments — wait for two other "
                "people to add before you contribute again.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            _StoryContributeModal(public_channel_id=self.public_channel_id)
        )


async def _show_tail(ctx: commands.Context) -> None:
    if not _can_contribute(ctx.author):
        await _private(
            ctx,
            "You don't have a role that can take part in the Return 73 story.",
        )
        return
    segs = await _segments()
    blocked = _authored_recent(segs, ctx.author.id)
    body = _render_tail([s.content for s in segs], blocked_self=blocked)
    view = _StoryView(blocked_self=blocked, public_channel_id=ctx.channel.id)
    await _private(ctx, body, view=view)


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


@register(73, custom_check=_can_contribute)
async def handle(ctx: commands.Context) -> None:
    await _show_tail(ctx)


@register_manage(
    73, "showMessage", tier=Tier.ADMIN,
    help="Dump the entire Return 73 story (paginated).",
    usage="",
)
async def _manage_show_message(ctx: commands.Context, args: list[str]) -> None:
    # _show_full handles its own admin gate, ephemeral/DM routing, and pagination.
    await _show_full(ctx)

"""Week 81: team photo bingo.

Flow
----
1. A caller with the Hiatus/Honourary/Member role runs ``/return 81``. The
   bot posts a "Invite teammates" button in the caller's channel.
2. Clicking the button opens ``_InviteModal`` — two text inputs for the
   Discord names/mentions of the two invitees. On submit the bot posts an
   invite message tagging both invitees in :data:`INVITE_THREAD_ID`, with
   Accept/Decline buttons for each. Team goes into the ``pending`` state
   with two ``BingoInvite`` rows.
3. Once *both* invitees Accept, the team enters ``picking``: the bot
   creates a private thread ``r81-Team-<n>`` under
   :data:`TEAMS_PARENT_CHANNEL_ID`, adds all three current members, and
   posts a picker message with 10 buttons — each a candidate drawn at
   random from Wynncraft-guild-``Returners`` accounts linked to Discord.
4. First click on a picker button resolves the wildcard: the picked
   Discord user is added to the thread and made a team member. The bot
   seeds the default-4x4's 16 ``BingoCellState`` captions and pins the
   5-message dashboard (status grid + four 2x2 embeds). Team goes into
   ``playing``.
5. Team members post ``~return 81 submit <cell>`` (e.g.
   ``~return 81 submit A1``) with a photo attachment in the team thread.
   The attachment is what gets submitted; the ``on_message`` listener
   validates + upserts a ``BingoSubmission``, edits the relevant embed to
   show the photo, edits the status post to mark the cell filled, awards
   ``POINTS_PER_CELL`` via ``WeeklyEvent(week=81)`` / ``Score`` to every
   current team member, and detects any newly-completed bingo lines. Each
   new line awards ``POINTS_PER_BINGO_LINE`` to every teammate and
   auto-opens an extra-teammate picker; when someone is picked they
   join AND the board expands by one 2x2 slab (see ``board_dims`` for
   the growth rule). The ``handle`` dispatcher guards against firing the
   invite flow when the same ``~return 81 submit <cell>`` shape shows up
   as a prefix command — the listener owns that path.

Staff surfaces live under ``~manage_return 81 <subcommand>``. See the
``@register_manage`` decorators near the bottom.

Only Discord CDN URLs are stored (see :func:`_on_message_submit`), matching
``cogs/rewards/donations/donations.py``'s approach. There is no re-upload
step; the source message must not be deleted for the embed to keep
rendering.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Optional

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont
from tortoise.exceptions import OperationalError

from cogs.events.returns import (
    Tier,
    register,
    register_manage,
    register_tick,
)
from cogs.events.returns._common import (
    is_persist_context,
    send_feedback,
    tier_allows,
)
from cogs.events.returns.lib import resolver as _resolver
from config import CurrConfig
from lib.auth import _resolve_member
from orm import DiscordAccount, MinecraftAccount, Score, WeeklyEvent
from orm_returns import (
    BingoBingoEvent,
    BingoCellState,
    BingoInvite,
    BingoSubmission,
    BingoTeam,
    BingoTeamMember,
)

logger = logging.getLogger("dazebot.cogs.events.returns.week_81")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


WEEK = 81

# The thread (inside channel 1320597026118828112) that hosts the "please
# confirm the invite" posts. The user creates this manually and pastes the
# id here on deploy. 0 = not-yet-configured; the modal fails loudly with a
# staff-facing error rather than dropping teams on the floor.
INVITE_THREAD_ID = 1526103277308084265

# The parent text channel under which per-team private threads are created.
TEAMS_PARENT_CHANNEL_ID = 1313769181321236493

# Wynncraft in-game guild name the random-pool draw scans.
WYNN_GUILD_NAME = "Returners"

# The set of roles a user must hold to be eligible as caller/invitee/wildcard.
# Waitlisted and Registered-only users are intentionally excluded — the
# spec at .claude/ephemeral/return-81.md is explicit that only "hiatus,
# honourary, or member" qualify.
TEAM_ROLE_IDS = frozenset(
    {
        CurrConfig.ROLE_HIATUS,
        CurrConfig.ROLE_HONOURARY,
        CurrConfig.ROLE_MEMBER,
    }
)

# Score awarded per team member per accepted cell submission.
POINTS_PER_CELL = 1

# Number of candidates presented in the wildcard picker.
PICKER_SIZE = 10

# Smaller shortlist size for the post-bingo "extra teammate" bonus. Same
# picker view (BingoRandomPickView has 10 buttons; surplus ones get
# disabled at render), just fewer options to choose from.
EXTRA_MEMBER_PICKER_SIZE = 6

# Board geometry is a pure function of ``BingoTeam.expansion_count``.
# Expansion 0 = the default 4x4 board (4 pinned 2x2 posts).
#
# Rule: odd expansions add two columns to every existing row-slab; even
# expansions (> 0) add two rows spanning every current col-slab. Each
# expansion's new cells are grouped into fresh 2x2 posts appended to the
# team's pinned dashboard. See ``board_dims`` for the exact dims formula.


@functools.lru_cache(maxsize=None)
def board_dims(exp: int) -> tuple[int, int]:
    """Return ``(rows, cols)`` at the given expansion count.

    exp=0 → (4, 4). Each odd exp adds 2 cols; each even exp > 0 adds 2 rows.
    So exp=1 → (4, 6), exp=2 → (6, 6), exp=3 → (6, 8), exp=4 → (8, 8), …
    """
    rows = 4 + 2 * (exp // 2)
    cols = 4 + 2 * ((exp + 1) // 2)
    return rows, cols


def _team_exp(team: BingoTeam) -> int:
    """``team.expansion_count`` with a defensive default.

    If a stale schema slips past both the aerich upgrade AND the startup
    schema-heal in ``orm._ensure_r81_schema`` (e.g. container hot-reloaded
    code but never re-ran ``init_db``), Tortoise leaves the field off the
    hydrated instance and every read crashes. Fall back to 0 so the code
    stays useful until the next boot fixes the schema for real.
    """
    return getattr(team, "expansion_count", 0) or 0


def row_label(idx: int) -> str:
    """0→"A", 25→"Z", 26→"AA", 27→"AB", … (spreadsheet-style base-26).

    Practically the board rarely grows past Z (that would need a team of
    ~30), but the caller doesn't have to think about it.
    """
    letters = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def cell_name(row_idx: int, col_idx: int) -> str:
    return f"{row_label(row_idx)}{col_idx + 1}"


@functools.lru_cache(maxsize=None)
def bingo_cells(exp: int) -> tuple[str, ...]:
    """Row-major flat tuple of every cell name at this expansion."""
    rows, cols = board_dims(exp)
    return tuple(
        cell_name(r, c) for r in range(rows) for c in range(cols)
    )


@functools.lru_cache(maxsize=None)
def bingo_lines(exp: int) -> tuple[tuple[str, ...], ...]:
    """All winning lines at this expansion — full rows, full cols, and the
    two main diagonals anchored at the top corners (length min(rows, cols)).
    """
    rows, cols = board_dims(exp)
    lines: list[tuple[str, ...]] = []
    for r in range(rows):
        lines.append(tuple(cell_name(r, c) for c in range(cols)))
    for c in range(cols):
        lines.append(tuple(cell_name(r, c) for r in range(rows)))
    diag_len = min(rows, cols)
    lines.append(tuple(cell_name(i, i) for i in range(diag_len)))
    lines.append(tuple(cell_name(i, cols - 1 - i) for i in range(diag_len)))
    return tuple(lines)


@functools.lru_cache(maxsize=None)
def post_layouts(exp: int) -> tuple[tuple[tuple[str, str], tuple[str, str]], ...]:
    """Ordered list of every 2x2 pinned-post layout up through ``exp``.

    Index i in this list corresponds to ``BingoTeam.embed_msg_ids_json[i]``.
    Base 4x4 contributes 4 posts; each subsequent expansion appends the
    ``new_post_layouts_for(n)`` slab. This function's ordering is the
    source of truth for post index ↔ layout mapping.
    """
    layouts: list[tuple[tuple[str, str], tuple[str, str]]] = []
    for r in (0, 2):
        for c in (0, 2):
            layouts.append(_quadrant(r, c))
    for n in range(1, exp + 1):
        layouts.extend(new_post_layouts_for(n))
    return tuple(layouts)


@functools.lru_cache(maxsize=None)
def new_post_layouts_for(
    exp: int,
) -> tuple[tuple[tuple[str, str], tuple[str, str]], ...]:
    """The 2x2 layouts that expansion ``exp`` (≥ 1) contributes on top of
    ``exp - 1``. Empty for ``exp <= 0``.
    """
    if exp <= 0:
        return ()
    prev_rows, prev_cols = board_dims(exp - 1)
    _, new_cols = board_dims(exp)
    layouts: list[tuple[tuple[str, str], tuple[str, str]]] = []
    if new_cols > prev_cols:
        # Odd expansion: add cols to every existing row-slab.
        for r in range(0, prev_rows, 2):
            layouts.append(_quadrant(r, prev_cols))
    else:
        # Even expansion: add rows spanning every col-slab of the new width.
        for c in range(0, new_cols, 2):
            layouts.append(_quadrant(prev_rows, c))
    return tuple(layouts)


def _quadrant(
    r0: int, c0: int
) -> tuple[tuple[str, str], tuple[str, str]]:
    return (
        (cell_name(r0, c0), cell_name(r0, c0 + 1)),
        (cell_name(r0 + 1, c0), cell_name(r0 + 1, c0 + 1)),
    )


@functools.lru_cache(maxsize=None)
def cell_to_post_index(exp: int) -> dict[str, int]:
    """Reverse index: cell name → 1-based post index at this expansion."""
    return {
        cell: i + 1
        for i, layout in enumerate(post_layouts(exp))
        for row in layout
        for cell in row
    }

# Placeholder image render settings. We generate cell PNGs locally via
# Pillow because placehold.co (and every similar free service) truncates
# long captions — a 512x512 canvas can hold ~200 chars, but the services
# clip at ~80. Pillow is only used for placeholder text; user submissions
# stay as their raw Discord CDN URLs. Total Pillow use per team grows
# linearly with expansion count (16 cells + ~8-12 per expansion, once at
# seed plus per rerender for placeholder cells), so the CPU cost on a
# weak VPS stays negligible.
_PLACEHOLDER_PX = 512
_PLACEHOLDER_BG = (30, 34, 48)  # dark navy
_PLACEHOLDER_FG = (240, 240, 240)  # off-white
_PLACEHOLDER_LABEL_SIZE = 72
_PLACEHOLDER_CAPTION_SIZE = 28
_PLACEHOLDER_MARGIN = 32  # px padding on each side

# TrueType font search order. First hit wins. fonts-dejavu-core (installed
# via the vets-deploy Dockerfile) provides the Linux path; Windows dev has
# Arial. If none are found we fall back to Pillow's bundled default which
# is legible in Pillow 10+ (the size argument routes through a scalable
# vector font, not the tiny 8px bitmap of older releases).
_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)

# Arbitrary anchor URL: 4 embeds in one message that share the same ``url``
# collapse into a 2x2 gallery in the Discord client — that's how we get
# four independent images inside "one embed" without an image library.
# The URL doesn't need to resolve; it's just an equality key.
_GROUPING_URL_PREFIX = "https://wynnvets.org/r81/team"

# Timeout for the ephemeral "click to invite" button that /return 81 posts.
# The persistent invite/pick/bonus views use timeout=None.
_INVITE_STARTER_TIMEOUT = 600

# Regex for the in-thread submission command. Case-insensitive on the cell.
# Shape: ``~return 81 submit <cell>`` in a message with an attachment. The
# attachment IS the photo. The ``submit`` verb disambiguates from the
# ``/return 81`` entry-point without needing extra guards. Also used by
# ``handle`` to noop the dispatcher when a submission is inbound, so both
# sides look at the same pattern.
_SUBMIT_RE = re.compile(
    r"^\s*~return\s+81\s+submit\s+([A-Za-z]{1,4}\d{1,3})\s*$",
    flags=re.IGNORECASE,
)

# What the bot tells users when they need a reminder of the command shape.
# Threaded through every dashboard surface so a team never has to hunt for
# it.
SUBMIT_COMMAND_HINT = (
    "To submit a photo, post it as an attachment on a message reading "
    "`~return 81 submit <cell>` (e.g. `~return 81 submit A1`)."
)

# How many `%player%` slots to pre-fill in the resolver call. Main.yaml
# templates cap out at ~3 slots; oversizing to 10 gives the resolver plenty
# of headroom without a per-template count.
_PLAYERS_POOL_SIZE = 10


# ---------------------------------------------------------------------------
# Eligibility + resolution
# ---------------------------------------------------------------------------


def _has_team_role(member: Optional[discord.Member]) -> bool:
    if member is None:
        return False
    return any(r.id in TEAM_ROLE_IDS for r in member.roles)


def _can_invite(user: discord.abc.User, client: Optional[discord.Client]) -> bool:
    """``custom_check`` for :func:`register`: only H/H/M holders qualify."""
    return _has_team_role(_resolve_member(user, client))


_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d+)>$")
_DISCORD_ID_RE = re.compile(r"^\d{15,20}$")


async def _resolve_invitee(
    client: discord.Client, raw: str, guild_id: int
) -> tuple[Optional[discord.Member], Optional[str]]:
    """Resolve a raw invitee string to a guild :class:`discord.Member`.

    Accepts a mention, snowflake id, username, global name, or display
    name. Returns ``(member, error_message_or_None)``. Never falls back to
    MC lookup — r81 invitees are always Discord users, and we want the
    error to say so rather than mysteriously succeeding with a random MC
    account that happens to match a nickname.
    """
    val = raw.strip()
    if not val:
        return None, "Invitee is required — leave nothing blank."
    guild = client.get_guild(guild_id)
    if guild is None:
        return None, (
            "I can't reach the server right now. Try again in a moment."
        )

    disc_id: Optional[str] = None
    m = _DISCORD_MENTION_RE.match(val)
    if m:
        disc_id = m.group(1)
    elif _DISCORD_ID_RE.match(val):
        disc_id = val

    if disc_id is not None:
        member = guild.get_member(int(disc_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(disc_id))
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is None:
            return None, (
                f"`{disc_id}` isn't a member of this server."
            )
        return member, None

    # Users often type `@name` as literal text — they meant to pick from
    # Discord's mention popup but hit enter/space first, so the `@` never
    # became part of a real `<@id>` mention. Strip it before name matching
    # so `@faulischlumpf` resolves the same as `faulischlumpf`.
    had_at_prefix = val.startswith("@")
    if had_at_prefix:
        val = val[1:].strip()

    lower = val.lower()
    for cand in guild.members:
        if (
            cand.name.lower() == lower
            or (cand.global_name or "").lower() == lower
            or cand.display_name.lower() == lower
        ):
            return cand, None

    at_hint = (
        " Note: a literal `@` prefix only counts as a mention if you"
        " pick the user from Discord's popup — otherwise it's just text."
        if had_at_prefix
        else ""
    )
    return None, (
        f"`{raw.strip()}` doesn't match anyone on this server."
        " Try picking them from Discord's @mention popup, pasting their"
        f" user ID, or typing their exact username.{at_hint}"
    )


async def _current_team_for(disc_uuid: str) -> Optional[BingoTeam]:
    """Return the team the given Discord user is currently on (any non-
    ``disbanded`` state), or ``None``. Both accepted-invite members and
    the creator count."""
    member_row = (
        await BingoTeamMember.filter(disc_uuid=disc_uuid)
        .prefetch_related("team")
        .first()
    )
    if member_row is not None:
        team = member_row.team
        if team.state != "disbanded":
            return team
    # Also check: creator with team still ``pending`` (no BingoTeamMember
    # rows exist yet — those get inserted when invites resolve).
    pending = await BingoTeam.filter(
        creator_disc_uuid=disc_uuid, state="pending"
    ).first()
    if pending is not None:
        return pending
    # Also check: pending invites out to this user (they haven't accepted
    # yet, but they can't be double-invited or start their own team).
    pending_invite = (
        await BingoInvite.filter(invitee_disc_uuid=disc_uuid, state="pending")
        .prefetch_related("team")
        .first()
    )
    if pending_invite is not None and pending_invite.team.state != "disbanded":
        return pending_invite.team
    return None


# ---------------------------------------------------------------------------
# Random pool
# ---------------------------------------------------------------------------


async def _wynn_guild_candidates(
    client: discord.Client, exclude_disc_uuids: set[str]
) -> list[tuple[discord.Member, str]]:
    """Draw up to :data:`PICKER_SIZE` (Discord Member, MC username) pairs
    eligible for the r81 wildcard slot.

    Pool: Wynncraft in-game guild ``Returners`` MC accounts that are linked
    to a Discord account whose guild member holds one of :data:`TEAM_ROLE_IDS`.
    Excludes any user already on any (non-disbanded) r81 team. Returning
    the MC username alongside the member lets the picker post show both
    (Discord chip + in-game name) without a second DB round trip.
    """
    guild = client.get_guild(CurrConfig.GUILD)
    if guild is None:
        return []

    mc_rows = await MinecraftAccount.filter(guild=WYNN_GUILD_NAME).all()
    if not mc_rows:
        return []
    mc_username_by_id = {mc.id: mc.mc_username for mc in mc_rows}

    excluded_from_teams = set(
        await BingoTeamMember.filter().values_list("disc_uuid", flat=True)
    )
    excluded = exclude_disc_uuids | excluded_from_teams

    # Walk DiscordAccount → MinecraftAccount rather than the reverse: the
    # reverse relation on ``MinecraftAccount`` is a queryset, not a single
    # object, and OneToOne reverse traversal here needs a separate query
    # anyway.
    discs = await DiscordAccount.filter(
        minecraft_account_id__in=list(mc_username_by_id.keys())
    )

    candidates: list[tuple[discord.Member, str]] = []
    seen: set[int] = set()
    for d in discs:
        if d.disc_uuid in excluded:
            continue
        try:
            disc_id = int(d.disc_uuid)
        except ValueError:
            continue
        if disc_id in seen:
            continue
        member = guild.get_member(disc_id)
        if member is None or not _has_team_role(member):
            continue
        mc_name = mc_username_by_id.get(d.minecraft_account_id)
        if not mc_name:
            continue
        seen.add(disc_id)
        candidates.append((member, mc_name))

    random.shuffle(candidates)
    return candidates[:PICKER_SIZE]


# ---------------------------------------------------------------------------
# Bingo detection
# ---------------------------------------------------------------------------


def _line_key(line: tuple[str, ...]) -> str:
    return "|".join(line)


def _detect_new_bingos(
    filled_before: set[str], filled_after: set[str], exp: int
) -> list[tuple[str, ...]]:
    new: list[tuple[str, ...]] = []
    for line in bingo_lines(exp):
        cells = set(line)
        if cells <= filled_after and not (cells <= filled_before):
            new.append(line)
    return new


# ---------------------------------------------------------------------------
# Caption + image stubs
# ---------------------------------------------------------------------------


_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try ``_FONT_PATHS`` in order; cache per size; fall back to Pillow's
    bundled default (which in Pillow 10+ is a scalable TrueType that
    accepts a ``size`` argument — legible, unlike the tiny bitmap of older
    releases).
    """
    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached
    for path in _FONT_PATHS:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                _FONT_CACHE[size] = font
                return font
            except OSError:
                continue
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10 doesn't accept size on load_default
        font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def _wrap_to_width(
    text: str, font, max_width: int
) -> list[str]:
    """Greedy word wrap using pixel measurements from ``font``.

    Never splits a word; if a single word is wider than ``max_width`` the
    caller accepts it overflowing rather than mangling it (unlikely with
    natural-language captions).
    """
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = " ".join(current + [word]) if current else word
        # font.getlength is Pillow 9.2+; falls back to getbbox on older.
        try:
            width = font.getlength(trial)
        except AttributeError:
            width = font.getbbox(trial)[2]
        if width <= max_width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _render_cell_png(cell: str, caption: str) -> bytes:
    """Render a single 512x512 PNG with the cell label + wrapped caption.

    Layout: label anchored at the top, caption wrapped and vertically
    centred in the remaining space. Uses solid-fill background because
    that's what a placeholder should look like — the user's photo takes
    over on submission.
    """
    W = _PLACEHOLDER_PX
    canvas = Image.new("RGB", (W, W), _PLACEHOLDER_BG)
    draw = ImageDraw.Draw(canvas)

    label_font = _load_font(_PLACEHOLDER_LABEL_SIZE)
    caption_font = _load_font(_PLACEHOLDER_CAPTION_SIZE)

    # Label: horizontally centred, near the top.
    draw.text(
        (W // 2, _PLACEHOLDER_MARGIN + _PLACEHOLDER_LABEL_SIZE // 2),
        cell,
        fill=_PLACEHOLDER_FG,
        font=label_font,
        anchor="mm",
    )

    # Caption: wrap to inner width, vertically centre in the remaining band.
    inner_w = W - 2 * _PLACEHOLDER_MARGIN
    lines = _wrap_to_width(caption, caption_font, inner_w)
    line_h = int(_PLACEHOLDER_CAPTION_SIZE * 1.25)
    caption_band_top = _PLACEHOLDER_MARGIN + _PLACEHOLDER_LABEL_SIZE + 24
    caption_band_bottom = W - _PLACEHOLDER_MARGIN
    band_h = caption_band_bottom - caption_band_top
    total_text_h = line_h * len(lines)
    y = caption_band_top + max(0, (band_h - total_text_h) // 2) + line_h // 2

    for line in lines:
        draw.text(
            (W // 2, y),
            line,
            fill=_PLACEHOLDER_FG,
            font=caption_font,
            anchor="mm",
        )
        y += line_h

    buf = BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# In-memory name override for `~manage_return 81 fakeTest` teams. Keyed by
# str(team.id) so we don't have to import ``uuid`` just for a type hint.
# Cleared on restart — test teams are ephemeral by design, so
# restart-persistence isn't worth a schema field.
_FAKE_TEAM_NAMES: dict[str, list[str]] = {}


async def _team_player_names(team: BingoTeam) -> list[str]:
    """MC usernames of every current team member, in join order.

    Each ``%player%`` slot in a resolved caption is one of the actual
    Wynncraft names of the people in the team thread — so a card reads
    like a prompt that names the participants. Members whose Discord
    account has no linked MinecraftAccount fall back to
    ``"a teammate"``; in practice that's rare because H/H/M eligibility
    usually implies the auth-stack flow ran, but staff ``forceAdd`` can
    bypass linking.

    :data:`_FAKE_TEAM_NAMES` short-circuits the DB lookup for test teams
    created via ``~manage_return 81 fakeTest`` so the renderer can be
    exercised with a canned name list.
    """
    override = _FAKE_TEAM_NAMES.get(str(team.id))
    if override is not None:
        return list(override)
    members = await BingoTeamMember.filter(team=team)
    names: list[str] = []
    for m in members:
        disc = await DiscordAccount.filter(disc_uuid=m.disc_uuid).first()
        if disc is not None and disc.minecraft_account_id is not None:
            mc = await MinecraftAccount.get_or_none(id=disc.minecraft_account_id)
            if mc is not None and mc.mc_username:
                names.append(mc.mc_username)
                continue
        names.append("a teammate")
    return names


def _make_players_pool(names: list[str], k: int = _PLAYERS_POOL_SIZE) -> list[str]:
    """Build a k-element pool for the resolver by cycling shuffled batches.

    ``%player%`` gets popped sequentially, and templates use up to ~3
    slots. Shuffling per-batch (rather than ``random.choices`` with
    replacement) means every team member appears before any repeats —
    keeps templates with multiple ``%player%`` slots from picking the
    same person twice in a row when the team has 4+ members. We also
    swap out an immediate-repeat at each batch boundary to avoid
    "Alice giving a gift to Alice".
    """
    if not names:
        return ["a teammate"] * k
    pool: list[str] = []
    while len(pool) < k:
        batch = list(names)
        random.shuffle(batch)
        if pool and batch and pool[-1] == batch[0] and len(batch) > 1:
            batch[0], batch[1] = batch[1], batch[0]
        pool.extend(batch)
    return pool[:k]


async def _generate_cell_caption(team: BingoTeam) -> str:
    """Resolve a fresh caption for one cell, prefixed with ``"Post a picture of "``.

    Templates come from ``resources/main.yaml`` (via
    :mod:`cogs.events.returns.lib.resolver`), which all lead with
    ``%player%`` → a real MC username of one of the team members in the
    thread. Simple concatenation after "Post a picture of " reads
    naturally because ``%player%`` resolves to a proper noun. On
    resolver failure we fall back to a stable placeholder so a bad
    template can't break card seeding.
    """
    names = await _team_player_names(team)
    pool = _make_players_pool(names)
    try:
        body = await _resolver.resolve(players=pool)
    except Exception:
        logger.exception("r81 resolver failed for team %s; using fallback", team.id)
        body = "your team doing something photo-worthy in Wynncraft."
    return f"Post a picture of {body}"


# ---------------------------------------------------------------------------
# Thread helpers
# ---------------------------------------------------------------------------


async def _resolve_thread(
    bot: discord.Client, thread_id: Optional[int]
) -> Optional[discord.Thread]:
    if not thread_id:
        return None
    ch = bot.get_channel(thread_id)
    if isinstance(ch, discord.Thread):
        return ch
    try:
        ch = await bot.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logger.warning("r81 thread %s not fetchable: %s", thread_id, e)
        return None
    return ch if isinstance(ch, discord.Thread) else None


# ---------------------------------------------------------------------------
# Dashboard rendering
# ---------------------------------------------------------------------------


def _status_text(filled: set[str], exp: int) -> str:
    """Render the pinned status grid + header.

    Cell tokens are padded to a per-row width so the grid stays aligned
    in monospace regardless of how many chars the widest cell name uses
    (``[A1]`` vs ``[AA100]``). Filled cells render as a centred check
    within the same width; do NOT collapse the padding — a future dev
    "cleaning up" the spacing will break alignment on wider boards.
    """
    rows, cols = board_dims(exp)
    total_cells = rows * cols
    max_name_w = max(len(cell_name(r, c)) for r in range(rows) for c in range(cols))
    box_inner_w = max_name_w  # width inside the brackets
    fill_token = "✓".center(box_inner_w)
    lines = []
    for r in range(rows):
        parts = []
        for c in range(cols):
            key = cell_name(r, c)
            token = fill_token if key in filled else key.ljust(box_inner_w)
            parts.append(f"[{token}]")
        lines.append("".join(parts))
    header = (
        f"**Return 81 — Team Bingo**  ·  Board `{rows}×{cols}`"
        f" (expansion {exp})  ·  `{len(filled)}/{total_cells}` cells complete"
    )
    return (
        header
        + "\n```\n" + "\n".join(lines) + "\n```\n"
        + SUBMIT_COMMAND_HINT
    )


def _embeds_and_files_for_post(
    post_index: int,
    *,
    layout: tuple[tuple[str, str], tuple[str, str]],
    team_number: int,
    captions: dict[str, str],
    submissions: dict[str, str],
    version: int,
) -> tuple[list[discord.Embed], list[discord.File]]:
    """Return (embeds, files) for one 2x2 post.

    All four embeds share the same ``url`` field; Discord's client groups
    same-URL embeds attached to a single message into a gallery, giving
    us four visible images inside what reads as one post. The first embed
    carries the post title + submit-command hint; the others are
    image-only so they collapse cleanly into the gallery.

    Placeholder cells reference locally-rendered PNGs attached to the
    same message via ``attachment://<filename>``. ``version`` is baked
    into the filename so every edit produces new attachment CDN URLs,
    which is what actually busts Discord's client-side image cache
    (query-string cache-busters get stripped by some clients).

    Submitted cells point directly at the user's Discord CDN URL — no
    file upload needed for those.
    """
    cells_flat = [cell for row in layout for cell in row]
    header_cells = " · ".join(cells_flat)
    grouping_url = (
        f"{_GROUPING_URL_PREFIX}/{team_number}/post/{post_index}"
    )

    embeds: list[discord.Embed] = []
    files: list[discord.File] = []
    for idx, cell in enumerate(cells_flat):
        caption = captions.get(cell, "(missing caption)")
        submitted = submissions.get(cell)
        if idx == 0:
            embed = discord.Embed(
                title=f"Return 81 — {header_cells}",
                description=SUBMIT_COMMAND_HINT,
                url=grouping_url,
            )
        else:
            embed = discord.Embed(url=grouping_url)
        if submitted:
            embed.set_image(url=submitted)
        else:
            png_bytes = _render_cell_png(cell, caption)
            fname = f"r81_t{team_number}_p{post_index}_{cell}_v{version}.png"
            files.append(discord.File(BytesIO(png_bytes), filename=fname))
            embed.set_image(url=f"attachment://{fname}")
        embeds.append(embed)
    return embeds, files


async def _load_captions(team: BingoTeam) -> dict[str, str]:
    rows = await BingoCellState.filter(team=team)
    return {r.cell: r.caption for r in rows}


async def _load_submissions(team: BingoTeam) -> dict[str, str]:
    rows = await BingoSubmission.filter(team=team)
    return {r.cell: r.image_url for r in rows}


def _parse_embed_msg_ids(team: BingoTeam) -> list[int]:
    # getattr fallback so a stale-schema hydration (missing column) reads
    # as "no posts yet" instead of AttributeError. Matches _team_exp.
    raw = getattr(team, "embed_msg_ids_json", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "r81 team %s: embed_msg_ids_json is corrupt (%r)", team.id, raw
        )
        return []
    return [int(x) for x in parsed]


async def _rerender_team_dashboard(
    bot: discord.Client, team: BingoTeam, *, only_post: Optional[int] = None
) -> None:
    """Recompute the status message and every pinned 2x2 post from DB state.

    If ``only_post`` is set, only that post index (1-based) is edited; the
    status message is always refreshed.
    """
    thread = await _resolve_thread(bot, team.thread_id)
    if thread is None:
        logger.warning("rerender: team %s has no reachable thread", team.id)
        return
    captions = await _load_captions(team)
    submissions = await _load_submissions(team)

    if team.status_msg_id:
        try:
            msg = await thread.fetch_message(team.status_msg_id)
            await msg.edit(
                content=_status_text(set(submissions.keys()), _team_exp(team))
            )
        except (discord.NotFound, discord.HTTPException) as e:
            logger.warning("rerender status edit failed: %s", e)

    # ``version`` doubles as a per-edit cache-buster on the placehold.co
    # URLs. time_ns() gives strict monotonicity even for back-to-back
    # edits within the same second.
    version = time.time_ns()
    msg_ids = _parse_embed_msg_ids(team)
    layouts = post_layouts(_team_exp(team))
    for i, (msg_id, layout) in enumerate(zip(msg_ids, layouts), start=1):
        if only_post is not None and i != only_post:
            continue
        if not msg_id:
            continue
        try:
            msg = await thread.fetch_message(msg_id)
            embeds, files = _embeds_and_files_for_post(
                i,
                layout=layout,
                team_number=team.team_number,
                captions=captions,
                submissions=submissions,
                version=version,
            )
            # attachments= replaces the message's file list wholesale;
            # each rerender uploads new PNGs with unique filenames so the
            # new CDN URLs bust any client-side image cache.
            await msg.edit(embeds=embeds, attachments=files)
        except (discord.NotFound, discord.HTTPException) as e:
            logger.warning("rerender embed %s edit failed: %s", i, e)


# ---------------------------------------------------------------------------
# Persistent views
# ---------------------------------------------------------------------------


class BingoInviteConfirmView(discord.ui.View):
    """Persistent Accept/Decline buttons attached to a shared invite post.

    Dispatch: look up the team by ``interaction.message.id`` (both invitees
    click on the same message). Match the clicker against the team's
    :class:`BingoInvite` rows to find the row to mutate.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Accept",
        style=discord.ButtonStyle.success,
        custom_id="r81:invite:accept",
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_invite_click(interaction, "accepted")

    @discord.ui.button(
        label="Decline",
        style=discord.ButtonStyle.danger,
        custom_id="r81:invite:decline",
    )
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_invite_click(interaction, "declined")


class BingoRandomPickView(discord.ui.View):
    """Persistent view for the wildcard picker post. Ten buttons; each
    button's custom_id encodes only its slot index. The actual candidate
    for that slot is looked up in ``BingoTeam.picker_candidates_json`` at
    click time (message-id → team).
    """

    def __init__(self):
        super().__init__(timeout=None)
        for i in range(PICKER_SIZE):
            self.add_item(_PickButton(i))


class _PickButton(discord.ui.Button):
    def __init__(self, slot: int):
        super().__init__(
            label=f"Slot {slot + 1}",
            style=discord.ButtonStyle.primary,
            custom_id=f"r81:pick:{slot}",
        )
        self._slot = slot

    async def callback(self, interaction: discord.Interaction):
        cid = self.custom_id or ""
        try:
            slot = int(cid.rsplit(":", 1)[-1])
        except (ValueError, IndexError):
            slot = self._slot
        await _handle_picker_click(interaction, slot)


# ---------------------------------------------------------------------------
# Invite handling
# ---------------------------------------------------------------------------


async def _handle_invite_click(
    interaction: discord.Interaction, new_state: str
) -> None:
    msg_id = interaction.message.id if interaction.message else None
    if msg_id is None:
        await interaction.response.send_message(
            "Couldn't identify this invite post.", ephemeral=True
        )
        return
    team = await BingoTeam.filter(pending_invite_msg_id=msg_id).first()
    if team is None:
        await interaction.response.send_message(
            "This invite is no longer active.", ephemeral=True
        )
        return
    if team.state != "pending":
        await interaction.response.send_message(
            "This team has already moved past the invite stage.",
            ephemeral=True,
        )
        return
    invite = await BingoInvite.filter(
        team=team, invitee_disc_uuid=str(interaction.user.id)
    ).first()
    if invite is None:
        await interaction.response.send_message(
            "This invite isn't for you.", ephemeral=True
        )
        return
    if invite.state != "pending":
        await interaction.response.send_message(
            f"You've already {invite.state} this invite.", ephemeral=True
        )
        return

    invite.state = new_state
    await invite.save(update_fields=["state"])

    if new_state == "declined":
        await interaction.response.send_message(
            "You've declined the invite.", ephemeral=True
        )
        # A single decline kills the whole team.
        team.state = "disbanded"
        await team.save(update_fields=["state"])
        try:
            if interaction.message is not None:
                await interaction.message.edit(
                    content=(
                        interaction.message.content
                        + f"\n\n_❌ <@{interaction.user.id}> declined — team disbanded._"
                    ),
                    view=None,
                )
        except discord.HTTPException:
            pass
        logger.info("r81 team %s disbanded via decline by %s", team.id, interaction.user.id)
        return

    await interaction.response.send_message(
        "✅ You've accepted the invite.", ephemeral=True
    )

    # If both invitees have accepted, advance to picking.
    outstanding = await BingoInvite.filter(team=team).exclude(state="accepted").count()
    if outstanding > 0:
        return
    try:
        await _advance_to_picking(interaction.client, team)
    except Exception:
        logger.exception("r81 team %s: advance_to_picking failed", team.id)
        await interaction.followup.send(
            "Both invitees accepted, but something went wrong creating the "
            "team thread. Ping wen.",
            ephemeral=True,
        )


async def _advance_to_picking(client: discord.Client, team: BingoTeam) -> None:
    """Transition team from ``pending`` → ``picking``. Creates the private
    thread, adds everyone, and posts the picker.
    """
    parent = client.get_channel(TEAMS_PARENT_CHANNEL_ID)
    if not isinstance(parent, discord.TextChannel):
        raise RuntimeError(
            f"TEAMS_PARENT_CHANNEL_ID={TEAMS_PARENT_CHANNEL_ID} isn't a TextChannel"
        )
    thread = await parent.create_thread(
        name=f"r81-Team-{team.team_number}",
        type=discord.ChannelType.private_thread,
        invitable=False,
    )

    all_ids: list[int] = [int(team.creator_disc_uuid)]
    invites = await BingoInvite.filter(team=team)
    for inv in invites:
        try:
            all_ids.append(int(inv.invitee_disc_uuid))
        except ValueError:
            continue

    for uid in all_ids:
        try:
            await thread.add_user(discord.Object(id=uid))
        except discord.HTTPException as e:
            logger.warning("r81 add_user(%s) on thread %s: %s", uid, thread.id, e)

    # Persist creator + invitees as team members immediately (wildcard comes
    # next). Their ``role`` distinguishes how they joined.
    await BingoTeamMember.get_or_create(
        team=team, disc_uuid=team.creator_disc_uuid, defaults={"role": "creator"}
    )
    for inv in invites:
        await BingoTeamMember.get_or_create(
            team=team,
            disc_uuid=inv.invitee_disc_uuid,
            defaults={"role": "invitee"},
        )

    candidates = await _wynn_guild_candidates(
        client, exclude_disc_uuids=set(str(uid) for uid in all_ids)
    )
    if len(candidates) == 0:
        team.state = "picking"
        team.thread_id = thread.id
        await team.save(update_fields=["state", "thread_id"])
        await thread.send(
            "No wildcard candidates are eligible right now — ping staff for "
            "help. (Team assembly is paused.)"
        )
        return

    picker_msg = await thread.send(
        content=_picker_intro(candidates),
        view=_render_picker_view(candidates),
        allowed_mentions=discord.AllowedMentions.none(),
    )

    team.state = "picking"
    team.thread_id = thread.id
    team.picker_msg_id = picker_msg.id
    team.picker_candidates_json = json.dumps(
        [str(member.id) for member, _mc in candidates]
    )
    await team.save(update_fields=[
        "state", "thread_id", "picker_msg_id", "picker_candidates_json"
    ])
    logger.info(
        "r81 team %s advanced to picking with %d candidates (thread %s)",
        team.id, len(candidates), thread.id,
    )


def _picker_intro(candidates: list[tuple[discord.Member, str]]) -> str:
    """List the 10 candidates as ``<@discord_id> (mc_username)``.

    The ``<@id>`` syntax renders as a nice mention chip in the client
    (name + avatar) but does NOT ping or auto-add the user to the thread,
    provided the send site passes ``AllowedMentions.none()`` — which
    suppresses both notification delivery AND private-thread auto-adds.
    That's what let ten random guild members get dragged into someone
    else's team thread in the initial version of this flow.
    """
    lines = ["🎯 **Pick your wildcard teammate.** First click wins."]
    for i, (member, mc_username) in enumerate(candidates, start=1):
        lines.append(f"{i}. <@{member.id}> ({mc_username})")
    return "\n".join(lines)


def _render_picker_view(candidates: list[tuple[discord.Member, str]]) -> discord.ui.View:
    """Build the per-team picker view, overriding button labels to show the
    candidate name. The persistent-view class handles dispatch; this view
    exists to render friendly button labels at message-send time.
    """
    view = BingoRandomPickView()
    for i, item in enumerate(view.children):
        if isinstance(item, _PickButton) and i < len(candidates):
            item.label = candidates[i][0].display_name[:80]
    # Disable any surplus buttons.
    for i, item in enumerate(view.children):
        if isinstance(item, _PickButton) and i >= len(candidates):
            item.disabled = True
    return view


# ---------------------------------------------------------------------------
# Picker click
# ---------------------------------------------------------------------------


async def _handle_picker_click(
    interaction: discord.Interaction, slot: int
) -> None:
    msg_id = interaction.message.id if interaction.message else None
    if msg_id is None:
        await interaction.response.send_message(
            "Couldn't identify this picker post.", ephemeral=True
        )
        return
    team = await BingoTeam.filter(picker_msg_id=msg_id).first()
    if team is None:
        await interaction.response.send_message(
            "This picker post is no longer active.", ephemeral=True
        )
        return
    # Both wildcard picks (team.state=="picking") and extra_member bonus
    # picks (state=="playing") come through this handler. Disband is the
    # only state we outright refuse.
    if team.state not in ("picking", "playing"):
        await interaction.response.send_message(
            "This team isn't accepting picks right now.", ephemeral=True
        )
        return
    is_wildcard_pick = team.state == "picking"

    # Only current team members may pick.
    is_member = await BingoTeamMember.filter(
        team=team, disc_uuid=str(interaction.user.id)
    ).exists()
    if not is_member:
        await interaction.response.send_message(
            "Only the team can make this pick.", ephemeral=True
        )
        return
    if not team.picker_candidates_json:
        await interaction.response.send_message(
            "The candidate list is missing — ping staff.", ephemeral=True
        )
        return
    try:
        candidates = json.loads(team.picker_candidates_json)
    except json.JSONDecodeError:
        await interaction.response.send_message(
            "Corrupt candidate list — ping staff.", ephemeral=True
        )
        return
    if not (0 <= slot < len(candidates)):
        await interaction.response.send_message(
            "That button is empty.", ephemeral=True
        )
        return
    picked_disc_uuid = candidates[slot]

    # First-click-wins: recheck via unique constraint on join.
    already_on_a_team = await _current_team_for(picked_disc_uuid)
    if already_on_a_team is not None and already_on_a_team.id != team.id:
        await interaction.response.send_message(
            "That candidate just joined a different team. Pick another.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=False)

    await BingoTeamMember.create(
        team=team, disc_uuid=picked_disc_uuid, role="wildcard"
    )
    thread = await _resolve_thread(interaction.client, team.thread_id)
    if thread is not None:
        try:
            await thread.add_user(discord.Object(id=int(picked_disc_uuid)))
        except (discord.HTTPException, ValueError) as e:
            logger.warning("r81 add wildcard %s to thread: %s", picked_disc_uuid, e)

    if is_wildcard_pick:
        # Seed cell captions and post the dashboard — the "playing"
        # transition is a wildcard-pick-only side effect.
        await _seed_cell_captions(team)
        if thread is not None:
            await _post_pinned_dashboard(thread, team)
        team.state = "playing"
        await team.save(update_fields=["state"])
    else:
        # Extra-teammate pick (via _announce_bingo). Adding this member
        # tips the roster past 4, which triggers a board expansion.
        await _apply_expansion_if_needed(interaction.client, team)

    # Delete the picker post outright — the candidate list has served
    # its purpose and shouldn't linger. Then post a fresh, minimal
    # "joined as X" note; the picked user is now a legit thread member
    # so their @mention is welcome.
    try:
        if interaction.message is not None:
            await interaction.message.delete()
    except discord.HTTPException:
        pass
    if thread is not None:
        role_label = "wildcard" if is_wildcard_pick else "extra teammate"
        try:
            await thread.send(
                f"🎯 <@{picked_disc_uuid}> joined as the {role_label}.",
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
        except discord.HTTPException as e:
            logger.warning("r81 join notice send failed: %s", e)
    team.picker_msg_id = None
    team.picker_candidates_json = None
    await team.save(update_fields=["picker_msg_id", "picker_candidates_json"])
    logger.info(
        "r81 team %s wildcard resolved to %s (slot %s)",
        team.id, picked_disc_uuid, slot,
    )


async def _seed_cell_captions(
    team: BingoTeam, *, only_cells: Optional[tuple[str, ...]] = None
) -> None:
    """Seed missing captions for either the full board at ``expansion_count``
    or the given ``only_cells`` subset (used to backfill an expansion slab
    without re-touching already-seeded cells).
    """
    cells = only_cells if only_cells is not None else bingo_cells(_team_exp(team))
    for cell in cells:
        if await BingoCellState.filter(team=team, cell=cell).exists():
            continue
        caption = await _generate_cell_caption(team)
        await BingoCellState.create(team=team, cell=cell, caption=caption)


async def _post_pinned_dashboard(thread: discord.Thread, team: BingoTeam) -> None:
    """Post + pin the status message and one 2x2 embed per layout at the
    current ``expansion_count``. Called once at the ``playing`` handoff.
    """
    captions = await _load_captions(team)
    submissions = await _load_submissions(team)  # empty at this point

    status_msg = await thread.send(
        _status_text(set(submissions.keys()), _team_exp(team))
    )
    try:
        await status_msg.pin()
    except discord.HTTPException as e:
        logger.warning("r81 pin status failed: %s", e)
    team.status_msg_id = status_msg.id

    version = time.time_ns()
    msg_ids: list[int] = []
    for i, layout in enumerate(post_layouts(_team_exp(team)), start=1):
        embeds, files = _embeds_and_files_for_post(
            i,
            layout=layout,
            team_number=team.team_number,
            captions=captions,
            submissions=submissions,
            version=version,
        )
        msg = await thread.send(embeds=embeds, files=files)
        try:
            await msg.pin()
        except discord.HTTPException as e:
            logger.warning("r81 pin embed %s failed: %s", i, e)
        msg_ids.append(msg.id)

    team.embed_msg_ids_json = json.dumps(msg_ids)
    try:
        await team.save(update_fields=["status_msg_id", "embed_msg_ids_json"])
    except OperationalError as e:
        # Stale returns.db schema — the embed_msg_ids_json column doesn't
        # exist. Fall back to persisting the status id only so the team
        # can still enter "playing" and accept submissions; the pinned
        # embeds won't be re-editable until orm._ensure_r81_schema heals
        # the schema on the next container restart.
        logger.warning(
            "r81 team %s: schema stale (missing embed_msg_ids_json); "
            "saving status_msg_id only, dashboard rerenders disabled "
            "until container restart. (%s)",
            team.id, e,
        )
        await team.save(update_fields=["status_msg_id"])


async def _apply_expansion_if_needed(
    bot: discord.Client, team: BingoTeam
) -> bool:
    """Expand the board when the roster has grown past 4 members.

    Called after every ``BingoTeamMember`` insert that could push the count
    over the threshold. Idempotent: safe to invoke multiple times per
    member add, and any concurrent invocation is short-circuited by the
    ``expansion_count >= new_exp`` guard.

    Returns ``True`` if an expansion was applied, ``False`` otherwise.
    """
    if team.state != "playing":
        return False
    count = await BingoTeamMember.filter(team=team).count()
    new_exp = max(0, count - 4)
    if new_exp <= _team_exp(team):
        return False

    # Bail BEFORE any side effect if the container is running against a
    # stale returns.db that predates the expansion columns. Reads survive
    # via _team_exp's getattr fallback, but writes (below) would emit an
    # UPDATE against non-existent columns and SQL-error. Container restart
    # runs orm._ensure_r81_schema and cures this — until then, skip
    # expansion cleanly so the picker + member-add still succeed.
    if not (
        hasattr(team, "expansion_count")
        and hasattr(team, "embed_msg_ids_json")
    ):
        logger.warning(
            "r81 team %s: returns.db schema is stale (missing expansion_count "
            "or embed_msg_ids_json); skipping board expansion. Restart the "
            "dazebot container so orm._ensure_r81_schema can heal the schema.",
            team.id,
        )
        return False

    thread = await _resolve_thread(bot, team.thread_id)
    if thread is None:
        logger.warning(
            "r81 team %s: cannot expand — thread %s unreachable",
            team.id, team.thread_id,
        )
        return False

    # Seed captions for just the new slab's cells.
    prev_cells = set(bingo_cells(_team_exp(team)))
    new_cells = tuple(c for c in bingo_cells(new_exp) if c not in prev_cells)
    await _seed_cell_captions(team, only_cells=new_cells)

    # Post + pin one embed per new 2x2 quadrant, in the layout order
    # ``post_layouts`` guarantees. The first new post index is len(prev).
    captions = await _load_captions(team)
    submissions = await _load_submissions(team)
    version = time.time_ns()
    existing_ids = _parse_embed_msg_ids(team)
    new_layouts = new_post_layouts_for(new_exp)
    for offset, layout in enumerate(new_layouts):
        post_index = len(existing_ids) + offset + 1
        embeds, files = _embeds_and_files_for_post(
            post_index,
            layout=layout,
            team_number=team.team_number,
            captions=captions,
            submissions=submissions,
            version=version,
        )
        msg = await thread.send(embeds=embeds, files=files)
        try:
            await msg.pin()
        except discord.HTTPException as e:
            logger.warning("r81 pin expansion embed %s failed: %s", post_index, e)
        existing_ids.append(msg.id)

    # Persist the new post ids + expansion count. Order matters: write the
    # ids BEFORE bumping ``expansion_count`` so a concurrent rerender that
    # reads stale state still only touches the old posts.
    team.embed_msg_ids_json = json.dumps(existing_ids)
    team.expansion_count = new_exp
    try:
        await team.save(update_fields=["embed_msg_ids_json", "expansion_count"])
    except OperationalError as e:
        # Should have been caught by the hasattr guard above, but if a
        # partial-heal DB slips through, log and bail without a raise so
        # the picker click still succeeds. The new pinned embeds will be
        # orphaned in the thread; a rerender_hard after container restart
        # will clean them up.
        logger.warning(
            "r81 team %s: expansion save failed on stale schema (%s); "
            "board dims will re-derive from prev expansion_count on next boot",
            team.id, e,
        )
        return False

    await _rerender_team_dashboard(bot, team)
    rows, cols = board_dims(new_exp)
    try:
        await thread.send(
            f"🧩 **Board expanded** — now `{rows}×{cols}` "
            f"(expansion {new_exp}). {len(new_cells)} new cell(s) added."
        )
    except discord.HTTPException as e:
        logger.warning("r81 expansion notice send failed: %s", e)
    logger.info(
        "r81 team %s expanded to exp=%s (%dx%d, +%d cells)",
        team.id, new_exp, rows, cols, len(new_cells),
    )
    return True


# ---------------------------------------------------------------------------
# Submission listener (~return 81 submit <cell>)
# ---------------------------------------------------------------------------


async def _on_message_submit(bot: discord.Client, message: discord.Message) -> None:
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.Thread):
        return
    m = _SUBMIT_RE.match(message.content or "")
    if m is None:
        return
    cell = m.group(1).upper()
    team = await BingoTeam.filter(thread_id=message.channel.id).first()
    if team is None:
        return
    if team.state != "playing":
        await message.reply(
            "This team's bingo card isn't live yet.", mention_author=False
        )
        return
    is_member = await BingoTeamMember.filter(
        team=team, disc_uuid=str(message.author.id)
    ).exists()
    if not is_member:
        await message.reply(
            "Only team members can submit here.", mention_author=False
        )
        return
    if cell not in bingo_cells(_team_exp(team)):
        return
    if not message.attachments:
        await message.reply(
            f"Attach a photo to your `~return 81 submit {cell}` message. "
            "The attached image is what gets submitted.",
            mention_author=False,
        )
        return
    existing = await BingoSubmission.filter(team=team, cell=cell).first()
    if existing is not None:
        await message.reply(
            f"`{cell}` already has a submission. Ask staff to "
            f"`~manage_return 81 clear {team.team_number} {cell}` first.",
            mention_author=False,
        )
        return

    image_url = message.attachments[0].url
    filled_before = set(
        await BingoSubmission.filter(team=team).values_list("cell", flat=True)
    )
    await BingoSubmission.create(
        team=team,
        cell=cell,
        submitter_disc_uuid=str(message.author.id),
        image_url=image_url,
    )
    filled_after = filled_before | {cell}

    await _award_score(team, POINTS_PER_CELL)

    caption = await _caption_for_cell(team, cell)
    subject_matches: list[str] = []
    if caption:
        subject_matches = await _award_subject_bonus(
            team, caption, POINTS_PER_SUBJECT_BONUS
        )

    await _rerender_team_dashboard(
        bot, team, only_post=cell_to_post_index(_team_exp(team))[cell]
    )

    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass

    if subject_matches:
        try:
            await message.reply(
                f"🎯 Subject bonus: +{POINTS_PER_SUBJECT_BONUS} to "
                + ", ".join(f"`{n}`" for n in subject_matches),
                mention_author=False,
            )
        except discord.HTTPException:
            pass

    new_lines = _detect_new_bingos(filled_before, filled_after, _team_exp(team))
    for line in new_lines:
        await _announce_bingo(bot, message.channel, team, line)


async def _award_score(team: BingoTeam, per_member_points: int) -> None:
    event, _ = await WeeklyEvent.get_or_create(week=team.week)
    members = await BingoTeamMember.filter(team=team)
    for m in members:
        disc, _ = await DiscordAccount.get_or_create(disc_uuid=m.disc_uuid)
        row, created = await Score.get_or_create(
            event=event, discord_account=disc, defaults={"score": per_member_points}
        )
        if not created:
            row.score += per_member_points
            await row.save(update_fields=["score"])


# Bonus points awarded per non-empty MC username matched in a submitted
# cell's caption — the "extra points to the players in the photos"
# incentive. Applied on top of the flat POINTS_PER_CELL going to team
# members. Cleared/regenerated cells reverse the bonus by passing a
# negative value.
POINTS_PER_SUBJECT_BONUS = 1

# Score awarded to every current team member each time a bingo line
# completes. MUST be strictly greater than POINTS_PER_SUBJECT_BONUS —
# completing a line should out-earn merely being named in a photo caption
# so the incentive lines up with the spec ("more points for an actual
# bingo line than for participation"). Not clawed back when a submission
# is cleared: rewiring the retroactive line un-detection is not worth the
# complexity for the once-a-week user impact.
POINTS_PER_BINGO_LINE = 5


async def _award_subject_bonus(
    team: BingoTeam, caption: str, per_subject_points: int
) -> list[str]:
    """Award ``per_subject_points`` to each Returner whose MC username
    appears (as a whole word, case-insensitive) in ``caption``.

    Universe of candidates = current ``guild="Returners"`` roster + this
    team's own MC usernames, so we only match against real player names
    and never mistake a common noun in a template ("Fighter", "Brawl") for
    a person. Longer names are matched first so a hypothetical shorter
    substring name can't steal the match.

    Returns the list of matched MC usernames — useful for logging + the
    on-submission Discord confirmation. Returns ``[]`` when
    ``per_subject_points == 0``.
    """
    if per_subject_points == 0:
        return []

    roster = set(
        await MinecraftAccount.filter(guild=WYNN_GUILD_NAME).values_list(
            "mc_username", flat=True
        )
    )
    team_names = set(await _team_player_names(team))
    # ``"a teammate"`` is the fallback from _team_player_names for unlinked
    # members — filter it out so we don't try to match a literal phrase.
    candidates = {n for n in (roster | team_names) if n and n != "a teammate"}
    if not candidates:
        return []

    matched: list[str] = []
    for name in sorted(candidates, key=len, reverse=True):
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        if pattern.search(caption):
            matched.append(name)

    if not matched:
        return []

    event, _ = await WeeklyEvent.get_or_create(week=team.week)
    for name in matched:
        mc = await MinecraftAccount.filter(mc_username__iexact=name).first()
        if mc is None:
            continue
        disc = await DiscordAccount.filter(minecraft_account_id=mc.id).first()
        if disc is None:
            continue
        row, created = await Score.get_or_create(
            event=event,
            discord_account=disc,
            defaults={"score": per_subject_points},
        )
        if not created:
            row.score += per_subject_points
            await row.save(update_fields=["score"])

    logger.info(
        "r81 team %s subject bonus: matched=%s per_subject=%+d",
        team.id, matched, per_subject_points,
    )
    return matched


async def _caption_for_cell(team: BingoTeam, cell: str) -> Optional[str]:
    row = await BingoCellState.filter(team=team, cell=cell).first()
    return row.caption if row is not None else None


async def _announce_bingo(
    bot: discord.Client,
    thread: discord.Thread,
    team: BingoTeam,
    line: tuple[str, ...],
) -> None:
    """Announce a newly-completed bingo line + kick off the extra-teammate
    picker. Idempotent via the unique constraint on ``BingoBingoEvent.line_key``.

    Awards ``POINTS_PER_BINGO_LINE`` to every current team member, which is
    strictly greater than ``POINTS_PER_SUBJECT_BONUS`` — completing a line
    should out-earn just being named in a photo caption.

    If a picker is already live in the thread (e.g. the previous bingo
    hasn't resolved its picker yet), we skip posting a second one — the
    ``BingoBingoEvent`` row still records the line so it can't fire again,
    but no extra teammate is added for that specific bingo. Team gets the
    points either way.
    """
    key = _line_key(line)
    exists = await BingoBingoEvent.filter(team=team, line_key=key).first()
    if exists is not None:
        return
    await BingoBingoEvent.create(team=team, line_key=key)
    await _award_score(team, POINTS_PER_BINGO_LINE)

    header = (
        f"🎉 **Bingo!** Line: `{' → '.join(line)}` "
        f"— `+{POINTS_PER_BINGO_LINE}` to each teammate."
    )

    if team.picker_msg_id is not None:
        await thread.send(
            f"{header}\n"
            "A picker is already active in this thread — no extra teammate "
            "was queued for this bingo. Resolve the current picker first, "
            "then the next bingo will trigger a fresh one."
        )
        return

    candidates = await _wildcard_candidates_excluding_team(bot, team)
    candidates = candidates[:EXTRA_MEMBER_PICKER_SIZE]
    if not candidates:
        await thread.send(
            f"{header}\n"
            "No eligible extra teammates are available right now — team "
            "keeps the points but doesn't gain a member for this bingo."
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
    await team.save(update_fields=["picker_msg_id", "picker_candidates_json"])



async def _reroll_empty_captions(team: BingoTeam) -> None:
    filled = set(await BingoSubmission.filter(team=team).values_list("cell", flat=True))
    for cell in bingo_cells(_team_exp(team)):
        if cell in filled:
            continue
        caption = await _generate_cell_caption(team)
        row = await BingoCellState.filter(team=team, cell=cell).first()
        if row is None:
            await BingoCellState.create(team=team, cell=cell, caption=caption)
        else:
            row.caption = caption
            await row.save(update_fields=["caption"])


async def _wildcard_candidates_excluding_team(
    client: discord.Client, team: BingoTeam
) -> list[tuple[discord.Member, str]]:
    existing = set(
        await BingoTeamMember.filter(team=team).values_list("disc_uuid", flat=True)
    )
    return await _wynn_guild_candidates(client, exclude_disc_uuids=existing)


# ---------------------------------------------------------------------------
# Listener registration (called from Returns.cog_load)
# ---------------------------------------------------------------------------


def register_listeners(bot: commands.Bot) -> None:
    async def _on_message(message: discord.Message) -> None:
        try:
            await _on_message_submit(bot, message)
        except Exception:
            logger.exception("r81 on_message_submit failed")
        try:
            await _track_admin_presence(message)
        except Exception:
            logger.exception("r81 admin presence tracking failed")

    bot.add_listener(_on_message, name="on_message")


# ---------------------------------------------------------------------------
# /return 81 handler — the "click to invite" starter view + modal
# ---------------------------------------------------------------------------


class _InviteStartView(discord.ui.View):
    def __init__(self, *, guild_id: int):
        super().__init__(timeout=_INVITE_STARTER_TIMEOUT)
        self.guild_id = guild_id
        btn = discord.ui.Button(
            label="🎯 Invite teammates",
            style=discord.ButtonStyle.primary,
        )
        btn.callback = self._on_click
        self.add_item(btn)

    async def _on_click(self, interaction: discord.Interaction) -> None:
        # Re-gate on the same role set the dispatcher used.
        if not _can_invite(interaction.user, interaction.client):
            await interaction.response.send_message(
                "You don't have access to `/return 81`.", ephemeral=True
            )
            return
        # Also refuse if the caller is already on any r81 team.
        existing = await _current_team_for(str(interaction.user.id))
        if existing is not None:
            await interaction.response.send_message(
                "You're already on an active r81 team.", ephemeral=True
            )
            return
        await interaction.response.send_modal(_InviteModal(guild_id=self.guild_id))


class _InviteModal(discord.ui.Modal):
    invitee_a = discord.ui.TextInput(
        label="First teammate (mention / id / username)",
        placeholder="@someone or a Discord name",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=100,
        required=True,
    )
    invitee_b = discord.ui.TextInput(
        label="Second teammate (mention / id / username)",
        placeholder="@someone or a Discord name",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=100,
        required=True,
    )

    def __init__(self, *, guild_id: int):
        super().__init__(title="Return 81 — Invite teammates")
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if INVITE_THREAD_ID == 0:
            await interaction.followup.send(
                "The r81 invite thread hasn't been configured yet. Ping wen.",
                ephemeral=True,
            )
            return

        raw_a = str(self.invitee_a.value)
        raw_b = str(self.invitee_b.value)

        member_a, err_a = await _resolve_invitee(
            interaction.client, raw_a, self.guild_id
        )
        if err_a:
            await interaction.followup.send(f"Invitee A: {err_a}", ephemeral=True)
            return
        member_b, err_b = await _resolve_invitee(
            interaction.client, raw_b, self.guild_id
        )
        if err_b:
            await interaction.followup.send(f"Invitee B: {err_b}", ephemeral=True)
            return
        assert member_a is not None and member_b is not None  # narrow for type-checker

        if member_a.id == interaction.user.id or member_b.id == interaction.user.id:
            await interaction.followup.send(
                "You can't invite yourself.", ephemeral=True
            )
            return
        if member_a.id == member_b.id:
            await interaction.followup.send(
                "Invitees must be two different people.", ephemeral=True
            )
            return
        if not _has_team_role(member_a):
            await interaction.followup.send(
                f"{member_a.mention} isn't eligible — they'd need the Member, "
                "Hiatus, or Honourary role.",
                ephemeral=True,
            )
            return
        if not _has_team_role(member_b):
            await interaction.followup.send(
                f"{member_b.mention} isn't eligible — they'd need the Member, "
                "Hiatus, or Honourary role.",
                ephemeral=True,
            )
            return
        for m in (member_a, member_b):
            other_team = await _current_team_for(str(m.id))
            if other_team is not None:
                await interaction.followup.send(
                    f"{m.mention} is already on an active r81 team.",
                    ephemeral=True,
                )
                return

        invite_thread = await _resolve_thread(interaction.client, INVITE_THREAD_ID)
        if invite_thread is None:
            await interaction.followup.send(
                f"I can't reach the invite thread ({INVITE_THREAD_ID}). Ping wen.",
                ephemeral=True,
            )
            return

        team_number = await _next_team_number()
        team = await BingoTeam.create(
            week=WEEK,
            team_number=team_number,
            creator_disc_uuid=str(interaction.user.id),
            state="pending",
        )
        await BingoInvite.create(
            team=team, invitee_disc_uuid=str(member_a.id), state="pending"
        )
        await BingoInvite.create(
            team=team, invitee_disc_uuid=str(member_b.id), state="pending"
        )

        try:
            invite_msg = await invite_thread.send(
                content=(
                    f"🎯 **r81 invite** — {interaction.user.mention} wants to "
                    f"form Team {team_number} with {member_a.mention} and "
                    f"{member_b.mention}.\n"
                    "Both invitees must click **Accept** for the team to start. "
                    "One decline disbands the team."
                ),
                view=BingoInviteConfirmView(),
                allowed_mentions=discord.AllowedMentions(
                    users=[member_a, member_b], roles=False, everyone=False
                ),
            )
        except discord.HTTPException:
            logger.exception("r81 failed to post invite for team %s", team.id)
            team.state = "disbanded"
            await team.save(update_fields=["state"])
            await interaction.followup.send(
                "I couldn't post the invite message. The team has been "
                "cancelled — try again in a moment.",
                ephemeral=True,
            )
            return

        team.pending_invite_msg_id = invite_msg.id
        await team.save(update_fields=["pending_invite_msg_id"])

        await interaction.followup.send(
            f"✅ Invite posted for Team {team_number}: {invite_msg.jump_url}",
            ephemeral=True,
        )
        logger.info(
            "r81 team %s (#%d) invite posted by %s → %s, %s",
            team.id, team_number, interaction.user.id, member_a.id, member_b.id,
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        logger.exception(
            "r81 invite modal error (user=%s)", interaction.user.id, exc_info=error
        )
        msg = (
            "Something went wrong submitting your invite. Nothing was "
            "posted. Try again — if it keeps failing, ping wen."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


async def _next_team_number() -> int:
    latest = await BingoTeam.all().order_by("-team_number").first()
    return 1 if latest is None else latest.team_number + 1


@register(WEEK, tier=Tier.GUILD, custom_check=_can_invite)
async def handle(ctx: commands.Context) -> None:
    # Prefix path: if the message is really an r81 submission (~return 81
    # <cell> with an attachment), the on_message listener is going to
    # handle it — don't also post the invite button and spam the thread.
    # The slash path (ctx.interaction is not None) can't be a submission,
    # so it's always the invite flow there.
    if ctx.interaction is None and ctx.message is not None:
        if _SUBMIT_RE.match(ctx.message.content or ""):
            return

    guild_id = ctx.guild.id if ctx.guild is not None else CurrConfig.GUILD
    view = _InviteStartView(guild_id=guild_id)
    intro = (
        "🎯 **Return 81 — Team Bingo.**\n"
        "Click below to invite two teammates. Once both accept, a private "
        "team thread opens and you pick a wildcard fourth from a shortlist."
    )
    await ctx.reply(intro, view=view)


# ---------------------------------------------------------------------------
# ~manage_return 81 subcommands
# ---------------------------------------------------------------------------


async def _resolve_team_by_id_or_number(raw: str) -> Optional[BingoTeam]:
    """Accept either a team UUID (any UUID prefix that matches uniquely) or
    a team_number int. Kept lenient so staff can grab a team from the list
    without copying the whole UUID.
    """
    if raw.isdigit():
        return await BingoTeam.filter(team_number=int(raw)).first()
    match = await BingoTeam.filter(id__startswith=raw).first()
    if match is not None:
        return match
    return await BingoTeam.filter(id=raw).first()


@register_manage(
    WEEK, "list", tier=Tier.STAFF,
    help="List every non-disbanded r81 team.",
    usage="",
)
async def _manage_list(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    teams = await BingoTeam.exclude(state="disbanded").order_by("team_number")
    if not teams:
        await send_feedback(ctx, "No active r81 teams.", persist=persist)
        return
    lines = ["**Return 81 — active teams:**"]
    for t in teams:
        members = await BingoTeamMember.filter(team=t).count()
        subs = await BingoSubmission.filter(team=t).count()
        bingos = await BingoBingoEvent.filter(team=t).count()
        t_exp = _team_exp(t)
        total_cells = len(bingo_cells(t_exp))
        thread = f"<#{t.thread_id}>" if t.thread_id else "(no thread)"
        lines.append(
            f"- **Team {t.team_number}** — state=`{t.state}` "
            f"members={members} cells={subs}/{total_cells} "
            f"exp={t_exp} bingos={bingos} {thread} "
            f"id=`{t.id}`"
        )
    await send_feedback(ctx, "\n".join(lines), persist=persist)


@register_manage(
    WEEK, "show", tier=Tier.STAFF,
    help="Full detail dump for one team.",
    usage="<team_id_or_number>",
)
async def _manage_show(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if not args:
        await send_feedback(ctx, "Usage: `~manage_return 81 show <team>`", persist=persist)
        return
    team = await _resolve_team_by_id_or_number(args[0])
    if team is None:
        await send_feedback(ctx, f"No team matches `{args[0]}`.", persist=persist)
        return
    members = await BingoTeamMember.filter(team=team)
    subs = await BingoSubmission.filter(team=team)
    filled = {s.cell for s in subs}
    bingos = await BingoBingoEvent.filter(team=team)
    exp = _team_exp(team)
    rows, cols = board_dims(exp)
    lines = [
        f"**Team {team.team_number}** (state=`{team.state}`, id=`{team.id}`)",
        f"Board: `{rows}×{cols}` (expansion {exp})",
        f"Thread: <#{team.thread_id}>" if team.thread_id else "Thread: (none)",
        "Members:",
    ]
    for m in members:
        lines.append(f"  - <@{m.disc_uuid}> ({m.role})")
    lines.append("Cells filled:")
    for r in range(rows):
        row_letter = row_label(r)
        row_cells = " ".join(
            "✅" if cell_name(r, c) in filled else "·" for c in range(cols)
        )
        lines.append(f"  {row_letter}: {row_cells}")
    if bingos:
        lines.append("Bingos:")
        for b in bingos:
            lines.append(f"  - `{b.line_key}`")
    await send_feedback(ctx, "\n".join(lines), persist=persist)


@register_manage(
    WEEK, "sample", tier=Tier.STAFF,
    help=(
        "Preview N randomly-generated cell captions using real Wynn guild "
        "member names — no DB writes, no team involvement. Handy for "
        "sanity-checking new resources/main.yaml templates."
    ),
    usage="[count=1]",
)
async def _manage_sample(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    count = 1
    if args:
        try:
            count = int(args[0])
        except ValueError:
            await send_feedback(
                ctx, f"`{args[0]}` isn't a number.", persist=persist
            )
            return
    count = max(1, min(count, 16))

    roster = await MinecraftAccount.filter(guild=WYNN_GUILD_NAME).values_list(
        "mc_username", flat=True
    )
    roster = [n for n in roster if n]
    if not roster:
        await send_feedback(
            ctx,
            f"No Wynncraft guild `{WYNN_GUILD_NAME}` members are in the DB "
            "— nothing to sample against.",
            persist=persist,
        )
        return

    lines: list[str] = [
        f"**Return 81 — {count} sample caption(s)** "
        f"(pool drawn from `{WYNN_GUILD_NAME}`, {len(roster)} eligible)"
    ]
    for i in range(1, count + 1):
        picks = random.sample(roster, k=min(4, len(roster)))
        pool = _make_players_pool(picks)
        try:
            body = await _resolver.resolve(players=pool)
        except Exception:
            logger.exception("r81 sample: resolver failed")
            body = "(resolver failed — check logs)"
        lines.append(f"{i}. Post a picture of {body}")
    await send_feedback(ctx, "\n".join(lines), persist=persist)


@register_manage(
    WEEK, "regen", tier=Tier.STAFF,
    help="Reroll a cell's caption; also clears any submission and its points.",
    usage="<team> <cell>",
)
async def _manage_regen(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if len(args) < 2:
        await send_feedback(
            ctx, "Usage: `~manage_return 81 regen <team> <cell>`", persist=persist
        )
        return
    team = await _resolve_team_by_id_or_number(args[0])
    if team is None:
        await send_feedback(ctx, f"No team matches `{args[0]}`.", persist=persist)
        return
    cell = args[1].upper()
    exp = _team_exp(team)
    valid = bingo_cells(exp)
    if cell not in valid:
        rows, cols = board_dims(exp)
        await send_feedback(
            ctx,
            f"`{cell}` isn't a valid cell (board is {rows}×{cols} at "
            f"expansion {exp}).",
            persist=persist,
        )
        return
    submission = await BingoSubmission.filter(team=team, cell=cell).first()
    if submission is not None:
        # Snapshot the caption BEFORE the reroll so the un-award targets
        # the same names the original submission credited. If no cell_row
        # exists somehow, subject bonus is a no-op.
        old_caption = await _caption_for_cell(team, cell)
        await submission.delete()
        # Subtract the points we awarded for this cell to every team member.
        await _award_score(team, -POINTS_PER_CELL)
        if old_caption:
            await _award_subject_bonus(team, old_caption, -POINTS_PER_SUBJECT_BONUS)
    cell_row = await BingoCellState.filter(team=team, cell=cell).first()
    new_caption = await _generate_cell_caption(team)
    if cell_row is None:
        await BingoCellState.create(team=team, cell=cell, caption=new_caption)
    else:
        cell_row.caption = new_caption
        await cell_row.save(update_fields=["caption"])
    await _rerender_team_dashboard(
        ctx.bot, team, only_post=cell_to_post_index(exp)[cell]
    )
    await send_feedback(
        ctx,
        f"✅ Regenerated `{cell}` for team {team.team_number}."
        + (" Cleared existing submission." if submission is not None else ""),
        persist=persist,
    )


@register_manage(
    WEEK, "clear", tier=Tier.STAFF,
    help="Clear a cell's submission without rerolling its caption.",
    usage="<team> <cell>",
)
async def _manage_clear(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if len(args) < 2:
        await send_feedback(
            ctx, "Usage: `~manage_return 81 clear <team> <cell>`", persist=persist
        )
        return
    team = await _resolve_team_by_id_or_number(args[0])
    if team is None:
        await send_feedback(ctx, f"No team matches `{args[0]}`.", persist=persist)
        return
    cell = args[1].upper()
    submission = await BingoSubmission.filter(team=team, cell=cell).first()
    if submission is None:
        await send_feedback(ctx, f"No submission at `{cell}`.", persist=persist)
        return
    caption = await _caption_for_cell(team, cell)
    await submission.delete()
    await _award_score(team, -POINTS_PER_CELL)
    if caption:
        await _award_subject_bonus(team, caption, -POINTS_PER_SUBJECT_BONUS)
    await _rerender_team_dashboard(
        ctx.bot, team, only_post=cell_to_post_index(_team_exp(team))[cell]
    )
    await send_feedback(
        ctx, f"✅ Cleared `{cell}` for team {team.team_number}.", persist=persist
    )


@register_manage(
    WEEK, "rerollCard", tier=Tier.ADMIN,
    help="Reroll every caption for a team's card (empty cells only unless --force).",
    usage="<team> [--force]",
)
async def _manage_reroll_card(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if not args:
        await send_feedback(
            ctx, "Usage: `~manage_return 81 rerollCard <team> [--force]`", persist=persist
        )
        return
    force = "--force" in args
    team = await _resolve_team_by_id_or_number(args[0])
    if team is None:
        await send_feedback(ctx, f"No team matches `{args[0]}`.", persist=persist)
        return
    subs = await BingoSubmission.filter(team=team).count()
    if subs > 0 and not force:
        await send_feedback(
            ctx,
            f"Team has {subs} submitted cells. Re-run with `--force` to reroll "
            "captions for the remaining empty cells only.",
            persist=persist,
        )
        return
    await _reroll_empty_captions(team)
    await _rerender_team_dashboard(ctx.bot, team)
    await send_feedback(
        ctx, f"✅ Rerolled captions for team {team.team_number}.", persist=persist
    )


@register_manage(
    WEEK, "rerollPool", tier=Tier.STAFF,
    help="Redraw the 10-candidate wildcard shortlist (state must be picking).",
    usage="<team>",
)
async def _manage_reroll_pool(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if not args:
        await send_feedback(ctx, "Usage: `~manage_return 81 rerollPool <team>`", persist=persist)
        return
    team = await _resolve_team_by_id_or_number(args[0])
    if team is None:
        await send_feedback(ctx, f"No team matches `{args[0]}`.", persist=persist)
        return
    if team.state != "picking":
        await send_feedback(
            ctx,
            f"Team {team.team_number} isn't in picking (state=`{team.state}`).",
            persist=persist,
        )
        return
    all_members = await BingoTeamMember.filter(team=team).values_list(
        "disc_uuid", flat=True
    )
    candidates = await _wynn_guild_candidates(
        ctx.bot, exclude_disc_uuids=set(all_members)
    )
    if not candidates:
        await send_feedback(ctx, "No eligible candidates right now.", persist=persist)
        return
    thread = await _resolve_thread(ctx.bot, team.thread_id)
    if thread is None:
        await send_feedback(ctx, "Team thread is unreachable.", persist=persist)
        return
    if team.picker_msg_id:
        try:
            old = await thread.fetch_message(team.picker_msg_id)
            await old.delete()
        except discord.HTTPException:
            pass
    picker_msg = await thread.send(
        content=_picker_intro(candidates),
        view=_render_picker_view(candidates),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    team.picker_msg_id = picker_msg.id
    team.picker_candidates_json = json.dumps(
        [str(member.id) for member, _mc in candidates]
    )
    await team.save(update_fields=["picker_msg_id", "picker_candidates_json"])
    await send_feedback(
        ctx,
        f"✅ Rerolled pool for team {team.team_number} ({len(candidates)} candidates).",
        persist=persist,
    )


@register_manage(
    WEEK, "forceAdd", tier=Tier.ADMIN,
    help="Bypass invite/pick and add a member to a team.",
    usage="<team> <@user>",
)
async def _manage_force_add(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if len(args) < 2:
        await send_feedback(
            ctx, "Usage: `~manage_return 81 forceAdd <team> <@user>`", persist=persist
        )
        return
    team = await _resolve_team_by_id_or_number(args[0])
    if team is None:
        await send_feedback(ctx, f"No team matches `{args[0]}`.", persist=persist)
        return
    member, err = await _resolve_invitee(ctx.bot, args[1], CurrConfig.GUILD)
    if err:
        await send_feedback(ctx, err, persist=persist)
        return
    assert member is not None
    row, created = await BingoTeamMember.get_or_create(
        team=team, disc_uuid=str(member.id), defaults={"role": "forced"}
    )
    if not created:
        await send_feedback(
            ctx, f"{member.mention} is already on this team.", persist=persist
        )
        return
    thread = await _resolve_thread(ctx.bot, team.thread_id)
    if thread is not None:
        try:
            await thread.add_user(discord.Object(id=member.id))
        except discord.HTTPException as e:
            logger.warning("forceAdd add_user failed: %s", e)
    # A forced insert past the base-4 roster triggers a board expansion.
    # No-op if the team is still pre-playing or already at the right dims.
    expanded = await _apply_expansion_if_needed(ctx.bot, team)
    suffix = " (board expanded)." if expanded else "."
    await send_feedback(
        ctx,
        f"✅ Added {member.mention} to team {team.team_number}{suffix}",
        persist=persist,
    )


@register_manage(
    WEEK, "forceRemove", tier=Tier.ADMIN,
    help="Remove a member from a team (thread + roster).",
    usage="<team> <@user>",
)
async def _manage_force_remove(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if len(args) < 2:
        await send_feedback(
            ctx, "Usage: `~manage_return 81 forceRemove <team> <@user>`", persist=persist
        )
        return
    team = await _resolve_team_by_id_or_number(args[0])
    if team is None:
        await send_feedback(ctx, f"No team matches `{args[0]}`.", persist=persist)
        return
    member, err = await _resolve_invitee(ctx.bot, args[1], CurrConfig.GUILD)
    if err:
        await send_feedback(ctx, err, persist=persist)
        return
    assert member is not None
    row = await BingoTeamMember.filter(team=team, disc_uuid=str(member.id)).first()
    if row is None:
        await send_feedback(
            ctx, f"{member.mention} isn't on this team.", persist=persist
        )
        return
    await row.delete()
    thread = await _resolve_thread(ctx.bot, team.thread_id)
    if thread is not None:
        try:
            await thread.remove_user(discord.Object(id=member.id))
        except (discord.NotFound, discord.HTTPException) as e:
            logger.debug("forceRemove remove_user: %s", e)
    remaining = await BingoTeamMember.filter(team=team).count()
    if remaining == 0:
        team.state = "disbanded"
        await team.save(update_fields=["state"])
        await send_feedback(
            ctx,
            f"✅ Removed {member.mention}; team {team.team_number} disbanded (empty).",
            persist=persist,
        )
        return
    await send_feedback(
        ctx, f"✅ Removed {member.mention} from team {team.team_number}.", persist=persist
    )


async def _mc_username_of(disc_id: int | str) -> Optional[str]:
    """MC username linked to this Discord id, or ``None`` if unlinked.
    Kept as its own helper because both the swap command and any future
    caller need the same DiscordAccount → MinecraftAccount lookup."""
    disc = await DiscordAccount.filter(disc_uuid=str(disc_id)).first()
    if disc is None or disc.minecraft_account_id is None:
        return None
    mc = await MinecraftAccount.get_or_none(id=disc.minecraft_account_id)
    return mc.mc_username if mc and mc.mc_username else None


@register_manage(
    WEEK, "swap", tier=Tier.STAFF,
    help=(
        "Swap an existing team member out for another player. Rewrites "
        "any placeholder captions that mentioned the outgoing member's MC "
        "username to the incoming member's MC username, then re-renders "
        "the dashboard."
    ),
    usage="<team> <@out> <@in>",
)
async def _manage_swap(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if len(args) < 3:
        await send_feedback(
            ctx,
            "Usage: `~manage_return 81 swap <team> <@out> <@in>`",
            persist=persist,
        )
        return
    team = await _resolve_team_by_id_or_number(args[0])
    if team is None:
        await send_feedback(ctx, f"No team matches `{args[0]}`.", persist=persist)
        return
    if team.state == "disbanded":
        await send_feedback(
            ctx, f"Team {team.team_number} is disbanded.", persist=persist
        )
        return

    out_member, err = await _resolve_invitee(ctx.bot, args[1], CurrConfig.GUILD)
    if err:
        await send_feedback(ctx, f"Outgoing: {err}", persist=persist)
        return
    assert out_member is not None
    in_member, err = await _resolve_invitee(ctx.bot, args[2], CurrConfig.GUILD)
    if err:
        await send_feedback(ctx, f"Incoming: {err}", persist=persist)
        return
    assert in_member is not None

    if out_member.id == in_member.id:
        await send_feedback(
            ctx, "The outgoing and incoming users must differ.", persist=persist
        )
        return

    out_row = await BingoTeamMember.filter(
        team=team, disc_uuid=str(out_member.id)
    ).first()
    if out_row is None:
        await send_feedback(
            ctx,
            f"{out_member.mention} isn't on team {team.team_number}.",
            persist=persist,
        )
        return
    if await BingoTeamMember.filter(
        team=team, disc_uuid=str(in_member.id)
    ).exists():
        await send_feedback(
            ctx,
            f"{in_member.mention} is already on team {team.team_number}.",
            persist=persist,
        )
        return
    other_team = await _current_team_for(str(in_member.id))
    if other_team is not None and other_team.id != team.id:
        await send_feedback(
            ctx,
            f"{in_member.mention} is already on a different r81 team "
            f"(#{other_team.team_number}). Resolve that first.",
            persist=persist,
        )
        return

    # Fetch MC usernames BEFORE the roster mutation so caption rewrites
    # see the correct name pair even if the DB rows shift underneath.
    out_mc = await _mc_username_of(out_member.id)
    in_mc = await _mc_username_of(in_member.id)

    # Roster mutation: preserve the outgoing role — the incoming player
    # inherits the slot. (Alternatively we could tag as "forced" to mark
    # the intervention, but preserving keeps the "how they got here"
    # semantic more useful — the incoming member replaces them structurally.)
    original_role = out_row.role
    await out_row.delete()
    await BingoTeamMember.create(
        team=team, disc_uuid=str(in_member.id), role=original_role
    )
    if team.creator_disc_uuid == str(out_member.id):
        team.creator_disc_uuid = str(in_member.id)
        await team.save(update_fields=["creator_disc_uuid"])

    # Thread membership: add first, then remove — reduces the window where
    # the team has one fewer visible member and matches the sweeper's
    # exempt rules (the incoming user is now in BingoTeamMember before
    # the next tick runs).
    thread = await _resolve_thread(ctx.bot, team.thread_id)
    if thread is not None:
        try:
            await thread.add_user(discord.Object(id=in_member.id))
        except discord.HTTPException as e:
            logger.warning("swap: add %s to thread failed: %s", in_member.id, e)
        try:
            await thread.remove_user(discord.Object(id=out_member.id))
        except (discord.NotFound, discord.HTTPException) as e:
            logger.debug("swap: remove %s from thread: %s", out_member.id, e)

    # Caption rewrite: word-boundary case-insensitive substitution of the
    # outgoing MC username with the incoming one across every cell.
    rewrites = 0
    if out_mc and in_mc and out_mc.lower() != in_mc.lower():
        pattern = re.compile(rf"\b{re.escape(out_mc)}\b", re.IGNORECASE)
        for row in await BingoCellState.filter(team=team):
            replaced = pattern.sub(in_mc, row.caption)
            if replaced != row.caption:
                row.caption = replaced
                await row.save(update_fields=["caption"])
                rewrites += 1

    # Any of the 4 posts could have been touched — full rerender.
    await _rerender_team_dashboard(ctx.bot, team)

    if thread is not None:
        try:
            await thread.send(
                f"🔄 {in_member.mention} has replaced {out_member.mention} on the team.",
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
        except discord.HTTPException as e:
            logger.warning("swap: thread notice send failed: %s", e)

    mc_note = (
        f"MC rename: `{out_mc}` → `{in_mc}`."
        if (out_mc and in_mc and out_mc.lower() != in_mc.lower())
        else "(No MC name change — captions untouched.)"
    )
    await send_feedback(
        ctx,
        f"✅ Team {team.team_number}: {out_member.mention} → {in_member.mention}. "
        f"{rewrites} caption(s) rewritten. {mc_note}",
        persist=persist,
    )
    logger.info(
        "r81 swap team=%s out=%s in=%s role=%s captions_rewritten=%d",
        team.team_number, out_member.id, in_member.id, original_role, rewrites,
    )


# --- Thread member sweep -----------------------------------------------------
#
# Anything that adds a user to a private thread (a stray @mention, a legacy
# bug from earlier in the picker flow, a manual `+add` by staff) can leave
# non-team members floating around. This sweep is the reconciler: the DB
# is the source of truth (BingoTeamMember), the Discord thread is the
# projection, and anyone in the projection but not the source gets kicked.

_SWEEP_INTERVAL = timedelta(minutes=10)
_last_sweep_at = datetime.min.replace(tzinfo=timezone.utc)

# Admin/strategist/operator users who have spoken in a specific team's
# thread. Once they've posted a message they're treated as "here by their
# own accord" and get exempted from sweeps — the reasoning is that an
# admin dragged in by an @-mention is silent by default (they never
# opened the thread), so speaking is the earliest reliable signal of
# intentional presence. Keyed by (str(team.id), str(disc_uuid)); cleared
# on restart (the admin just re-speaks once to re-exempt).
_ADMIN_SPOKE_IN_THREAD: set[tuple[str, str]] = set()


async def _track_admin_presence(message: discord.Message) -> None:
    """If a privileged user speaks in a team thread, mark them as
    intentionally present. The sweeper reads
    :data:`_ADMIN_SPOKE_IN_THREAD` to decide whom to exempt.
    """
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.Thread):
        return
    if not isinstance(message.author, discord.Member):
        return
    if not tier_allows(message.author, Tier.STAFF):
        return
    team = await BingoTeam.filter(thread_id=message.channel.id).first()
    if team is None or team.state == "disbanded":
        return
    key = (str(team.id), str(message.author.id))
    if key in _ADMIN_SPOKE_IN_THREAD:
        return
    _ADMIN_SPOKE_IN_THREAD.add(key)
    logger.info(
        "sweep: exempting privileged lurker %s in team %s thread %s (spoke)",
        message.author.id, team.team_number, message.channel.id,
    )


async def _sweep_team_threads(bot: discord.Client) -> tuple[int, int, int]:
    """Walk every non-disbanded team's thread and remove users who aren't
    in ``BingoTeamMember``. Returns ``(checked_threads, removed_users,
    unreachable_threads)``.

    Exemptions:
    * The bot itself is never removed.
    * A privileged user (STRATEGIST or higher) who has spoken in the
      thread this process lifetime — see :data:`_ADMIN_SPOKE_IN_THREAD` —
      is exempt. "Spoke" is the proxy for "here by their own accord",
      since Discord doesn't expose how a member was added (self-join vs
      auto-add-via-mention).

    ``NotFound`` on remove is treated as a no-op (user beat us to leaving).
    Runs sequentially across teams — parallelism isn't worth it for a
    periodic reconciler.
    """
    teams = await BingoTeam.exclude(state="disbanded")
    bot_id = bot.user.id if bot.user is not None else None
    guild = bot.get_guild(CurrConfig.GUILD)
    checked = removed = unreachable = 0
    for team in teams:
        if not team.thread_id:
            continue
        thread = await _resolve_thread(bot, team.thread_id)
        if thread is None:
            unreachable += 1
            continue
        checked += 1
        expected = set(
            await BingoTeamMember.filter(team=team).values_list("disc_uuid", flat=True)
        )
        try:
            members = await thread.fetch_members()
        except discord.HTTPException as e:
            logger.warning(
                "sweep: fetch_members thread=%s (team %s) failed: %s",
                thread.id, team.team_number, e,
            )
            continue
        for tm in members:
            if bot_id is not None and tm.id == bot_id:
                continue
            if str(tm.id) in expected:
                continue
            # Privileged-lurker exemption: only skips the remove if the
            # user has spoken in this thread AND is still privileged now
            # (re-check the role in case they were demoted since).
            if (str(team.id), str(tm.id)) in _ADMIN_SPOKE_IN_THREAD:
                gm = guild.get_member(tm.id) if guild is not None else None
                if gm is not None and tier_allows(gm, Tier.STRATEGIST):
                    continue
            try:
                await thread.remove_user(discord.Object(id=tm.id))
                removed += 1
                logger.info(
                    "sweep: removed %s from team %s thread %s",
                    tm.id, team.team_number, thread.id,
                )
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                logger.warning(
                    "sweep: remove_user(%s) team=%s failed: %s",
                    tm.id, team.team_number, e,
                )
    return checked, removed, unreachable


@register_tick(WEEK)
async def _sweep_tick(bot: discord.Client) -> None:
    """Shared 60s tick self-gate — actually sweeps once per
    :data:`_SWEEP_INTERVAL`. Idempotent and swallows its own exceptions
    so a bad state can't stop future ticks.
    """
    global _last_sweep_at
    now = datetime.now(timezone.utc)
    if now - _last_sweep_at < _SWEEP_INTERVAL:
        return
    _last_sweep_at = now
    try:
        checked, removed, unreachable = await _sweep_team_threads(bot)
        if removed or unreachable:
            logger.info(
                "r81 auto-sweep: checked=%d removed=%d unreachable=%d",
                checked, removed, unreachable,
            )
    except Exception:
        logger.exception("r81 auto-sweep raised")


@register_manage(
    WEEK, "sweep", tier=Tier.STAFF,
    help="Force a thread-member sweep now (removes anyone in a team thread who isn't on that team).",
    usage="",
)
async def _manage_sweep(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    checked, removed, unreachable = await _sweep_team_threads(ctx.bot)
    await send_feedback(
        ctx,
        f"✅ Swept {checked} thread(s): removed {removed} unauthorised user(s), "
        f"{unreachable} thread(s) unreachable.",
        persist=persist,
    )


async def _delete_pinned_dashboard(
    bot: discord.Client, team: BingoTeam
) -> tuple[int, int]:
    """Delete the status message + every pinned embed post. Returns (deleted, missing).

    Silently skips messages that are already gone. Doesn't clear the id
    fields on the team row — the caller is expected to call
    :func:`_post_pinned_dashboard` right after, which overwrites them
    with the fresh ids.
    """
    thread = await _resolve_thread(bot, team.thread_id)
    embed_ids = _parse_embed_msg_ids(team)
    if thread is None:
        return (0, 1 + len(embed_ids))
    deleted = 0
    missing = 0
    ids = [team.status_msg_id, *embed_ids]
    for msg_id in ids:
        if not msg_id:
            missing += 1
            continue
        try:
            msg = await thread.fetch_message(msg_id)
            await msg.delete()
            deleted += 1
        except discord.NotFound:
            missing += 1
        except discord.HTTPException as e:
            logger.warning("delete pinned msg %s failed: %s", msg_id, e)
    return (deleted, missing)


@register_manage(
    WEEK, "rerenderHard", tier=Tier.STAFF,
    help=(
        "Nuke and re-send the pinned dashboard (status + 4 embeds). Use "
        "if a Discord client is stuck showing a stale placeholder image "
        "that the normal `&_v=` cache-buster didn't beat."
    ),
    usage="<team>",
)
async def _manage_rerender_hard(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if not args:
        await send_feedback(
            ctx, "Usage: `~manage_return 81 rerenderHard <team>`", persist=persist
        )
        return
    team = await _resolve_team_by_id_or_number(args[0])
    if team is None:
        await send_feedback(ctx, f"No team matches `{args[0]}`.", persist=persist)
        return
    if team.state not in ("playing", "picking"):
        await send_feedback(
            ctx,
            f"Team {team.team_number} state is `{team.state}` — nothing pinned to "
            "rerender.",
            persist=persist,
        )
        return
    thread = await _resolve_thread(ctx.bot, team.thread_id)
    if thread is None:
        await send_feedback(ctx, "Team thread is unreachable.", persist=persist)
        return

    deleted, missing = await _delete_pinned_dashboard(ctx.bot, team)
    # _post_pinned_dashboard writes fresh msg ids into team and saves them,
    # so no manual null-out needed here.
    await _post_pinned_dashboard(thread, team)
    await send_feedback(
        ctx,
        f"✅ Rerendered team {team.team_number}: deleted {deleted} old message(s) "
        f"({missing} were already missing), sent + pinned 5 fresh ones.",
        persist=persist,
    )
    logger.info(
        "r81 rerenderHard team %s (#%d) by %s: deleted=%d missing=%d",
        team.id, team.team_number, ctx.author.id, deleted, missing,
    )


@register_manage(
    WEEK, "fakeTest", tier=Tier.STAFF,
    help=(
        "Create a solo test team with fake player names for renderer + "
        "submission testing. Skips invites and the wildcard pick."
    ),
    usage="[name1 name2 ...]  (defaults: Alice Bob Carol Dave)",
)
async def _manage_fake_test(ctx: commands.Context, args: list[str]) -> None:
    """Bring up a private thread with a live bingo card without the full
    invite/pick flow. Only the invoker is added as a real team member —
    submissions and scoring exercise the full listener + score path, but
    only for the tester. Caption ``%player%`` slots are filled from
    ``args`` (or a default four-name list) via :data:`_FAKE_TEAM_NAMES`,
    which short-circuits :func:`_team_player_names`.

    Note on visibility: private threads are visible to anyone with
    Manage Threads on the parent channel (typically staff/admins) — this
    is a Discord limitation, not something we can suppress. Only the
    invoker is *added* as a thread member, so casual users can't see it.
    """
    persist = is_persist_context(ctx)
    fake_names = [a for a in args if a] or ["Alice", "Bob", "Carol", "Dave"]

    parent = ctx.bot.get_channel(TEAMS_PARENT_CHANNEL_ID)
    if not isinstance(parent, discord.TextChannel):
        await send_feedback(
            ctx,
            f"TEAMS_PARENT_CHANNEL_ID={TEAMS_PARENT_CHANNEL_ID} isn't a "
            "TextChannel — check the config.",
            persist=persist,
        )
        return

    team_number = await _next_team_number()
    team = await BingoTeam.create(
        week=WEEK,
        team_number=team_number,
        creator_disc_uuid=str(ctx.author.id),
        state="playing",  # bypass pending/picking
    )
    _FAKE_TEAM_NAMES[str(team.id)] = list(fake_names)

    try:
        thread = await parent.create_thread(
            name=f"r81-Team-{team_number}-TEST",
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
    except discord.HTTPException as e:
        await team.delete()
        _FAKE_TEAM_NAMES.pop(str(team.id), None)
        await send_feedback(ctx, f"Couldn't create thread: {e}", persist=persist)
        return

    try:
        await thread.add_user(discord.Object(id=ctx.author.id))
    except discord.HTTPException as e:
        logger.warning("fakeTest add_user failed: %s", e)

    team.thread_id = thread.id
    await team.save(update_fields=["thread_id"])

    await BingoTeamMember.create(
        team=team, disc_uuid=str(ctx.author.id), role="creator"
    )

    await _seed_cell_captions(team)
    await _post_pinned_dashboard(thread, team)

    await thread.send(
        f"🧪 **Test team {team_number}** — fake players: "
        f"{', '.join(f'`{n}`' for n in fake_names)}.\n"
        + SUBMIT_COMMAND_HINT
        + "\nWhen you're done, `~manage_return 81 disband "
        f"{team_number}` will archive this thread."
    )

    await send_feedback(
        ctx,
        f"✅ Test team **#{team_number}** created in {thread.mention}. "
        f"Fake players: {', '.join(fake_names)}.",
        persist=persist,
    )
    logger.info(
        "r81 fakeTest team %s (#%d) created by %s with names=%s",
        team.id, team_number, ctx.author.id, fake_names,
    )


@register_manage(
    WEEK, "disband", tier=Tier.ADMIN,
    help="Disband a team: soft-delete + remove members from the thread.",
    usage="<team>",
)
async def _manage_disband(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if not args:
        await send_feedback(ctx, "Usage: `~manage_return 81 disband <team>`", persist=persist)
        return
    team = await _resolve_team_by_id_or_number(args[0])
    if team is None:
        await send_feedback(ctx, f"No team matches `{args[0]}`.", persist=persist)
        return
    if team.state == "disbanded":
        await send_feedback(ctx, "Already disbanded.", persist=persist)
        return
    thread = await _resolve_thread(ctx.bot, team.thread_id)
    members = await BingoTeamMember.filter(team=team)
    if thread is not None:
        for m in members:
            try:
                await thread.remove_user(discord.Object(id=int(m.disc_uuid)))
            except (discord.NotFound, discord.HTTPException, ValueError):
                pass
        try:
            await thread.edit(archived=True, locked=True)
        except discord.HTTPException as e:
            logger.warning("disband archive failed: %s", e)
    team.state = "disbanded"
    await team.save(update_fields=["state"])
    await send_feedback(
        ctx, f"✅ Team {team.team_number} disbanded.", persist=persist
    )

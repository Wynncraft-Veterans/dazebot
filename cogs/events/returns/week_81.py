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
   seeds 16 ``BingoCellState`` captions and pins the 5-message dashboard
   (status grid + four 2x2 embeds). Team goes into ``playing``.
5. Team members post ``~return 81 submit <cell>`` (e.g.
   ``~return 81 submit A1``) with a photo attachment in the team thread.
   The attachment is what gets submitted; the ``on_message`` listener
   validates + upserts a ``BingoSubmission``, edits the relevant embed to
   show the photo, edits the status post to mark the cell filled, awards
   ``POINTS_PER_CELL`` via ``WeeklyEvent(week=81)`` / ``Score`` to every
   current team member, and detects any newly-completed bingo lines. Each
   new line posts a ``BingoBonusView`` in the thread offering an extra
   card (more slots) or an extra teammate. The ``handle`` dispatcher
   guards against firing the invite flow when the same
   ``~return 81 submit <cell>`` shape shows up as a prefix command — the
   listener owns that path.

Staff surfaces live under ``~manage_return 81 <subcommand>``. See the
``@register_manage`` decorators near the bottom.

Only Discord CDN URLs are stored (see :func:`_on_message_submit`), matching
``cogs/rewards/donations/donations.py``'s approach. There is no re-upload
step; the source message must not be deleted for the embed to keep
rendering.
"""

from __future__ import annotations

import json
import logging
import random
import re
from typing import Optional

import discord
from discord.ext import commands

from cogs.events.returns import (
    Tier,
    register,
    register_manage,
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

# 4x4 grid cells, row-major.
BINGO_CELLS: tuple[str, ...] = tuple(
    f"{row}{col}" for row in "ABCD" for col in "1234"
)

# 10 winning lines: 4 rows + 4 cols + 2 diagonals.
BINGO_LINES: tuple[tuple[str, ...], ...] = (
    ("A1", "A2", "A3", "A4"),
    ("B1", "B2", "B3", "B4"),
    ("C1", "C2", "C3", "C4"),
    ("D1", "D2", "D3", "D4"),
    ("A1", "B1", "C1", "D1"),
    ("A2", "B2", "C2", "D2"),
    ("A3", "B3", "C3", "D3"),
    ("A4", "B4", "C4", "D4"),
    ("A1", "B2", "C3", "D4"),
    ("A4", "B3", "C2", "D1"),
)

# Which of the four 2x2 embed posts (1..4) hosts each cell. Spec split:
#   post 1: A1 A2 / B1 B2
#   post 2: A3 A4 / B3 B4
#   post 3: C1 C2 / D1 D2
#   post 4: C3 C4 / D3 D4
_POST_LAYOUTS: tuple[tuple[tuple[str, str], tuple[str, str]], ...] = (
    (("A1", "A2"), ("B1", "B2")),
    (("A3", "A4"), ("B3", "B4")),
    (("C1", "C2"), ("D1", "D2")),
    (("C3", "C4"), ("D3", "D4")),
)
CELL_TO_POST_INDEX: dict[str, int] = {
    cell: i + 1
    for i, layout in enumerate(_POST_LAYOUTS)
    for row in layout
    for cell in row
}

# Placeholder image used before a slot has been submitted. Discord doesn't
# render a broken URL well; a 1x1 transparent PNG hosted somewhere stable is
# ideal. Overridable at runtime via ``~manage_return 81`` if needed.
PLACEHOLDER_IMAGE_URL = "https://raw.githubusercontent.com/Wynncraft-Veterans/dazebot/master/assets/r81_placeholder.png"

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
    r"^\s*~return\s+81\s+submit\s+([A-Da-d][1-4])\s*$",
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

    lower = val.lower()
    for cand in guild.members:
        if (
            cand.name.lower() == lower
            or (cand.global_name or "").lower() == lower
            or cand.display_name.lower() == lower
        ):
            return cand, None

    return None, (
        f"`{val}` doesn't match anyone on this server. Try a Discord "
        "@mention or a full username."
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
) -> list[discord.Member]:
    """Draw up to :data:`PICKER_SIZE` random guild members eligible for the
    r81 wildcard slot.

    Pool: Wynncraft in-game guild ``Returners`` MC accounts that are linked
    to a Discord account whose guild member holds one of :data:`TEAM_ROLE_IDS`.
    Excludes any user already on any (non-disbanded) r81 team.
    """
    guild = client.get_guild(CurrConfig.GUILD)
    if guild is None:
        return []

    mc_ids = list(
        await MinecraftAccount.filter(guild=WYNN_GUILD_NAME).values_list("id", flat=True)
    )
    if not mc_ids:
        return []

    excluded_from_teams = set(
        await BingoTeamMember.filter().values_list("disc_uuid", flat=True)
    )
    excluded = exclude_disc_uuids | excluded_from_teams

    # Walk DiscordAccount → MinecraftAccount rather than the reverse: the
    # reverse relation on ``MinecraftAccount`` is a queryset, not a single
    # object, and OneToOne reverse traversal here needs a separate query
    # anyway.
    discs = await DiscordAccount.filter(minecraft_account_id__in=mc_ids)

    candidates: list[discord.Member] = []
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
        seen.add(disc_id)
        candidates.append(member)

    random.shuffle(candidates)
    return candidates[:PICKER_SIZE]


# ---------------------------------------------------------------------------
# Bingo detection
# ---------------------------------------------------------------------------


def _line_key(line: tuple[str, ...]) -> str:
    return "|".join(line)


def _detect_new_bingos(
    filled_before: set[str], filled_after: set[str]
) -> list[tuple[str, ...]]:
    new: list[tuple[str, ...]] = []
    for line in BINGO_LINES:
        cells = set(line)
        if cells <= filled_after and not (cells <= filled_before):
            new.append(line)
    return new


# ---------------------------------------------------------------------------
# Caption + image stubs
# ---------------------------------------------------------------------------


def _placeholder_image_url() -> str:
    return PLACEHOLDER_IMAGE_URL


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


def _status_text(filled: set[str]) -> str:
    lines = []
    for row in "ABCD":
        cells = []
        for col in "1234":
            key = f"{row}{col}"
            cells.append("[✅]" if key in filled else f"[{key}]")
        lines.append("".join(cells))
    return (
        "**Return 81 — Team Bingo**\n"
        + "```\n" + "\n".join(lines) + "\n```\n"
        + SUBMIT_COMMAND_HINT
    )


def _embed_for_post(
    post_index: int,
    *,
    captions: dict[str, str],
    submissions: dict[str, str],
) -> discord.Embed:
    """Render one of the four 2x2 embed posts.

    Uses one field per cell; each field's value is the cell caption.
    ``image`` on the embed can only hold one URL, so we use the top-level
    ``image`` for the first submitted cell of the post (if any) and inline
    URLs as field values for the rest. This keeps the display readable
    without spawning 4 embeds per post.
    """
    layout = _POST_LAYOUTS[post_index - 1]
    header_cells = " · ".join(cell for row in layout for cell in row)
    embed = discord.Embed(
        title=f"Return 81 — {header_cells}",
        description=SUBMIT_COMMAND_HINT,
    )
    for row in layout:
        for cell in row:
            caption = captions.get(cell, "(missing caption)")
            submitted = submissions.get(cell)
            if submitted:
                embed.add_field(
                    name=f"{cell} ✅",
                    value=f"[photo]({submitted})\n_{caption}_",
                    inline=True,
                )
            else:
                embed.add_field(
                    name=f"{cell}",
                    value=f"_{caption}_",
                    inline=True,
                )
    # Use the first submitted cell's photo as the embed's large image so
    # at least one photo renders inline. Falls back to the placeholder.
    hero: Optional[str] = None
    for row in layout:
        for cell in row:
            if cell in submissions:
                hero = submissions[cell]
                break
        if hero is not None:
            break
    embed.set_image(url=hero or _placeholder_image_url())
    return embed


async def _load_captions(team: BingoTeam) -> dict[str, str]:
    rows = await BingoCellState.filter(team=team)
    return {r.cell: r.caption for r in rows}


async def _load_submissions(team: BingoTeam) -> dict[str, str]:
    rows = await BingoSubmission.filter(team=team)
    return {r.cell: r.image_url for r in rows}


async def _rerender_team_dashboard(
    bot: discord.Client, team: BingoTeam, *, only_post: Optional[int] = None
) -> None:
    """Recompute the status message and the four embed posts from DB state.

    If ``only_post`` is set, only that post index (1..4) is edited; the
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
            await msg.edit(content=_status_text(set(submissions.keys())))
        except (discord.NotFound, discord.HTTPException) as e:
            logger.warning("rerender status edit failed: %s", e)

    for i in range(1, 5):
        if only_post is not None and i != only_post:
            continue
        msg_id = getattr(team, f"embed_msg_{i}_id", None)
        if not msg_id:
            continue
        try:
            msg = await thread.fetch_message(msg_id)
            await msg.edit(
                embed=_embed_for_post(
                    i, captions=captions, submissions=submissions
                )
            )
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


class BingoBonusView(discord.ui.View):
    """Persistent view for the post-bingo "extra card / extra teammate"
    prompt. Idempotent: once a ``BingoBingoEvent.bonus_choice`` is set,
    further clicks reply with "already claimed".
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Extra card",
        style=discord.ButtonStyle.primary,
        custom_id="r81:bonus:extra_card",
    )
    async def extra_card(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_bonus_click(interaction, "extra_card")

    @discord.ui.button(
        label="Extra teammate",
        style=discord.ButtonStyle.secondary,
        custom_id="r81:bonus:extra_member",
    )
    async def extra_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_bonus_click(interaction, "extra_member")


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
    )

    team.state = "picking"
    team.thread_id = thread.id
    team.picker_msg_id = picker_msg.id
    team.picker_candidates_json = json.dumps(
        [str(m.id) for m in candidates]
    )
    await team.save(update_fields=[
        "state", "thread_id", "picker_msg_id", "picker_candidates_json"
    ])
    logger.info(
        "r81 team %s advanced to picking with %d candidates (thread %s)",
        team.id, len(candidates), thread.id,
    )


def _picker_intro(candidates: list[discord.Member]) -> str:
    lines = ["🎯 **Pick your wildcard teammate.** First click wins."]
    for i, m in enumerate(candidates, start=1):
        lines.append(f"{i}. {m.mention}")
    return "\n".join(lines)


def _render_picker_view(candidates: list[discord.Member]) -> discord.ui.View:
    """Build the per-team picker view, overriding button labels to show the
    candidate name. The persistent-view class handles dispatch; this view
    exists to render friendly button labels at message-send time.
    """
    view = BingoRandomPickView()
    for i, item in enumerate(view.children):
        if isinstance(item, _PickButton) and i < len(candidates):
            item.label = candidates[i].display_name[:80]
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
    if team.state != "picking":
        await interaction.response.send_message(
            "This team already has its wildcard.", ephemeral=True
        )
        return
    # Only current team members may pick.
    is_member = await BingoTeamMember.filter(
        team=team, disc_uuid=str(interaction.user.id)
    ).exists()
    if not is_member:
        await interaction.response.send_message(
            "Only the team can pick the wildcard.", ephemeral=True
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

    # Seed cell captions and post the dashboard.
    await _seed_cell_captions(team)
    if thread is not None:
        await _post_pinned_dashboard(thread, team)

    team.state = "playing"
    await team.save(update_fields=["state"])

    # Update the picker message to reflect the completed pick.
    try:
        if interaction.message is not None:
            await interaction.message.edit(
                content=(
                    interaction.message.content
                    + f"\n\n_🎯 <@{picked_disc_uuid}> joined as the wildcard._"
                ),
                view=None,
            )
    except discord.HTTPException:
        pass
    logger.info(
        "r81 team %s wildcard resolved to %s (slot %s)",
        team.id, picked_disc_uuid, slot,
    )


async def _seed_cell_captions(team: BingoTeam) -> None:
    for cell in BINGO_CELLS:
        if await BingoCellState.filter(team=team, cell=cell).exists():
            continue
        caption = await _generate_cell_caption(team)
        await BingoCellState.create(team=team, cell=cell, caption=caption)


async def _post_pinned_dashboard(thread: discord.Thread, team: BingoTeam) -> None:
    captions = await _load_captions(team)
    submissions = await _load_submissions(team)  # empty at this point

    status_msg = await thread.send(_status_text(set(submissions.keys())))
    try:
        await status_msg.pin()
    except discord.HTTPException as e:
        logger.warning("r81 pin status failed: %s", e)
    team.status_msg_id = status_msg.id

    for i in range(1, 5):
        msg = await thread.send(
            embed=_embed_for_post(i, captions=captions, submissions=submissions)
        )
        try:
            await msg.pin()
        except discord.HTTPException as e:
            logger.warning("r81 pin embed %s failed: %s", i, e)
        setattr(team, f"embed_msg_{i}_id", msg.id)

    await team.save(update_fields=[
        "status_msg_id",
        "embed_msg_1_id",
        "embed_msg_2_id",
        "embed_msg_3_id",
        "embed_msg_4_id",
    ])


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
    if cell not in BINGO_CELLS:
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

    await _rerender_team_dashboard(
        bot, team, only_post=CELL_TO_POST_INDEX[cell]
    )

    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass

    new_lines = _detect_new_bingos(filled_before, filled_after)
    for line in new_lines:
        await _announce_bingo(message.channel, team, line)


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


async def _announce_bingo(
    thread: discord.Thread, team: BingoTeam, line: tuple[str, ...]
) -> None:
    key = _line_key(line)
    exists = await BingoBingoEvent.filter(team=team, line_key=key).first()
    if exists is not None:
        return
    event = await BingoBingoEvent.create(team=team, line_key=key)
    text = (
        f"🎉 **Bingo!** Line: `{' → '.join(line)}`\n"
        "Pick a bonus:\n"
        "- **Extra card** — a second 4x4 for more points\n"
        "- **Extra teammate** — expand the roster and split the load"
    )
    msg = await thread.send(text, view=BingoBonusView())
    event.bonus_msg_id = msg.id
    await event.save(update_fields=["bonus_msg_id"])


# ---------------------------------------------------------------------------
# Bonus click
# ---------------------------------------------------------------------------


async def _handle_bonus_click(
    interaction: discord.Interaction, choice: str
) -> None:
    msg_id = interaction.message.id if interaction.message else None
    if msg_id is None:
        await interaction.response.send_message(
            "Couldn't identify this bonus post.", ephemeral=True
        )
        return
    event = await BingoBingoEvent.filter(bonus_msg_id=msg_id).prefetch_related("team").first()
    if event is None:
        await interaction.response.send_message(
            "This bonus post is no longer active.", ephemeral=True
        )
        return
    if event.bonus_choice is not None:
        await interaction.response.send_message(
            f"Already claimed: `{event.bonus_choice}`.", ephemeral=True
        )
        return
    is_member = await BingoTeamMember.filter(
        team=event.team, disc_uuid=str(interaction.user.id)
    ).exists()
    if not is_member:
        await interaction.response.send_message(
            "Only team members can claim this.", ephemeral=True
        )
        return

    event.bonus_choice = choice
    await event.save(update_fields=["bonus_choice"])

    if choice == "extra_card":
        # Placeholder: reroll all captions for empty cells so the team has
        # "fresh" prompts to keep working from. A dedicated second-card
        # surface can replace this later.
        await _reroll_empty_captions(event.team)
        await _rerender_team_dashboard(interaction.client, event.team)
        followup = (
            "✅ Extra card claimed — empty slots have fresh captions."
        )
    else:  # extra_member
        candidates = await _wildcard_candidates_excluding_team(
            interaction.client, event.team
        )
        if not candidates:
            followup = (
                "No eligible extra teammates are available right now."
            )
        else:
            chosen = random.choice(candidates)
            await BingoTeamMember.create(
                team=event.team,
                disc_uuid=str(chosen.id),
                role="wildcard",
            )
            thread = await _resolve_thread(interaction.client, event.team.thread_id)
            if thread is not None:
                try:
                    await thread.add_user(discord.Object(id=chosen.id))
                except discord.HTTPException as e:
                    logger.warning("r81 extra_member add_user failed: %s", e)
            followup = f"✅ Extra teammate: {chosen.mention} joined."

    await interaction.response.send_message(followup, ephemeral=False)
    try:
        if interaction.message is not None:
            await interaction.message.edit(view=None)
    except discord.HTTPException:
        pass


async def _reroll_empty_captions(team: BingoTeam) -> None:
    filled = set(await BingoSubmission.filter(team=team).values_list("cell", flat=True))
    for cell in BINGO_CELLS:
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
) -> list[discord.Member]:
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
        thread = f"<#{t.thread_id}>" if t.thread_id else "(no thread)"
        lines.append(
            f"- **Team {t.team_number}** — state=`{t.state}` "
            f"members={members} cells={subs}/16 bingos={bingos} {thread} "
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
    lines = [
        f"**Team {team.team_number}** (state=`{team.state}`, id=`{team.id}`)",
        f"Thread: <#{team.thread_id}>" if team.thread_id else "Thread: (none)",
        "Members:",
    ]
    for m in members:
        lines.append(f"  - <@{m.disc_uuid}> ({m.role})")
    lines.append("Cells filled:")
    for row in "ABCD":
        row_cells = " ".join(
            "✅" if f"{row}{col}" in filled else "·" for col in "1234"
        )
        lines.append(f"  {row}: {row_cells}")
    if bingos:
        lines.append("Bingos:")
        for b in bingos:
            claim = f" → {b.bonus_choice}" if b.bonus_choice else ""
            lines.append(f"  - `{b.line_key}`{claim}")
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
    if cell not in BINGO_CELLS:
        await send_feedback(ctx, f"`{cell}` isn't a valid cell (A1..D4).", persist=persist)
        return
    submission = await BingoSubmission.filter(team=team, cell=cell).first()
    if submission is not None:
        await submission.delete()
        # Subtract the points we awarded for this cell to every team member.
        await _award_score(team, -POINTS_PER_CELL)
    cell_row = await BingoCellState.filter(team=team, cell=cell).first()
    new_caption = await _generate_cell_caption(team)
    if cell_row is None:
        await BingoCellState.create(team=team, cell=cell, caption=new_caption)
    else:
        cell_row.caption = new_caption
        await cell_row.save(update_fields=["caption"])
    await _rerender_team_dashboard(
        ctx.bot, team, only_post=CELL_TO_POST_INDEX[cell]
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
    await submission.delete()
    await _award_score(team, -POINTS_PER_CELL)
    await _rerender_team_dashboard(
        ctx.bot, team, only_post=CELL_TO_POST_INDEX[cell]
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
            await old.edit(content=old.content + "\n_(replaced)_", view=None)
        except discord.HTTPException:
            pass
    picker_msg = await thread.send(
        content=_picker_intro(candidates),
        view=_render_picker_view(candidates),
    )
    team.picker_msg_id = picker_msg.id
    team.picker_candidates_json = json.dumps([str(m.id) for m in candidates])
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
    await send_feedback(
        ctx, f"✅ Added {member.mention} to team {team.team_number}.", persist=persist
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

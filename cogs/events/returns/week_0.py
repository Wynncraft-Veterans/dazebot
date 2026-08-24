"""Week 0: cults.

User-facing UI is a single ``/return 0`` ephemeral that adapts to the
caller's state (no cult / in a cult / figurehead) and presents the
appropriate buttons. Cult management is exposed via ``~manage_return 0
<subcommand>`` — see the ``@register_manage`` decorators at the bottom of
this file.

Cult ↔ thread mapping is stored in ``Cult.thread_id`` (a Discord channel
id). The legacy ``cogs.events.returns.lib.cult_threads.CULT_THREADS`` dict
is consulted only at startup by :func:`backfill_thread_ids_from_dict` to
seed the new column for the six grandfathered cults.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands

from config import CurrConfig
from cogs.events.returns import Tier, register, register_manage
from cogs.events.returns._common import (
    is_persist_context,
    send_feedback,
)
from cogs.events.returns.lib.cults import is_figurehead, lookup_owned_cult
from cogs.events.returns.lib.cult_threads import (
    CULT_THREADS,  # legacy seed source, used only by backfill
)
from lib.discord_utils.converters import CaseInsensitiveMember
from lib.mc.resolve import ensure_mc_account
from lib.mc.wynn_api.errors import WynnApiError
from lib.role_state import resolve_guild_member
from orm import (
    Cult,
    CultMembership,
    DiscordAccount,
    MinecraftAccount,
)

logger = logging.getLogger("dazebot.cogs.events.returns.week_0")


# Self-switch gate: a user can move themselves between cults at most once
# every SELF_SWITCH_COOLDOWN. Force-moves by staff bypass this entirely.
SELF_SWITCH_COOLDOWN = timedelta(days=180)

# `cultDistribution` activity window. Time-bounded (not message-capped) so
# quiet threads finish quickly; the bot's event loop stays responsive
# between history-pagination batches.
ACTIVITY_LOOKBACK = timedelta(days=14)

# Distinct fill characters for pie slices. Order is stable so the same cult
# index gets the same glyph in both charts (one legend covers both).
_PIE_CHARS = "█▓▒░#@*+"


# ---------------------------------------------------------------------------
# Thread helpers — operate on a Cult row's thread_id
# ---------------------------------------------------------------------------


async def _resolve_thread(bot, thread_id: Optional[int]) -> Optional[discord.Thread]:
    if not thread_id:
        return None
    ch = bot.get_channel(thread_id)
    if isinstance(ch, discord.Thread):
        return ch
    try:
        ch = await bot.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logger.warning("cult thread %s not fetchable: %s", thread_id, e)
        return None
    return ch if isinstance(ch, discord.Thread) else None


async def _add_to_cult_thread(bot, cult: Cult, discord_id: int) -> None:
    if not cult.thread_id:
        logger.debug("no thread_id set for cult %r; skipping add", cult.name)
        return
    thread = await _resolve_thread(bot, cult.thread_id)
    if thread is None:
        return
    try:
        await thread.add_user(discord.Object(id=discord_id))
    except discord.HTTPException as e:
        logger.warning("add_user(%s) on thread %s failed: %s", discord_id, cult.thread_id, e)
        return
    try:
        await thread.send(
            f"<@{discord_id}> joined {cult.name}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    except discord.HTTPException as e:
        logger.warning("welcome msg in thread %s failed: %s", cult.thread_id, e)


async def _remove_from_cult_thread(bot, cult: Cult, discord_id: int) -> None:
    if not cult.thread_id:
        return
    thread = await _resolve_thread(bot, cult.thread_id)
    if thread is None:
        return
    try:
        await thread.remove_user(discord.Object(id=discord_id))
    except discord.HTTPException as e:
        # 404 here is normal (user wasn't in the thread).
        if isinstance(e, discord.NotFound):
            logger.debug(
                "remove_user(%s) on thread %s: not a member", discord_id, cult.thread_id
            )
        else:
            logger.warning(
                "remove_user(%s) on thread %s failed: %s", discord_id, cult.thread_id, e
            )


# ---------------------------------------------------------------------------
# Gateway: keep figureheads out of their own cult thread at runtime
# ---------------------------------------------------------------------------


async def _on_thread_member_join_sweep_figurehead(tm: discord.ThreadMember) -> None:
    """Sweep a figurehead back out if they get added to their own cult's
    thread (typically by being @mentioned — Discord auto-invites mentioned
    users into private threads). Mirrors the boot-time sweep in
    :func:`backfill_cult_threads` for the live case.
    """
    cult = await Cult.filter(thread_id=tm.thread_id).first()
    if cult is None:
        return
    disc = await DiscordAccount.filter(disc_uuid=str(tm.id)).first()
    if disc is None or disc.minecraft_account_id is None:
        return
    if disc.minecraft_account_id != cult.owner_id:
        return
    thread = tm.thread
    if thread is None:
        return
    try:
        await thread.remove_user(discord.Object(id=tm.id))
        logger.info(
            "swept figurehead %s out of own cult thread %s (%s)",
            tm.id, tm.thread_id, cult.name,
        )
    except discord.NotFound:
        pass
    except discord.HTTPException as e:
        logger.warning(
            "figurehead sweep remove(%s, %s) failed: %s", tm.id, cult.name, e,
        )


def register_listeners(bot: commands.Bot) -> None:
    bot.add_listener(
        _on_thread_member_join_sweep_figurehead, name="on_thread_member_join"
    )


# ---------------------------------------------------------------------------
# Backfill (called from Returns.cog_load)
# ---------------------------------------------------------------------------


async def backfill_thread_ids_from_dict() -> int:
    """Populate ``Cult.thread_id`` from the legacy CULT_THREADS map.

    Idempotent: rows that already have a thread_id are skipped. Returns the
    count of rows updated. The six grandfathered cults flow through this on
    first deploy of the column.
    """
    updated = 0
    rows = await Cult.filter(thread_id__isnull=True)
    for cult in rows:
        legacy = CULT_THREADS.get(cult.name.lower())
        if legacy is None:
            continue
        cult.thread_id = legacy
        await cult.save(update_fields=["thread_id"])
        updated += 1
        logger.info("backfilled thread_id=%s for cult %r", legacy, cult.name)
    return updated


async def backfill_cult_threads(bot) -> None:
    """Walk every CultMembership row and ensure each member is in their cult's
    thread. Idempotent. Excludes figureheads from every cult thread (any
    stale CultMembership row whose owner is a figurehead is deleted, and
    each figurehead is swept out of every cult thread).
    """
    figurehead_mc_ids = set(await Cult.all().values_list("owner_id", flat=True))

    cults = await Cult.all()
    # Snapshot each thread's current membership once (the thread-members
    # rate-limit bucket is per-channel — blind PUTs/DELETEs ate 429s before).
    thread_state: dict[int, tuple[Optional[discord.Thread], set[int], Cult]] = {}
    for cult in cults:
        if not cult.thread_id:
            thread_state[cult.id] = (None, set(), cult)
            continue
        thread = await _resolve_thread(bot, cult.thread_id)
        if thread is None:
            thread_state[cult.id] = (None, set(), cult)
            continue
        try:
            members = await thread.fetch_members()
            thread_state[cult.id] = (thread, {tm.id for tm in members}, cult)
        except discord.HTTPException as e:
            logger.warning(
                "backfill: fetch_members on thread %s (%s) failed: %s",
                cult.thread_id, cult.name, e,
            )
            thread_state[cult.id] = (thread, set(), cult)

    rows = await CultMembership.all().prefetch_related("cult", "discord_account")
    added = failed = skipped = pruned = already = 0
    for m in rows:
        mc_id = m.discord_account.minecraft_account_id
        if mc_id is not None and mc_id in figurehead_mc_ids:
            await m.delete()
            pruned += 1
            continue
        try:
            disc_id = int(m.discord_account.disc_uuid)
        except ValueError:
            skipped += 1
            continue
        state = thread_state.get(m.cult_id)
        if state is None:
            skipped += 1
            continue
        thread, members, _cult = state
        if thread is None:
            failed += 1
            continue
        if thread.guild.get_member(disc_id) is None:
            skipped += 1
            continue
        if disc_id in members:
            already += 1
            continue
        try:
            await thread.add_user(discord.Object(id=disc_id))
            members.add(disc_id)
            added += 1
        except discord.HTTPException as e:
            logger.warning(
                "backfill: add_user(%s) on thread %s (%s) failed: %s",
                disc_id, thread.id, m.cult.name, e,
            )
            failed += 1

    swept = 0
    if figurehead_mc_ids:
        figurehead_discs = await DiscordAccount.filter(
            minecraft_account_id__in=list(figurehead_mc_ids)
        )
        figurehead_disc_ids: set[int] = set()
        for disc in figurehead_discs:
            try:
                figurehead_disc_ids.add(int(disc.disc_uuid))
            except ValueError:
                continue
        for (thread, members, cult) in thread_state.values():
            if thread is None:
                continue
            for fdid in figurehead_disc_ids & members:
                try:
                    await thread.remove_user(discord.Object(id=fdid))
                    members.discard(fdid)
                    swept += 1
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    logger.debug(
                        "figurehead sweep remove(%s, %s) failed: %s", fdid, cult.name, e,
                    )

    logger.info(
        "cult thread backfill: total=%d added=%d already=%d failed=%d skipped=%d "
        "pruned_figureheads=%d swept_figurehead_thread_memberships=%d",
        len(rows), added, already, failed, skipped, pruned, swept,
    )


# ---------------------------------------------------------------------------
# Owner / MC resolution (reused by createCult)
# ---------------------------------------------------------------------------


async def _resolve_owner_mc(ctx: commands.Context, owner: str) -> Optional[MinecraftAccount]:
    """Discord mention/id/name → linked MC account, then MC username/UUID
    lookup, then Wynncraft API fetch (creating the row) for unknown names.
    """
    if ctx.guild is not None:
        try:
            member = await CaseInsensitiveMember().convert(ctx, owner)
        except commands.MemberNotFound:
            member = None
        if member is not None:
            disc = await DiscordAccount.filter(disc_uuid=str(member.id)).first()
            if disc is None or disc.minecraft_account_id is None:
                return None
            return await MinecraftAccount.get(id=disc.minecraft_account_id)

    # Delegate the local-lookup-then-create half to the shared helper. This
    # used to be an inlined copy of it, which meant it also carried the
    # rename bug ensure_mc_account now handles: a name-keyed miss on a
    # renamed player fell through to a create that violated the uuid
    # UNIQUE constraint.
    try:
        return await ensure_mc_account(owner)
    except WynnApiError as e:
        logger.info("week_0 owner resolution: Wynn API lookup failed for %r: %s", owner, e)
        return None


# ---------------------------------------------------------------------------
# Join / switch core
# ---------------------------------------------------------------------------


async def _switch_membership(
    bot,
    disc: DiscordAccount,
    discord_id: int,
    cult: Cult,
    existing: Optional[CultMembership],
) -> Optional[Cult]:
    prev_cult: Optional[Cult] = None
    if existing is not None:
        prev_cult = await Cult.get(id=existing.cult_id)
        await existing.delete()
    await CultMembership.create(cult=cult, discord_account=disc)
    if prev_cult and prev_cult.id != cult.id:
        await _remove_from_cult_thread(bot, prev_cult, discord_id)
    await _add_to_cult_thread(bot, cult, discord_id)
    return prev_cult


async def _self_join(bot, user_id: int, cult: Cult) -> tuple[bool, str]:
    """User-driven join from ``/return 0``. Returns ``(ok, message_to_user)``."""
    disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(user_id))
    if disc.minecraft_account_id is not None:
        owned = await Cult.filter(owner_id=disc.minecraft_account_id).first()
        if owned is not None:
            return False, (
                f"You're the figurehead of `{owned.name}` — figureheads can't "
                "join cult threads."
            )
    existing = await CultMembership.filter(discord_account=disc).first()
    if existing is not None and existing.cult_id == cult.id:
        return False, f"You're already in `{cult.name}`."
    if existing is not None:
        elapsed = datetime.now(timezone.utc) - existing.joined_at
        if elapsed < SELF_SWITCH_COOLDOWN:
            remaining = SELF_SWITCH_COOLDOWN - elapsed
            days = max(1, remaining.days + (1 if remaining.seconds else 0))
            return False, (
                f"You can switch cults again in ~{days} day(s). "
                "Ask staff for a force-move if you need it sooner."
            )
    await _switch_membership(bot, disc, user_id, cult, existing)
    return True, f"✅ Joined `{cult.name}`."


async def _force_move(bot, target_id: int, cult: Cult) -> tuple[bool, str]:
    """Staff-driven force-move. Returns ``(ok, message_to_invoker)``."""
    disc, _ = await DiscordAccount.get_or_create(disc_uuid=str(target_id))
    if disc.minecraft_account_id is not None:
        owned = await Cult.filter(owner_id=disc.minecraft_account_id).first()
        if owned is not None:
            return False, (
                f"<@{target_id}> is the figurehead of `{owned.name}`; "
                "figureheads can't be placed in any cult's thread."
            )
    existing = await CultMembership.filter(discord_account=disc).first()
    if existing is not None and existing.cult_id == cult.id:
        return False, f"<@{target_id}> is already in `{cult.name}`."
    prev = await _switch_membership(bot, disc, target_id, cult, existing)
    note = f"from `{prev.name}` " if prev else ""
    return True, f"✅ Force-moved <@{target_id}> {note}to `{cult.name}`."


# ---------------------------------------------------------------------------
# Listing helpers (shared by the user button and the manage subcommand)
# ---------------------------------------------------------------------------


async def _username_for_disc(bot, disc: DiscordAccount) -> str:
    if disc.minecraft_account_id is not None:
        mc = await MinecraftAccount.get(id=disc.minecraft_account_id)
        return mc.mc_username
    try:
        user = await bot.fetch_user(int(disc.disc_uuid))
        return f"@{user.name} (unlinked)"
    except (ValueError, discord.HTTPException):
        return f"<{disc.disc_uuid}> (unlinked)"


async def _render_cult_listing(bot, cult: Cult) -> str:
    """Format a cult's figurehead/staff/members listing."""
    memberships = await CultMembership.filter(cult=cult).prefetch_related("discord_account")
    staff_names: list[str] = []
    member_names: list[str] = []
    staff_role_id = CurrConfig.STAFF_ROLE
    for m in memberships:
        disc = m.discord_account
        username = await _username_for_disc(bot, disc)
        # resolve_guild_member falls back to a REST fetch on cache miss; a
        # plain guild.get_member() silently treats uncached members (common
        # for users who don't actively chat) as if they had no roles.
        member = await resolve_guild_member(bot, disc.disc_uuid)
        is_staff = member is not None and any(r.id == staff_role_id for r in member.roles)
        (staff_names if is_staff else member_names).append(username)

    figurehead = cult.owner.mc_username

    def fmt(names: list[str]) -> str:
        return ", ".join(f"`{n}`" for n in names) if names else "(none)"

    return "\n".join([
        f"# {cult.name.capitalize()} Members:",
        f"Figurehead: `{figurehead}`",
        f"Staff: {fmt(staff_names)}",
        f"Members: {fmt(member_names)}",
    ])


# ---------------------------------------------------------------------------
# Views — /return 0 button-based UX
# ---------------------------------------------------------------------------


_CULT_DESCRIPTION = (
    "Cults are silly clubs that have developed in secret around the guild's chiefs "
    "without their knowledge. The chiefs don't know what these groups get up to, "
    "but they have hideouts, compete with each other, have their own traditions, etc."
)


def _switch_remaining_days(membership: CultMembership) -> int:
    elapsed = datetime.now(timezone.utc) - membership.joined_at
    if elapsed >= SELF_SWITCH_COOLDOWN:
        return 0
    rem = SELF_SWITCH_COOLDOWN - elapsed
    return max(1, rem.days + (1 if rem.seconds else 0))


class _JoinCultSelect(discord.ui.Select):
    """Used both for first-time join (no cult yet) and for switching."""

    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Pick a cult to join",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        picked = self.values[0]
        cult = await Cult.filter(name=picked.lower()).first()
        if cult is None:
            await interaction.response.send_message(
                f"`{picked}` no longer exists.", ephemeral=True
            )
            return
        ok, msg = await _self_join(interaction.client, interaction.user.id, cult)
        await interaction.response.send_message(msg, ephemeral=True)


async def _build_join_select(*, exclude_name: Optional[str] = None) -> Optional[_JoinCultSelect]:
    cults = await Cult.all().order_by("name")
    options = [
        discord.SelectOption(label=c.name.capitalize(), value=c.name)
        for c in cults
        if c.name != (exclude_name or "").lower()
    ]
    if not options:
        return None
    return _JoinCultSelect(options[:25])  # Discord cap on select options


class _ListMembersButton(discord.ui.Button):
    def __init__(self, cult_id):
        super().__init__(label="📋 List members", style=discord.ButtonStyle.secondary)
        self.cult_id = cult_id

    async def callback(self, interaction: discord.Interaction):
        cult = await Cult.filter(id=self.cult_id).prefetch_related("owner").first()
        if cult is None:
            await interaction.response.send_message("That cult is gone.", ephemeral=True)
            return
        body = await _render_cult_listing(interaction.client, cult)
        await interaction.response.send_message(body, ephemeral=True)


class _SwitchButton(discord.ui.Button):
    def __init__(self, current_cult_name: str):
        super().__init__(label="🔄 Switch cults", style=discord.ButtonStyle.primary)
        self.current_cult_name = current_cult_name

    async def callback(self, interaction: discord.Interaction):
        select = await _build_join_select(exclude_name=self.current_cult_name)
        if select is None:
            await interaction.response.send_message(
                "No other cults exist to switch to.", ephemeral=True
            )
            return
        view = discord.ui.View(timeout=180)
        view.add_item(select)
        await interaction.response.send_message(
            "Pick the cult to switch to:", view=view, ephemeral=True
        )


def _link_button_for_thread(
    thread_id: Optional[int], *, label: str, guild_id: int
) -> Optional[discord.ui.Button]:
    if not thread_id or not guild_id:
        return None
    url = f"https://discord.com/channels/{guild_id}/{thread_id}"
    return discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url)


def _build_in_cult_view(
    membership: CultMembership, cult: Cult, *, guild_id: int
) -> discord.ui.View:
    view = discord.ui.View(timeout=600)
    view.add_item(_ListMembersButton(cult.id))
    link = _link_button_for_thread(cult.thread_id, label="🔗 Visit thread", guild_id=guild_id)
    if link is not None:
        view.add_item(link)
    if _switch_remaining_days(membership) == 0:
        view.add_item(_SwitchButton(cult.name))
    return view


async def _build_no_cult_view() -> Optional[discord.ui.View]:
    select = await _build_join_select()
    if select is None:
        return None
    view = discord.ui.View(timeout=600)
    view.add_item(select)
    return view


# ---------------------------------------------------------------------------
# /return 0 handler
# ---------------------------------------------------------------------------


@register(0, tier=Tier.REGISTERED)
async def handle(ctx: commands.Context) -> None:
    guild_id = ctx.guild.id if ctx.guild else 0
    user_id = ctx.author.id

    # State D: caller is a figurehead
    owned = await lookup_owned_cult(user_id)
    if owned is not None:
        await ctx.reply(
            f"You're the figurehead of `{owned.name}`. Figureheads can't join "
            "cult threads.",
            ephemeral=True,
        )
        return

    # States B / C: caller is already in a cult
    disc = await DiscordAccount.filter(disc_uuid=str(user_id)).first()
    membership: Optional[CultMembership] = None
    if disc is not None:
        membership = await CultMembership.filter(discord_account=disc).first()

    if membership is not None:
        cult = await Cult.filter(id=membership.cult_id).prefetch_related("owner").first()
        if cult is None:
            # Defensive: membership row points at a deleted cult.
            await membership.delete()
            membership = None
        else:
            days_remaining = _switch_remaining_days(membership)
            if days_remaining == 0:
                body = (
                    f"You're in `{cult.name}`. The switch cooldown has elapsed — "
                    "you can move to another cult if you want."
                )
            else:
                body = (
                    f"You're in `{cult.name}`. You can't change cults for "
                    f"~{days_remaining} day(s)."
                )
            view = _build_in_cult_view(membership, cult, guild_id=guild_id)
            await ctx.reply(body, view=view, ephemeral=True)
            return

    # State A: no cult yet
    view = await _build_no_cult_view()
    if view is None:
        await ctx.reply(
            f"{_CULT_DESCRIPTION}\n\n"
            "_(No cults are configured yet — ask an operator to create one with "
            "`~manage_return 0 createCult`.)_",
            ephemeral=True,
        )
        return
    await ctx.reply(
        f"{_CULT_DESCRIPTION}\n\nPick a cult to join:",
        view=view,
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Pie-chart rendering (for the cultDistribution subcommand)
# ---------------------------------------------------------------------------


def _render_pie_ascii(counts: list[int], *, radius: int = 7) -> list[str]:
    """Render a circular pie chart as monospace rows. Empty list if all zero.

    Cells use ``_PIE_CHARS[i]`` for slice ``i``. The x-axis is doubled so
    each cell renders ~square in a code block (Discord's monospace font is
    roughly 1:2 width:height).
    """
    total = sum(counts)
    if total <= 0:
        return []
    boundaries: list[float] = []
    acc = 0
    for c in counts:
        acc += c
        boundaries.append(acc / total * 2 * math.pi)
    rows: list[str] = []
    for y in range(-radius, radius + 1):
        cells: list[str] = []
        for x in range(-2 * radius, 2 * radius + 1):
            dx = x / 2
            if dx * dx + y * y > radius * radius:
                cells.append(" ")
                continue
            angle = math.atan2(dx, -y)  # 0 at 12 o'clock, increases clockwise
            if angle < 0:
                angle += 2 * math.pi
            glyph = _PIE_CHARS[(len(boundaries) - 1) % len(_PIE_CHARS)]
            for i, b in enumerate(boundaries):
                if angle <= b + 1e-9:
                    glyph = _PIE_CHARS[i % len(_PIE_CHARS)]
                    break
            cells.append(glyph)
        rows.append("".join(cells).rstrip())
    return rows


def _render_distribution_message(
    cults: list[Cult],
    membership: list[int],
    activity: list[int],
    *,
    activity_days: int,
) -> str:
    membership_rows = _render_pie_ascii(membership) or ["  (no members)"]
    activity_rows = _render_pie_ascii(activity) or ["  (no recent messages)"]

    m_total = sum(membership) or 1
    a_total = sum(activity) or 1
    legend = []
    for i, cult in enumerate(cults):
        glyph = _PIE_CHARS[i % len(_PIE_CHARS)]
        m_pct = membership[i] / m_total * 100
        a_pct = activity[i] / a_total * 100
        legend.append(
            f"  {glyph}  {cult.name.capitalize():<14} "
            f"size {m_pct:5.1f}%   activity {a_pct:5.1f}%"
        )

    body = "\n".join([
        "Distribution",
        *membership_rows,
        "",
        f"Activity (last {activity_days}d, non-bot messages)",
        *activity_rows,
        "",
        *legend,
    ])
    return f"**Cult distribution & activity**\n```\n{body}\n```"


# ---------------------------------------------------------------------------
# ~manage_return 0 subcommands
# ---------------------------------------------------------------------------


@register_manage(
    0, "createCult", tier=Tier.OPERATOR,
    help="Create a new cult.",
    usage="<name> <figurehead> <thread_id>",
)
async def _manage_create_cult(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if len(args) < 3:
        await send_feedback(
            ctx,
            "Usage: `~manage_return 0 createCult <name> <figurehead> <thread_id>`",
            persist=persist,
        )
        return
    name, figurehead, thread_id_str = args[0], args[1], args[2]
    name_key = name.lower()
    try:
        thread_id = int(thread_id_str)
    except ValueError:
        await send_feedback(
            ctx, f"`thread_id` must be an integer; got `{thread_id_str}`.", persist=persist
        )
        return
    if await Cult.filter(name=name_key).exists():
        await send_feedback(ctx, f"A cult named `{name}` already exists.", persist=persist)
        return
    mc = await _resolve_owner_mc(ctx, figurehead)
    if mc is None:
        await send_feedback(
            ctx,
            f"Could not resolve figurehead `{figurehead}` to a Minecraft account "
            "(tried Discord mention, MC username, and MC UUID).",
            persist=persist,
        )
        return
    thread = await _resolve_thread(ctx.bot, thread_id)
    if thread is None:
        await send_feedback(
            ctx,
            f"Could not resolve thread id `{thread_id}` to a Discord thread the bot can see.",
            persist=persist,
        )
        return
    await Cult.create(name=name_key, owner=mc, thread_id=thread_id)

    # Wire up the cross-cult messaging button. Late import keeps the
    # week_0 module import-time cheap and side-effect-free.
    from cogs.events.returns.lib.views.intercult_view import install_intercult_in_thread
    install_result = await install_intercult_in_thread(ctx.bot, thread)
    button_note = {
        "posted": "Intercult button installed and pinned.",
        "skipped": "Intercult button was already pinned in that thread.",
        "failed": (
            "⚠️ Intercult button install failed — retry with "
            "`/script install_intercult` once the issue is resolved."
        ),
    }[install_result]

    # Wire up the recruitment button too — skipped for deercult, which
    # gets it opted in later via `/script install_recruitment_deercult`.
    from cogs.events.returns.lib.views.recruitment_view import (
        DEERCULT_NAME,
        install_recruitment_in_thread,
    )
    if name_key == DEERCULT_NAME:
        recruit_note = (
            "Recruitment button skipped (deercult is opt-in via "
            "`/script install_recruitment_deercult`)."
        )
    else:
        recruit_result = await install_recruitment_in_thread(ctx.bot, thread)
        recruit_note = {
            "posted": "Recruitment button installed and pinned.",
            "skipped": "Recruitment button was already pinned in that thread.",
            "failed": (
                "⚠️ Recruitment button install failed — retry with "
                "`/script install_recruitment` once the issue is resolved."
            ),
        }[recruit_result]

    await send_feedback(
        ctx,
        f"✅ Created cult `{name}` with figurehead `{mc.mc_username}` "
        f"(thread `{thread_id}`). {button_note} {recruit_note}",
        persist=persist,
    )


@register_manage(
    0, "listMembers", tier=Tier.ADMIN,
    help="Show the figurehead, staff, and members of a cult.",
    usage="<cult>",
)
async def _manage_list_members(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if not args:
        await send_feedback(ctx, "Usage: `~manage_return 0 listMembers <cult>`", persist=persist)
        return
    cult_name = args[0]
    cult = await Cult.filter(name=cult_name.lower()).prefetch_related("owner").first()
    if cult is None:
        await send_feedback(ctx, f"No cult named `{cult_name}`.", persist=persist)
        return
    text = await _render_cult_listing(ctx.bot, cult)
    await send_feedback(ctx, text, persist=persist)


@register_manage(
    0, "broadcastMessage", tier=Tier.ADMIN,
    help="Send an announcement to every cult's thread.",
    usage="<message>",
)
async def _manage_broadcast(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if not args:
        await send_feedback(
            ctx, "Usage: `~manage_return 0 broadcastMessage <message>`", persist=persist
        )
        return
    # Re-join args; the literal ``\n`` two-character sequence converts to a real
    # newline (text args otherwise can't carry a line break).
    text = " ".join(args).replace("\\n", "\n")
    sent = failed = missing = 0
    for cult in await Cult.all():
        if not cult.thread_id:
            missing += 1
            continue
        thread = await _resolve_thread(ctx.bot, cult.thread_id)
        if thread is None:
            missing += 1
            continue
        try:
            await thread.send(
                f"**📢 Announcement:**\n{text}",
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
            sent += 1
        except discord.HTTPException as e:
            logger.warning(
                "broadcast: send to %s (%s) failed: %s", cult.thread_id, cult.name, e
            )
            failed += 1
    await send_feedback(
        ctx,
        f"✅ Sent to {sent} thread(s). ({failed} failed, {missing} unresolved)",
        persist=persist,
    )


@register_manage(
    0, "cultMessage", tier=Tier.ADMIN,
    help="Send an announcement to a single cult's thread.",
    usage="<cult> <message>",
)
async def _manage_cult_message(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if len(args) < 2:
        await send_feedback(
            ctx, "Usage: `~manage_return 0 cultMessage <cult> <message>`", persist=persist
        )
        return
    cult_name, *rest = args
    cult = await Cult.filter(name=cult_name.lower()).first()
    if cult is None:
        await send_feedback(ctx, f"No cult named `{cult_name}`.", persist=persist)
        return
    if not cult.thread_id:
        await send_feedback(ctx, f"`{cult.name}` has no thread configured.", persist=persist)
        return
    thread = await _resolve_thread(ctx.bot, cult.thread_id)
    if thread is None:
        await send_feedback(ctx, f"Couldn't resolve thread for `{cult.name}`.", persist=persist)
        return
    text = " ".join(rest).replace("\\n", "\n")
    try:
        await thread.send(
            f"**📢 Announcement:**\n{text}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    except discord.HTTPException as e:
        await send_feedback(ctx, f"Send failed: {e}", persist=persist)
        return
    await send_feedback(ctx, f"✅ Sent to `{cult.name}`.", persist=persist)


@register_manage(
    0, "addressOwnCult", tier=Tier.ADMIN,
    custom_check=is_figurehead,
    help="Figurehead-only: send a message to your own cult, attributed to you.",
    usage="<message>",
)
async def _manage_address_own(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if not args:
        await send_feedback(
            ctx, "Usage: `~manage_return 0 addressOwnCult <message>`", persist=persist
        )
        return
    cult = await lookup_owned_cult(ctx.author.id)
    if cult is None:
        # Should be unreachable thanks to is_figurehead, but guard anyway.
        await send_feedback(ctx, "You don't own a cult.", persist=persist)
        return
    if not cult.thread_id:
        await send_feedback(ctx, f"`{cult.name}` has no thread configured.", persist=persist)
        return
    thread = await _resolve_thread(ctx.bot, cult.thread_id)
    if thread is None:
        await send_feedback(ctx, f"Couldn't resolve thread for `{cult.name}`.", persist=persist)
        return
    await cult.fetch_related("owner")
    figurehead_name = cult.owner.mc_username
    text = " ".join(args).replace("\\n", "\n")
    try:
        await thread.send(
            f"**Message from `{figurehead_name}`:**\n{text}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    except discord.HTTPException as e:
        await send_feedback(ctx, f"Send failed: {e}", persist=persist)
        return
    await send_feedback(
        ctx, f"✅ Sent to `{cult.name}` as `{figurehead_name}`.", persist=persist
    )


@register_manage(
    0, "force", tier=Tier.STAFF,
    help="Force-move a user to a cult, bypassing the switch cooldown.",
    usage="<@target|id> <cult>",
)
async def _manage_force(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if len(args) < 2:
        await send_feedback(
            ctx, "Usage: `~manage_return 0 force <@target|id> <cult>`", persist=persist
        )
        return
    target_str, cult_name = args[0], args[1]
    try:
        member = await CaseInsensitiveMember().convert(ctx, target_str)
    except commands.MemberNotFound:
        await send_feedback(ctx, f"Could not find member `{target_str}`.", persist=persist)
        return
    cult = await Cult.filter(name=cult_name.lower()).first()
    if cult is None:
        await send_feedback(ctx, f"No cult named `{cult_name}`.", persist=persist)
        return
    _ok, msg = await _force_move(ctx.bot, member.id, cult)
    await send_feedback(ctx, msg, persist=persist)


@register_manage(
    0, "cultDistribution", tier=Tier.STAFF,
    help="Pie charts: relative cult sizes + recent thread activity (no raw counts).",
    usage="",
)
async def _manage_cult_distribution(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    cults = await Cult.all().order_by("name")
    if not cults:
        await send_feedback(ctx, "No cults exist yet.", persist=persist)
        return

    membership_counts: list[int] = []
    for cult in cults:
        membership_counts.append(await CultMembership.filter(cult_id=cult.id).count())

    cutoff = datetime.now(timezone.utc) - ACTIVITY_LOOKBACK
    activity_counts: list[int] = []
    for cult in cults:
        n = 0
        thread = await _resolve_thread(ctx.bot, cult.thread_id) if cult.thread_id else None
        if thread is not None:
            try:
                async for msg in thread.history(limit=None, after=cutoff):
                    if not msg.author.bot:
                        n += 1
            except discord.HTTPException as e:
                logger.warning(
                    "cultDistribution: history(%s) failed: %s", cult.name, e
                )
        activity_counts.append(n)

    days = int(ACTIVITY_LOOKBACK.total_seconds() // 86400)
    text = _render_distribution_message(
        cults, membership_counts, activity_counts, activity_days=days
    )
    await send_feedback(ctx, text, persist=persist)


@register_manage(
    0, "listOnlineUnaffiliated", tier=Tier.STAFF,
    help=(
        "List online players not in any cult. Staff bypass for the cult-thread "
        "recruitment button's 1h per-cult ratelimit (doesn't consume or check it)."
    ),
    usage="",
)
async def _manage_list_online_unaffiliated(
    ctx: commands.Context, args: list[str]
) -> None:
    persist = is_persist_context(ctx)

    # Late import: keeps week_0 import-time light and matches the
    # createCult pattern for the other recruitment_view imports.
    from cogs.events.returns.lib.views.recruitment_view import (
        gather_unaffiliated_online,
    )
    unaffiliated, temp_err, wynn_err = await gather_unaffiliated_online()

    if temp_err and wynn_err:
        await send_feedback(
            ctx,
            "Couldn't reach either online-player source "
            f"({temp_err}; {wynn_err}). Try again in a minute.",
            persist=persist,
        )
        return

    degraded = ""
    if temp_err:
        degraded += (
            f"\n_(temp-server unavailable: {temp_err} — "
            "VetsMod-only players may be missing.)_"
        )
    if wynn_err:
        degraded += (
            f"\n_(Wynncraft API unavailable: {wynn_err} — "
            "non-VetsMod guild members may be missing.)_"
        )

    if not unaffiliated:
        await send_feedback(
            ctx,
            "No unaffiliated players are online right now." + degraded,
            persist=persist,
        )
        return

    lines = [f"- {name}" for name in unaffiliated]
    header = f"**Online players not in any cult** ({len(unaffiliated)}):\n"
    body = "\n".join(lines)
    full = header + body + degraded
    if len(full) > 1900:
        footer = "\n…_(list truncated)_" + degraded
        budget = 2000 - len(header) - len(footer)
        trimmed: list[str] = []
        used = 0
        for line in lines:
            add = len(line) + 1  # newline
            if used + add > budget:
                break
            trimmed.append(line)
            used += add
        full = header + "\n".join(trimmed) + footer

    await send_feedback(ctx, full, persist=persist)


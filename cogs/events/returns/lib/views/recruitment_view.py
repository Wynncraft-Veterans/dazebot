"""Persistent button + ephemeral list reply for cult recruitment.

The pinned button is installed in each cult thread by
``/script install_recruitment`` (every cult except deercult) and
``/script install_recruitment_deercult`` (deercult only, one-shot once the
membership rebalance lands). New cults created via ``~manage_return 0
createCult`` also get the button automatically — deercult is the only
default exclusion.

When clicked, the button gathers the currently-online player list from
two sources, merges them, filters out anyone with a ``CultMembership``,
and replies ephemerally with the unaffiliated usernames so the cult can
go pitch them. Each cult is rate-limited to one query per hour.

Online-list sources (both queried per click; either failing is tolerated
and noted in the reply):
  * temporary-server ``GET /v1/outbound/list`` — VetsMod-connected
    users (closest analogue to ``/wv list``). Base URL from the
    ``TEMPORARY_SERVER_URL`` env var; defaults to the public
    ``https://api.wynnvets.org``.
  * Wynncraft v3 guild endpoint for the ``Returners`` guild — picks up
    in-guild players who happen not to be running VetsMod.

The button itself is persistent (registered with ``bot.add_view`` in
``bot.py:setup_hook``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import discord

from lib.mc.wynn_api.guild import get_guild
from orm import (
    Cult,
    CultMembership,
    DiscordAccount,
    MinecraftAccount,
    RecruitmentQuery,
)

logger = logging.getLogger(
    "dazebot.cogs.events.returns.lib.views.recruitment_view"
)


RECRUITMENT_BUTTON_CUSTOM_ID = "recruitment:button"

COOLDOWN = timedelta(hours=1)

# Cult excluded from default bulk-install. ``install_recruitment_button``
# skips it; ``install_recruitment_in_deercult`` is the one-shot opt-in.
DEERCULT_NAME = "deercult"

# Returners is the in-game guild we recruit *for*; the Wynncraft-API
# branch of the online-player fetch is scoped to its members. (VetsMod
# users from the temporary-server branch are not guild-filtered — anyone
# running the mod is a recruitment candidate even if they're not in the
# guild yet.)
RETURNERS_GUILD = "Returners"

# Wynncraft guild API can occasionally be slow; cap the wait so a stalled
# upstream doesn't keep the interaction "thinking…" indefinitely.
WYNN_FETCH_TIMEOUT = 10.0
TEMPSERVER_FETCH_TIMEOUT = 10.0


def _tempserver_url() -> str:
    base = os.environ.get(
        "TEMPORARY_SERVER_URL", "https://api.wynnvets.org"
    ).rstrip("/")
    return f"{base}/v1/outbound/list"


async def _sender_cult_for(disc_uuid: str) -> Optional[Cult]:
    disc = await DiscordAccount.filter(disc_uuid=disc_uuid).first()
    if disc is None:
        return None
    membership = await CultMembership.filter(discord_account=disc).first()
    if membership is None:
        return None
    return await Cult.get(id=membership.cult_id)


async def _cooldown_remaining(cult_name: str) -> Optional[timedelta]:
    cutoff = datetime.now(timezone.utc) - COOLDOWN
    latest = (
        await RecruitmentQuery.filter(
            cult_name=cult_name, queried_at__gte=cutoff
        )
        .order_by("-queried_at")
        .first()
    )
    if latest is None:
        return None
    remaining = (latest.queried_at + COOLDOWN) - datetime.now(timezone.utc)
    return remaining if remaining.total_seconds() > 0 else None


def _format_remaining(td: timedelta) -> str:
    total = max(0, int(td.total_seconds()))
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"


async def _fetch_tempserver_online() -> tuple[dict[str, str], Optional[str]]:
    """Fetch VetsMod-connected users from temporary-server.

    Returns ``(uuid -> username, error)``. ``error`` is None on success;
    on failure ``uuid -> username`` is empty and ``error`` is a short
    human-readable note appended to the ephemeral reply so the clicker
    knows the list might be incomplete.
    """
    url = _tempserver_url()
    try:
        timeout = aiohttp.ClientTimeout(total=TEMPSERVER_FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(
                        "recruitment: temp-server %s returned %s",
                        url, resp.status,
                    )
                    return {}, f"temporary-server HTTP {resp.status}"
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("recruitment: temp-server %s failed: %s", url, e)
        return {}, "temporary-server unreachable"
    out: dict[str, str] = {}
    for entry in data.get("connected", []):
        uuid = entry.get("uuid")
        username = entry.get("username")
        if uuid and username:
            out[uuid] = username
    return out, None


async def _fetch_wynn_online() -> tuple[dict[str, str], Optional[str]]:
    """Fetch online ``Returners`` members from the Wynncraft guild API.

    Returns ``(uuid -> username, error)``. Same shape as
    :func:`_fetch_tempserver_online`.
    """
    try:
        guild = await asyncio.wait_for(
            get_guild(RETURNERS_GUILD), timeout=WYNN_FETCH_TIMEOUT
        )
    except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
        # get_guild may raise on API error envelopes (raise_for_error_envelope)
        # or generic transport errors — treat them uniformly as a degraded
        # data source rather than crashing the interaction.
        logger.warning("recruitment: wynn guild fetch failed: %s", e)
        return {}, "Wynncraft API unreachable"
    out: dict[str, str] = {}
    for member in guild.members.all_members():
        if not bool(member.online):
            continue
        out[member.uuid] = member.username
    return out, None


async def _unaffiliated_usernames(online: dict[str, str]) -> list[str]:
    """Filter ``online`` (uuid -> username) down to users with no
    ``CultMembership`` row.

    A user qualifies as unaffiliated if either:
      * their UUID has no ``MinecraftAccount`` row (so no Discord link
        is possible, so no cult), OR
      * their MC account links to a ``DiscordAccount`` that has no
        ``CultMembership``, OR
      * their MC account has no owning ``DiscordAccount``.

    We also walk ``MinecraftAlt`` so an alt account online doesn't show
    up as recruitable when the primary's Discord owner is already in a
    cult.
    """
    if not online:
        return []

    uuids = list(online.keys())

    # One DB round-trip each for the three relevant lookups.
    primary_accounts = await MinecraftAccount.filter(uuid__in=uuids).all()
    primary_by_uuid = {a.uuid: a for a in primary_accounts}

    primary_ids = [a.id for a in primary_accounts]
    discord_rows = (
        await DiscordAccount.filter(minecraft_account_id__in=primary_ids).all()
        if primary_ids
        else []
    )
    disc_by_mc_id = {d.minecraft_account_id: d for d in discord_rows}

    # Also: alts. If this UUID is registered as an alt, find the owning
    # DiscordAccount that way too.
    from orm import MinecraftAlt  # local import: keeps module-load cheap

    alt_rows = (
        await MinecraftAlt.filter(
            minecraft_account__uuid__in=uuids
        ).prefetch_related("minecraft_account", "discord_account").all()
    )
    disc_by_alt_uuid: dict[str, DiscordAccount] = {
        a.minecraft_account.uuid: a.discord_account for a in alt_rows
    }

    # Collect every Discord owner we need to check for CultMembership.
    owners: set = set()
    for d in disc_by_mc_id.values():
        owners.add(d.id)
    for d in disc_by_alt_uuid.values():
        owners.add(d.id)

    cult_membership_owners: set = set()
    if owners:
        memberships = await CultMembership.filter(
            discord_account_id__in=list(owners)
        ).all()
        cult_membership_owners = {m.discord_account_id for m in memberships}

    unaffiliated: list[str] = []
    for uuid, username in online.items():
        mc = primary_by_uuid.get(uuid)
        owner_disc: Optional[DiscordAccount] = None
        if mc is not None:
            owner_disc = disc_by_mc_id.get(mc.id)
        if owner_disc is None:
            owner_disc = disc_by_alt_uuid.get(uuid)

        if owner_disc is None:
            # Not linked to any Discord user → can't be in a cult.
            unaffiliated.append(username)
            continue
        if owner_disc.id in cult_membership_owners:
            continue
        unaffiliated.append(username)

    unaffiliated.sort(key=str.lower)
    return unaffiliated


async def gather_unaffiliated_online() -> tuple[list[str], Optional[str], Optional[str]]:
    """Fetch both online-player sources, merge, and filter to unaffiliated.

    Shared by the in-cult-thread recruitment button and the staff
    ``~manage_return 0 listOnlineUnaffiliated`` bypass. Both upstream
    sources are tolerated as degraded — the per-source error strings
    are returned so each caller can format its own user-facing copy.

    Returns ``(unaffiliated, temp_err, wynn_err)``. When both errors
    are non-None the result list is empty and the caller should bail
    rather than treat it as "no one online".
    """
    (temp_users, temp_err), (wynn_users, wynn_err) = await asyncio.gather(
        _fetch_tempserver_online(),
        _fetch_wynn_online(),
    )
    if temp_err and wynn_err:
        return [], temp_err, wynn_err
    # Merge; Wynncraft API gives canonical guild-API username, temp-server
    # gives VetsMod's last-seen username. On UUID collision, Wynncraft
    # wins because it's the authoritative source for guild-member names.
    merged: dict[str, str] = {**temp_users, **wynn_users}
    unaffiliated = await _unaffiliated_usernames(merged)
    return unaffiliated, temp_err, wynn_err


class RecruitmentButtonView(discord.ui.View):
    """Persistent view pinned in cult threads.

    Register exactly once via ``bot.add_view(RecruitmentButtonView())`` in
    ``setup_hook`` so callbacks survive bot restarts.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="List unaffiliated online players",
        style=discord.ButtonStyle.primary,
        emoji="🪝",
        custom_id=RECRUITMENT_BUTTON_CUSTOM_ID,
    )
    async def list_unaffiliated(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        cult = await _sender_cult_for(str(interaction.user.id))
        if cult is None:
            await interaction.response.send_message(
                "You aren't in a cult, so you can't run a recruitment query. "
                "Use `/joincult` or `/return 0` to join one first.",
                ephemeral=True,
            )
            return

        remaining = await _cooldown_remaining(cult.name)
        if remaining is not None:
            await interaction.response.send_message(
                f"`{cult.name}` already ran a recruitment query recently. "
                f"Try again in **{_format_remaining(remaining)}**.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        unaffiliated, temp_err, wynn_err = await gather_unaffiliated_online()

        if temp_err and wynn_err:
            await interaction.followup.send(
                "Couldn't reach either online-player source "
                f"({temp_err}; {wynn_err}). Try again in a minute.",
                ephemeral=True,
            )
            return

        # Record the query *after* the work — failing fetches above don't
        # consume the cooldown so the clicker can retry immediately.
        await RecruitmentQuery.create(
            cult_name=cult.name,
            requester_disc_uuid=str(interaction.user.id),
        )

        degraded = ""
        if temp_err:
            degraded += f"\n_(temp-server unavailable: {temp_err} — VetsMod-only players may be missing.)_"
        if wynn_err:
            degraded += f"\n_(Wynncraft API unavailable: {wynn_err} — non-VetsMod guild members may be missing.)_"

        if not unaffiliated:
            await interaction.followup.send(
                "No unaffiliated players are online right now. "
                f"`{cult.name}` can query again in 1 hour." + degraded,
                ephemeral=True,
            )
            return

        # Format as a single bullet list. Discord ephemeral message limit
        # is 2000 chars; truncate gracefully if a huge crowd is online.
        lines = [f"- {name}" for name in unaffiliated]
        header = (
            f"**Online players not in any cult** ({len(unaffiliated)}):\n"
            "Go pitch them! Tell them what sets `"
            f"{cult.name}` apart from the others.\n"
        )
        body = "\n".join(lines)
        full = header + body + degraded
        if len(full) > 1900:
            # Trim list to fit, keeping header + footer.
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

        await interaction.followup.send(
            full,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


RECRUITMENT_PINNED_BODY = (
    "**Recruit for your cult.**\n"
    "Click below to get a list of all online players who aren't in any "
    "cult yet. Try to convince everyone why they should join you — let "
    "them know what sets you apart from the others! "
    "One query per cult per hour."
)


async def _resolve_thread(bot, thread_id: int) -> Optional[discord.Thread]:
    ch = bot.get_channel(thread_id)
    if isinstance(ch, discord.Thread):
        return ch
    try:
        ch = await bot.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logger.warning("recruitment: thread %s not fetchable: %s", thread_id, e)
        return None
    return ch if isinstance(ch, discord.Thread) else None


async def _recruitment_button_already_pinned(thread: discord.Thread) -> bool:
    try:
        pins = await thread.pins()
    except discord.HTTPException as e:
        logger.warning("recruitment install: pins() on %s failed: %s", thread.id, e)
        return False
    for msg in pins:
        for row in msg.components:
            for child in getattr(row, "children", []):
                if (
                    isinstance(child, discord.Button)
                    and child.custom_id == RECRUITMENT_BUTTON_CUSTOM_ID
                ):
                    return True
    return False


async def install_recruitment_in_thread(
    bot, thread: discord.Thread
) -> str:
    """Idempotently install + pin the recruitment button in one thread.

    Returns one of ``"posted"``, ``"skipped"`` (already pinned with our
    ``custom_id``), or ``"failed"``. Mirror of the intercult installer.
    """
    if await _recruitment_button_already_pinned(thread):
        return "skipped"
    try:
        msg = await thread.send(RECRUITMENT_PINNED_BODY, view=RecruitmentButtonView())
    except discord.HTTPException as e:
        logger.warning("recruitment install: send to %s failed: %s", thread.id, e)
        return "failed"
    try:
        await msg.pin(reason="recruitment bootstrap")
    except discord.HTTPException as e:
        logger.warning("recruitment install: pin in %s failed: %s", thread.id, e)
    return "posted"


async def install_recruitment_button(bot) -> tuple[int, int, int, int, int]:
    """Bulk-install the recruitment button across every cult thread
    *except* deercult.

    Idempotent. Cults with no ``thread_id`` or an unresolvable thread are
    counted as ``unresolved``. Returns
    ``(posted, skipped, failed, unresolved, excluded)`` where ``excluded``
    counts deercult.

    Bootstrap-only — call from ``/script install_recruitment``. Routine
    cult creation goes through ``createCult``, which installs the button
    at creation time (also excluding the deercult name).
    """
    posted = skipped = failed = unresolved = excluded = 0
    for cult in await Cult.all():
        if cult.name.lower() == DEERCULT_NAME:
            excluded += 1
            continue
        if not cult.thread_id:
            unresolved += 1
            continue
        thread = await _resolve_thread(bot, cult.thread_id)
        if thread is None:
            unresolved += 1
            continue
        result = await install_recruitment_in_thread(bot, thread)
        if result == "posted":
            posted += 1
        elif result == "skipped":
            skipped += 1
        else:
            failed += 1
    return posted, skipped, failed, unresolved, excluded


async def install_recruitment_in_deercult(bot) -> str:
    """One-shot installer for deercult specifically.

    Used by ``/script install_recruitment_deercult`` once membership
    rebalances make it appropriate to give deercult the recruitment
    button (the default ``install_recruitment_button`` skips it).
    Returns one of ``"posted"``, ``"skipped"``, ``"failed"``, or
    ``"unresolved"`` (no thread configured / unreachable).
    """
    cult = await Cult.filter(name=DEERCULT_NAME).first()
    if cult is None or not cult.thread_id:
        return "unresolved"
    thread = await _resolve_thread(bot, cult.thread_id)
    if thread is None:
        return "unresolved"
    return await install_recruitment_in_thread(bot, thread)

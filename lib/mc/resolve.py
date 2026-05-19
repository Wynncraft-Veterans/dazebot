"""Shared resolution helpers for the "Discord member or Minecraft account"
input shape that appears across cogs.

Three things this module centralises:

* ``ensure_mc_account`` -- the single MinecraftAccount creator. Used to live
  inlined in four places (``cogs/admin.py`` ``/link set``/``/link request``,
  ``cogs/management.py`` ``_ensure_mc_account``, ``lib/linking.py``
  ``try_consume_code``, ``lib/staff_actions.py`` ``resolve_target``). Those
  four blocks drifted apart over time (only three of them populated
  ``guild`` from the API at creation); this collapses them.

* ``resolve_target`` -- the combined "ping/id/name (Discord) OR
  username/UUID (Minecraft)" resolver. Replaces the duplicate methods
  previously on ``Management`` and ``Admin`` cogs.

* Vanity date parsing -- moved out of ``cogs/management.py`` so future
  vanity-related cogs can reuse the same parser without circular imports.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import logging
import re
from typing import Optional

import discord
from discord.ext import commands
from tortoise.expressions import Q

from config import CurrConfig
from lib.discord_utils.converters import CaseInsensitiveMember
from lib.mc.wynn_api.player import get_player_stats
from orm import DiscordAccount, MinecraftAccount, UNKNOWN_LAST_ONLINE

logger = logging.getLogger("dazebot.lib.mc.resolve")


_VANITY_DATE_RE = re.compile(
    r"^(?P<year>\d{4})(?:[-/](?P<month>\d{1,2})(?:[-/](?P<day>\d{1,2}))?)?$"
)


async def resolve_mc_account_loose(value: str) -> Optional[MinecraftAccount]:
    """Look up a MinecraftAccount in the local DB by uuid OR username (case-
    insensitive on both ``mc_username`` and ``wynn_username``). No fallback
    to the Wynncraft API -- use :func:`ensure_mc_account` for that.
    """
    return await MinecraftAccount.filter(
        Q(uuid=value)
        | Q(mc_username__iexact=value)
        | Q(wynn_username__iexact=value)
    ).first()


async def ensure_mc_account(value: str) -> MinecraftAccount:
    """Return the MinecraftAccount for ``value``, creating it via a
    Wynncraft API lookup if absent.

    ``value`` may be a UUID or a username. The new row is populated with
    ``guild`` from the live API so callers that need it (role-state
    enforcement, blocklist checks) don't see ``None`` until the activity
    loop's next tick.

    Raises :class:`lib.wynn_api.errors.WynnApiError` (and any underlying
    network exception) if the Wynncraft API can't resolve the value;
    callers handle this with a user-facing error message.
    """
    existing = await resolve_mc_account_loose(value)
    if existing is not None:
        return existing
    fs = await get_player_stats(value, full=True)
    return await MinecraftAccount.create(
        uuid=fs.uuid,
        wynn_username=fs.username,
        mc_username=fs.username,
        guild=fs.guild.name if fs.guild else None,
        # fs.lastJoin / firstJoin can be None per Wynncraft privacy opt-out;
        # use UNKNOWN_LAST_ONLINE as the in-band "unknown" marker.
        last_online=fs.lastJoin or UNKNOWN_LAST_ONLINE,
        last_manual_check=UNKNOWN_LAST_ONLINE,
        first_join=fs.firstJoin,
    )


async def refresh_mc_guild(mc: MinecraftAccount) -> MinecraftAccount:
    """Best-effort refresh of ``mc.guild`` from the live Wynncraft API.

    Why this is needed: nothing in the periodic path clears a *stale*
    ``guild``. ``cogs/activity`` only nulls ``guild`` for guilds it actively
    scans (Returners); ``lib/mc/wynn.check_player_full`` advances
    ``last_online`` but deliberately never touches ``guild``. So a player
    who left an unscanned guild keeps a non-null ``guild`` forever. Any
    caller that gates on guild membership (``/waitlist`` add/self, the
    role-state baseline in ``try_consume_code``) must refresh first, or it
    acts on a value that says "in a guild" when the player is guildless --
    which the ``waitlist_cleanup`` janitor reads as "joined a guild, drop
    them", purging a freshly-added entry within a minute.

    Best-effort: on any API failure the stored value is left untouched
    (matches the pre-existing inline behaviour in ``try_consume_code``).
    Mutates and saves ``mc``; returns it for call chaining.
    """
    try:
        fs = await get_player_stats(mc.uuid, full=True)
    except Exception:  # noqa: BLE001 - third-party API
        logger.warning(
            "refresh_mc_guild: API lookup failed for %s; using stored guild %r",
            mc.uuid, mc.guild,
        )
        return mc
    live_guild = fs.guild.name if fs.guild else None
    if mc.guild != live_guild:
        logger.info("refresh_mc_guild: %s guild %r -> %r", mc.uuid, mc.guild, live_guild)
        mc.guild = live_guild
        await mc.save(update_fields=["guild"])
    return mc


async def resolve_target_member(
    ctx: commands.Context, value: str
) -> Optional[discord.Member]:
    """Resolve ``value`` (a ping, id, username, or display name) to a
    :class:`discord.Member` of the guild this context is in. Returns
    ``None`` if no match or if invoked outside a guild.
    """
    if ctx.guild is None:
        return None
    try:
        return await CaseInsensitiveMember().convert(ctx, value)
    except commands.MemberNotFound:
        return None


async def resolve_target(
    ctx: commands.Context, target: str
) -> tuple[Optional[discord.Member], Optional[MinecraftAccount]]:
    """Resolve a free-form ``target`` string that may be a Discord ping/id/
    username OR a Minecraft username/UUID. Returns ``(member, mc)``: either
    may be ``None``, both may be set when one side links to the other.

    Lookup order:
      1. Try Discord member.
      2. If found AND they have a linked MC, return that.
      3. Otherwise fall back to a loose MC lookup; if it hits and we can
         find a Discord member linked to the MC, return both.
    """
    member = await resolve_target_member(ctx, target)
    mc: Optional[MinecraftAccount] = None
    if member is not None:
        disc = (
            await DiscordAccount.filter(disc_uuid=str(member.id))
            .select_related("minecraft_account")
            .first()
        )
        if disc and disc.minecraft_account:
            mc = disc.minecraft_account
    if mc is None:
        mc = await resolve_mc_account_loose(target)
        if mc is not None and member is None:
            disc = await DiscordAccount.filter(minecraft_account_id=mc.id).first()
            if disc is not None and ctx.guild is not None:
                member = ctx.guild.get_member(int(disc.disc_uuid))
    return member, mc


def parse_vanity_date(value: str) -> date:
    """Accept ``'2014'``, ``'2014-03'``, ``'2014-03-12'``, ``'2014/3/12'``.
    Missing parts default to January / day 1.
    """
    m = _VANITY_DATE_RE.match(value.strip())
    if not m:
        raise ValueError(
            "Could not parse date. Use a year (2014), year-month (2014-03), "
            "or year-month-day (2014-03-12)."
        )
    year = int(m.group("year"))
    month = int(m.group("month") or 1)
    day = int(m.group("day") or 1)
    return date(year, month, day)


def vanity_role_for_date(value: date) -> Optional[str]:
    """Return the configured vanity-role id for the given date, or ``None``
    if the date is later than the most recent cutoff.
    """
    from lib.discord_utils.vanity_roles import get_vanity_role_id

    dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return get_vanity_role_id(dt, CurrConfig)

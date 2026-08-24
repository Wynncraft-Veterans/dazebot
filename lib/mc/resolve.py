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
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q

from config import CurrConfig
from lib.discord_utils.converters import CaseInsensitiveMember
from lib.mc.mojang import get_mc_uuid, resolve_canonical_username
from lib.mc.wynn_api.errors import WynnApiError
# >>> PATCH BEGIN: WYNN-STALE-WORKAROUND (2026-07-11)
# Reason: /v3/player/{uuid}.guild ~12h stale for offline players.
# Remove: when Wynncraft invalidates the player endpoint on
#   officer-write events, or exposes a webhook, or an offline-player
#   spot-check shows <10min staleness.
from lib.mc.wynn_api.guild import get_guild
# <<< PATCH END: WYNN-STALE-WORKAROUND
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
    network exception) if neither the Wynncraft API nor a Mojang
    name->UUID fallback can resolve the value; callers handle this with a
    user-facing error message.

    Old-name fallback: when Wynncraft 404s on ``value``, we try to translate
    it to a UUID via Mojang/PlayerDB (see :func:`lib.mc.mojang.get_mc_uuid`).
    If the resulting UUID matches an existing MinecraftAccount row, that row
    is returned -- this catches Mojang renames where Wynncraft only knows
    the player by their new name but our DB still records the old one.
    This branch never creates new rows; it only finds existing ones.

    New-name fallback: the mirror case, where Wynncraft *does* know
    ``value`` but our DB filed the same UUID under the player's old name.
    The name-keyed lookup above misses, so we re-check by the UUID the API
    just handed us before creating anything, and refresh the stale
    usernames on the row we find (see :func:`_sync_usernames`). Without
    that re-check the create below raises ``UNIQUE constraint failed:
    minecraft_accounts.uuid`` out of whatever command invoked us.
    """
    existing = await resolve_mc_account_loose(value)
    if existing is not None:
        return existing
    try:
        fs = await get_player_stats(value, full=True)
    except WynnApiError as wynn_exc:
        try:
            resolved_uuid = await get_mc_uuid(value)
        except Exception:  # noqa: BLE001 - guard against helper bugs
            logger.exception(
                "ensure_mc_account: get_mc_uuid raised for %r; "
                "re-raising original WynnApiError",
                value,
            )
            raise wynn_exc from None
        if resolved_uuid:
            existing_by_uuid = await MinecraftAccount.filter(
                uuid=resolved_uuid
            ).first()
            if existing_by_uuid is not None:
                logger.info(
                    "ensure_mc_account: matched %r to existing MinecraftAccount "
                    "%s via Mojang name fallback (Wynncraft did not recognise "
                    "the name)",
                    value, resolved_uuid,
                )
                return existing_by_uuid
        raise
    # A name-keyed miss does NOT prove the account is new. Stored usernames
    # go stale the instant a player renames on Mojang, and only accounts on
    # the Returners roster get them refreshed (``cogs/activity._apply_guild``
    # is the sole periodic writer of the two username columns). So an
    # operator typing a player's *current* name for a row we filed under
    # their *old* one arrives here holding a UUID we already have.
    existing_by_uuid = await MinecraftAccount.filter(uuid=fs.uuid).first()
    if existing_by_uuid is not None:
        logger.info(
            "ensure_mc_account: %r resolved to existing MinecraftAccount %s, "
            "stored as %r/%r -- treating as a rename",
            value, fs.uuid,
            existing_by_uuid.mc_username, existing_by_uuid.wynn_username,
        )
        return await _sync_usernames(existing_by_uuid, fs.username)
    try:
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
    except IntegrityError:
        # Lost a create race -- the activity loop, the linking flow and any
        # concurrent command can all reach MinecraftAccount.create for the
        # same UUID, and the awaits above give them plenty of room. Adopt
        # the winner's row rather than surfacing a raw DB error.
        raced = await MinecraftAccount.filter(uuid=fs.uuid).first()
        if raced is None:
            raise
        logger.info(
            "ensure_mc_account: lost a create race for %s; adopting the "
            "concurrently-created row",
            fs.uuid,
        )
        return await _sync_usernames(raced, fs.username)


async def _sync_usernames(
    mc: MinecraftAccount, wynn_username: str
) -> MinecraftAccount:
    """Bring a stored row's usernames back in line with a fresh Wynncraft
    lookup. Mutates and saves ``mc`` (only the columns that actually
    changed, so a concurrent writer's other fields survive); returns it for
    call chaining.

    Only called from :func:`ensure_mc_account`'s UUID-recheck path, where
    the row was found by UUID *after* a name-keyed lookup missed it -- so
    the stored names are provably stale and worth the write. Nothing in the
    periodic path would otherwise fix them for an account outside the
    Returners roster scan.

    ``mc_username`` goes through :func:`lib.mc.mojang.resolve_canonical_username`
    with the Wynncraft name as the tiebreaker hint, matching how the
    activity loop derives it. On total Mojang failure we take the
    Wynncraft name rather than leave a value we know to be stale.
    """
    fields: list[str] = []
    changes: list[str] = []
    if mc.wynn_username != wynn_username:
        changes.append(f"wynn_username {mc.wynn_username!r} -> {wynn_username!r}")
        mc.wynn_username = wynn_username
        fields.append("wynn_username")
    try:
        canonical = await resolve_canonical_username(mc.uuid, hint=wynn_username)
    except Exception:  # noqa: BLE001 - third-party API; best-effort contract
        logger.warning(
            "_sync_usernames: Mojang lookup failed for %s; using the "
            "Wynncraft name %r",
            mc.uuid, wynn_username,
        )
        canonical = wynn_username
    if mc.mc_username != canonical:
        changes.append(f"mc_username {mc.mc_username!r} -> {canonical!r}")
        mc.mc_username = canonical
        fields.append("mc_username")
    if fields:
        logger.info("_sync_usernames: %s: %s", mc.uuid, "; ".join(changes))
        await mc.save(update_fields=fields)
    return mc


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

    .. note:: PATCH: WYNN-STALE-WORKAROUND (2026-07-11).
       ``/v3/player/{uuid}.guild`` runs ~12 h stale for offline players
       (nowhere near its advertised ``max-age=120``) because officer-side
       writes to another player's membership don't trigger a
       regeneration of that player's materialised profile row. To
       work around this we short-circuit through ``/v3/guild/Returners``
       (which IS on the officer-write invalidation path -- Nori and
       Wynnpool invert the same way for the same reason). Rip out the
       fenced blocks below once the upstream lag is fixed.
    """
    # >>> PATCH BEGIN: WYNN-STALE-WORKAROUND (2026-07-11)
    # Reason: /v3/player/{uuid}.guild ~12h stale for offline players.
    # Remove: when Wynncraft invalidates the player endpoint on
    #   officer-write events, or exposes a webhook, or an offline-player
    #   spot-check shows <10min staleness.
    # Roster-first crosscheck: /v3/guild/Returners IS invalidated on
    # officer writes, so a hit here is authoritative. Only fall through
    # to the (stale) player endpoint when the roster says "no" -- because
    # the player endpoint is the only source of a non-Returners guild
    # name, which we still need for the HIATUS -> REGISTERED (joined
    # other guild) transition path.
    try:
        returners = await get_guild("Returners")
    except Exception:  # noqa: BLE001 - third-party API; best-effort contract
        logger.warning(
            "refresh_mc_guild: Returners roster lookup failed for %s; "
            "leaving stored guild %r untouched",
            mc.uuid, mc.guild,
        )
        return mc
    in_returners = any(
        member.uuid == mc.uuid for member in returners.members.all_members()
    )
    if in_returners:
        if mc.guild != "Returners":
            logger.info(
                "refresh_mc_guild: %s guild %r -> 'Returners' (via roster crosscheck)",
                mc.uuid, mc.guild,
            )
            mc.guild = "Returners"
            await mc.save(update_fields=["guild"])
        return mc
    # <<< PATCH END: WYNN-STALE-WORKAROUND
    try:
        fs = await get_player_stats(mc.uuid, full=True)
    except Exception:  # noqa: BLE001 - third-party API
        logger.warning(
            "refresh_mc_guild: API lookup failed for %s; using stored guild %r",
            mc.uuid, mc.guild,
        )
        return mc
    live_guild = fs.guild.name if fs.guild else None
    # >>> PATCH BEGIN: WYNN-STALE-WORKAROUND (2026-07-11)
    # Reason: /v3/player/{uuid}.guild ~12h stale for offline players.
    # Remove: when Wynncraft invalidates the player endpoint on
    #   officer-write events, or exposes a webhook, or an offline-player
    #   spot-check shows <10min staleness.
    # Roster was authoritative above and said "not in Returners". If the
    # player endpoint still names Returners, it's provably stale; coerce
    # to None so we save the truth rather than the lie. (A non-Returners
    # name here is still the freshest signal we have for that guild --
    # its own roster's invalidation path is opaque to us -- so let it
    # through unchanged.)
    if live_guild == "Returners":
        live_guild = None
    # <<< PATCH END: WYNN-STALE-WORKAROUND
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


class DonationRecipientError(ValueError):
    """Raised when :func:`resolve_donation_recipient` can't produce a
    :class:`MinecraftAccount` for the given value.

    The cog catches this and replies with the carried ``message`` directly
    (it's written for end-user consumption).
    """


_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d+)>$")
# Pure-numeric Discord snowflakes are 17-20 digits in practice. A shorter
# digits-only value is more likely a typo than a real ID, so we don't treat
# it as a Discord lookup.
_DISCORD_ID_RE = re.compile(r"^\d{15,20}$")


async def resolve_donation_recipient(
    ctx: commands.Context, value: str
) -> MinecraftAccount:
    """Resolve a ``~donations`` recipient argument to a :class:`MinecraftAccount`.

    The argument may be one of:

    1. A Discord mention (``<@123>``) or a long pure-numeric Discord ID.
       The linked :class:`MinecraftAccount` is returned; if the Discord
       user has no linked MC, raises :class:`DonationRecipientError`
       (donations need an in-game identity).
    2. A Minecraft UUID or username. Delegated to :func:`ensure_mc_account`
       (which hits Wynncraft / Mojang as needed and persists new rows).

    Raises :class:`DonationRecipientError` on unlinked Discord users or
    unresolvable MC values. :class:`~lib.mc.wynn_api.errors.WynnApiError`
    can also propagate from the MC branch when the API itself fails.
    """
    raw = value.strip()
    if not raw:
        raise DonationRecipientError("Recipient is required.")

    disc_id: Optional[str] = None
    m = _DISCORD_MENTION_RE.match(raw)
    if m:
        disc_id = m.group(1)
    elif _DISCORD_ID_RE.match(raw):
        disc_id = raw

    if disc_id is not None:
        disc = (
            await DiscordAccount.filter(disc_uuid=disc_id)
            .select_related("minecraft_account")
            .first()
        )
        if disc is None or disc.minecraft_account is None:
            raise DonationRecipientError(
                f"<@{disc_id}> has no linked Minecraft account; "
                "they need to `/link` before they can receive donations."
            )
        return disc.minecraft_account

    try:
        return await ensure_mc_account(raw)
    except WynnApiError as e:
        raise DonationRecipientError(
            f"Couldn't resolve `{raw}` as a Minecraft account: {e}"
        ) from e


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

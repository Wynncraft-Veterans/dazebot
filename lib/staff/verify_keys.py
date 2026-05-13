"""VerifyKey helpers: issuance, revocation, and HTTP introspection.

The user-facing flow is: a Discord user runs ``/vetsmod``, dazebot DMs them
(or, if DMs are closed, falls back to a public ping) the modrinth link plus
``/unlock <key>``. The user pastes that into Minecraft chat; the vetsmod
client intercepts the slash command, stores the key, and uses it to auth
its WebSocket connection to ``api.wynnvets.org``.

``temporary-server`` validates each WS connection by POSTing the key to
:func:`introspect`. The result includes the *current* tier (re-resolved on
every call against live Discord roles + WAPI guild membership), so changes
in role state propagate without re-issuing the key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import secrets
from typing import TYPE_CHECKING, Optional

import discord

from lib.role_state import RoleState, state_of
from orm import Blocklist, DiscordAccount, MinecraftAccount, VerifyKey, Waitlist

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger("dazebot.lib.staff.verify_keys")


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------

# Canonical tier values stored on VerifyKey rows and returned to clients.
TIER_MEMBER = "member"
TIER_WAITLIST = "waitlist"
TIER_HONOURARY = "honourary"
TIER_OTHER = "other"

ALL_TIERS = (TIER_MEMBER, TIER_WAITLIST, TIER_HONOURARY, TIER_OTHER)


def tier_to_ws_protocol(tier: str) -> Optional[str]:
    """Map our canonical tier to the existing v1 WS register-frame tier value.

    The protocol predates this auth flow and only knows ``guild``, ``waitlist``,
    and ``honourary``. ``other`` has no protocol tier (no chat-channel access).
    """
    if tier == TIER_MEMBER:
        return "guild"
    if tier in (TIER_WAITLIST, TIER_HONOURARY):
        return tier
    return None


@dataclass
class ResolvedTier:
    tier: str
    mc_uuid: Optional[str]
    mc_username: Optional[str]
    blocked: bool


async def resolve_tier(member: discord.Member) -> ResolvedTier:
    """Resolve the current tier for a Discord member from their roles + DB.

    Order of precedence:
      1. Blocklisted (any linked MC account on the blocklist) -> ``other`` (blocked=True)
      2. Honourary role -> ``honourary``
      3. Member role (in-game Returners) -> ``member``
      4. Waitlisted role OR row in Waitlist table -> ``waitlist``
      5. anything else -> ``other``

    The MC identity comes from the *primary* DiscordAccount.minecraft_account
    link. Alts are not considered: a single key authenticates a single
    primary identity for chat purposes.
    """
    state = state_of(member)
    disc = (
        await DiscordAccount.filter(disc_uuid=str(member.id))
        .select_related("minecraft_account")
        .first()
    )
    mc: Optional[MinecraftAccount] = disc.minecraft_account if disc else None
    mc_uuid = mc.uuid if mc else None
    mc_username = (mc.mc_username if mc else None) or (mc.wynn_username if mc else None)

    if mc is not None:
        blocked = await Blocklist.filter(minecraft_account=mc).exists()
        if blocked:
            return ResolvedTier(
                tier=TIER_OTHER, mc_uuid=mc_uuid, mc_username=mc_username, blocked=True
            )

    if RoleState.HONOURARY in state:
        return ResolvedTier(
            tier=TIER_HONOURARY, mc_uuid=mc_uuid, mc_username=mc_username, blocked=False
        )
    if RoleState.MEMBER in state:
        return ResolvedTier(
            tier=TIER_MEMBER, mc_uuid=mc_uuid, mc_username=mc_username, blocked=False
        )
    if RoleState.WAITLISTED in state:
        return ResolvedTier(
            tier=TIER_WAITLIST, mc_uuid=mc_uuid, mc_username=mc_username, blocked=False
        )
    # Fallback: a DB Waitlist row without the role still counts as waitlist.
    if mc is not None and await Waitlist.filter(minecraft_account=mc).exists():
        return ResolvedTier(
            tier=TIER_WAITLIST, mc_uuid=mc_uuid, mc_username=mc_username, blocked=False
        )
    return ResolvedTier(
        tier=TIER_OTHER, mc_uuid=mc_uuid, mc_username=mc_username, blocked=False
    )


# ---------------------------------------------------------------------------
# Issuance / revocation
# ---------------------------------------------------------------------------


def _generate_key() -> str:
    """43-char URL-safe base64 token (32 bytes of entropy).

    Fits comfortably within Minecraft's 256-character chat limit even after
    ``/unlock `` (8 chars) is prepended.
    """
    return secrets.token_urlsafe(32)


@dataclass
class IssuedKey:
    row: VerifyKey
    is_new: bool


async def get_or_issue_key(member: discord.Member) -> IssuedKey:
    """Return the user's VerifyKey row, creating one if it doesn't exist.

    Re-running ``/vetsmod`` returns the same key (analogous to ``LinkCode``
    reuse). Tier is refreshed every call so a user whose role changed
    between issuance and re-run sees the up-to-date snapshot.

    A revoked row is treated as absent and a fresh key is minted.
    """
    resolved = await resolve_tier(member)

    existing = await VerifyKey.filter(disc_uuid=str(member.id)).first()
    if existing is not None and existing.revoked_at is None:
        # Refresh tier snapshot on the existing row so /vetsmod surfacing
        # always shows the user's current access level.
        if (
            existing.tier != resolved.tier
            or existing.mc_uuid != resolved.mc_uuid
            or existing.mc_username != resolved.mc_username
        ):
            existing.tier = resolved.tier
            existing.mc_uuid = resolved.mc_uuid
            existing.mc_username = resolved.mc_username
            await existing.save(
                update_fields=["tier", "mc_uuid", "mc_username"]
            )
        logger.info(
            "verify key reused: disc=%s tier=%s mc=%s",
            member.id, resolved.tier, resolved.mc_username,
        )
        return IssuedKey(row=existing, is_new=False)

    if existing is not None:
        # Revoked row -> rotate in place rather than collide on disc_uuid unique.
        existing.key = _generate_key()
        existing.tier = resolved.tier
        existing.mc_uuid = resolved.mc_uuid
        existing.mc_username = resolved.mc_username
        existing.revoked_at = None
        existing.last_used_at = None
        await existing.save(
            update_fields=["key", "tier", "mc_uuid", "mc_username", "revoked_at", "last_used_at"]
        )
        logger.info(
            "verify key un-revoked + rotated: disc=%s tier=%s",
            member.id, resolved.tier,
        )
        return IssuedKey(row=existing, is_new=True)

    row = await VerifyKey.create(
        key=_generate_key(),
        disc_uuid=str(member.id),
        mc_uuid=resolved.mc_uuid,
        mc_username=resolved.mc_username,
        tier=resolved.tier,
    )
    logger.info(
        "verify key issued: disc=%s tier=%s mc=%s",
        member.id, resolved.tier, resolved.mc_username,
    )
    return IssuedKey(row=row, is_new=True)


async def rotate_key(member: discord.Member) -> VerifyKey:
    """Force a new key for this user, invalidating the previous one.

    Use when the user reports a leaked/lost key. The new key is bound to
    the same Discord account; existing vetsmod installs that still hold
    the old key will start failing introspection on next reconnect.
    """
    resolved = await resolve_tier(member)
    row = await VerifyKey.filter(disc_uuid=str(member.id)).first()
    if row is None:
        return (await get_or_issue_key(member)).row
    row.key = _generate_key()
    row.tier = resolved.tier
    row.mc_uuid = resolved.mc_uuid
    row.mc_username = resolved.mc_username
    row.revoked_at = None
    row.last_used_at = None
    await row.save(
        update_fields=["key", "tier", "mc_uuid", "mc_username", "revoked_at", "last_used_at"]
    )
    logger.info("verify key rotated: disc=%s tier=%s", member.id, resolved.tier)
    return row


async def revoke_key(disc_uuid: str, *, reason: str | None = None) -> bool:
    """Mark a user's key as revoked. Returns True if a row was found and updated.

    The row is kept (not deleted) so /vetsmod can show "your key was revoked
    on <date>" instead of silently re-issuing.
    """
    row = await VerifyKey.filter(disc_uuid=disc_uuid).first()
    if row is None:
        return False
    if row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    await row.save(update_fields=["revoked_at"])
    logger.info("verify key revoked: disc=%s reason=%s", disc_uuid, reason or "unspecified")
    return True


# ---------------------------------------------------------------------------
# Introspection (called by temporary-server)
# ---------------------------------------------------------------------------


@dataclass
class IntrospectionResult:
    valid: bool
    disc_uuid: Optional[str] = None
    mc_uuid: Optional[str] = None
    mc_username: Optional[str] = None
    tier: Optional[str] = None
    ws_tier: Optional[str] = None
    reason: Optional[str] = None


async def introspect(bot: Bot, key: str) -> IntrospectionResult:
    """Validate a key and return the *current* identity/tier for it.

    The tier is re-resolved on every call against the user's live Discord
    roles + linked MC account. This means:
      - A waitlister promoted to member doesn't need to re-/vetsmod.
      - A user who is blocked or leaves the guild loses access immediately
        on next WS reconnect (or whenever temporary-server's cache expires).

    On any internal lookup failure (member not in cache, REST error) we
    fall back to the snapshot stored on the row. This keeps clients
    working during transient Discord outages.
    """
    if not key:
        return IntrospectionResult(valid=False, reason="empty key")

    row = await VerifyKey.filter(key=key).first()
    if row is None:
        return IntrospectionResult(valid=False, reason="unknown key")
    if row.revoked_at is not None:
        return IntrospectionResult(
            valid=False,
            disc_uuid=row.disc_uuid,
            reason=f"revoked at {row.revoked_at.isoformat()}",
        )

    # Re-resolve current tier. This pulls live roles, so the answer is
    # authoritative for "what are they right now".
    refreshed_tier = row.tier
    refreshed_mc_uuid = row.mc_uuid
    refreshed_mc_username = row.mc_username
    try:
        member = _find_member(bot, int(row.disc_uuid))
        if member is not None:
            resolved = await resolve_tier(member)
            refreshed_tier = resolved.tier
            refreshed_mc_uuid = resolved.mc_uuid
            refreshed_mc_username = resolved.mc_username
            if (
                row.tier != refreshed_tier
                or row.mc_uuid != refreshed_mc_uuid
                or row.mc_username != refreshed_mc_username
            ):
                row.tier = refreshed_tier
                row.mc_uuid = refreshed_mc_uuid
                row.mc_username = refreshed_mc_username
                await row.save(update_fields=["tier", "mc_uuid", "mc_username"])
    except Exception:  # noqa: BLE001 - introspection must never raise
        logger.exception("introspect: tier refresh failed for disc=%s", row.disc_uuid)

    row.last_used_at = datetime.now(timezone.utc)
    try:
        await row.save(update_fields=["last_used_at"])
    except Exception:  # noqa: BLE001 - bookkeeping, don't fail the request
        logger.exception("introspect: last_used_at write failed for disc=%s", row.disc_uuid)

    return IntrospectionResult(
        valid=True,
        disc_uuid=row.disc_uuid,
        mc_uuid=refreshed_mc_uuid,
        mc_username=refreshed_mc_username,
        tier=refreshed_tier,
        ws_tier=tier_to_ws_protocol(refreshed_tier),
    )


def _find_member(bot: Bot, disc_uuid_int: int) -> Optional[discord.Member]:
    for guild in bot.guilds:
        m = guild.get_member(disc_uuid_int)
        if m is not None:
            return m
    return None

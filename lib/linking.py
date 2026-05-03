"""Shared linking helpers used by both the Discord cog and the FastAPI
``/api/auth`` endpoint.

The flow is:

1. Discord user runs ``/link_code <mc_username>`` (or clicks the welcome
   message's link button). We persist (or reuse) a ``LinkCode`` row keyed
   on the lowercased mc_username and DM them the code.
2. The Picolimbo mini-server forwards every chat message to ``/api/auth``.
   When we see a message from ``<mc_username>`` containing the active code,
   we couple the two accounts and delete the ``LinkCode`` row.

The code lives in the DB, so it survives bot restarts and never times out.
A different discord user requesting the same mc_username overwrites the row,
implicitly invalidating the prior code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import secrets
from typing import TYPE_CHECKING

import discord

from lib.role_state import ensure_linked_baseline
from lib.wynn_api.player import get_player_full_stats
from orm import Blocklist, DiscordAccount, LinkCode, MinecraftAccount

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger("dazebot.lib.linking")


@dataclass
class LinkCompletion:
    """Outcome of attempting to consume a link code from in-game chat."""

    success: bool
    reason: str  # human-readable, used in DM and logs
    discord_user: discord.User | discord.Member | None = None
    mc_account: MinecraftAccount | None = None


# Excludes visually-confusable chars (0/O, 1/I/L) so users typing the code
# in Minecraft chat don't trip on them.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


async def get_or_issue_code(disc_uuid: str, mc_username: str) -> tuple[LinkCode, bool]:
    """Returns ``(row, is_new)``.

    Behaviour matches the spec:
      * Same discord user, same username \u2192 reuse existing code (is_new=False).
      * Different discord user, same username \u2192 invalidate (overwrite) and
        issue a new code (is_new=True).
      * No existing row \u2192 issue (is_new=True).
    """
    key = mc_username.lower()
    existing = await LinkCode.filter(mc_username=key).first()
    if existing is not None and existing.disc_uuid == disc_uuid:
        logger.info(
            "link code reused: disc=%s mc=%s code=%s",
            disc_uuid, key, existing.code,
        )
        return existing, False
    if existing is not None:
        prev_disc = existing.disc_uuid
        existing.disc_uuid = disc_uuid
        existing.code = _generate_code()
        await existing.save(update_fields=["disc_uuid", "code", "updated_at"])
        logger.info(
            "link code rotated: mc=%s new_disc=%s prev_disc=%s code=%s",
            key, disc_uuid, prev_disc, existing.code,
        )
        return existing, True
    row = await LinkCode.create(
        mc_username=key,
        disc_uuid=disc_uuid,
        code=_generate_code(),
    )
    logger.info(
        "link code issued: disc=%s mc=%s code=%s",
        disc_uuid, key, row.code,
    )
    return row, True


async def try_consume_code(
    bot: Bot, mc_uuid: str, mc_username: str, message: str
) -> LinkCompletion | None:
    """Called from the API for every in-game chat message.

    Returns ``None`` if there is no pending code for this username (the common
    case \u2014 chat is noisy). Returns a ``LinkCompletion`` describing the
    outcome otherwise.
    """
    key = mc_username.lower()
    row = await LinkCode.filter(mc_username=key).first()
    if row is None:
        return None
    # Case-insensitive substring match — Minecraft chat is shouty.
    if row.code.upper() not in message.upper():
        return None  # not the right message; keep waiting
    logger.info(
        "link code consumed: mc=%s mc_uuid=%s disc=%s code=%s",
        key, mc_uuid, row.disc_uuid, row.code,
    )

    # Resolve the discord user up-front for logging / DM.
    try:
        discord_user = await bot.fetch_user(int(row.disc_uuid))
    except (ValueError, discord.HTTPException):
        discord_user = None

    # If the MC account is already linked to someone else \u2192 refuse.
    existing_link = await DiscordAccount.filter(minecraft_account__uuid=mc_uuid).first()
    if existing_link is not None and existing_link.disc_uuid != row.disc_uuid:
        await row.delete()
        return LinkCompletion(
            success=False,
            reason=(
                f"`{mc_username}` is already linked to another Discord account. "
                "Ask them to /unlink first."
            ),
            discord_user=discord_user,
        )

    disc, _ = await DiscordAccount.get_or_create(disc_uuid=row.disc_uuid)
    if disc.minecraft_account_id is not None:
        existing_mc = await MinecraftAccount.get(id=disc.minecraft_account_id)
        await row.delete()
        return LinkCompletion(
            success=False,
            reason=(
                f"You are already linked to `{existing_mc.mc_username}`. "
                "Ask staff to /unlink you first."
            ),
            discord_user=discord_user,
        )

    mc = await MinecraftAccount.filter(uuid=mc_uuid).first()
    if mc is None:
        try:
            fs = await get_player_full_stats(mc_uuid)
            mc = await MinecraftAccount.create(
                uuid=mc_uuid,
                wynn_username=fs.username,
                mc_username=mc_username,
                # Populate guild from the live API so ensure_linked_baseline
                # below can correctly grant MEMBER instead of REGISTERED for
                # users who joined Returners between activity-loop ticks.
                guild=fs.guild.name if fs.guild else None,
                last_online=fs.lastJoin or datetime.fromtimestamp(0, tz=timezone.utc),
                last_manual_check=datetime.fromtimestamp(0, tz=timezone.utc),
                first_join=fs.firstJoin,  # may be None per Wynncraft privacy opt-out
            )
        except Exception as e:  # noqa: BLE001  \u2014 third-party API
            logger.exception("try_consume_code: failed to fetch player stats")
            return LinkCompletion(
                success=False,
                reason=f"Failed to fetch Wynncraft stats for `{mc_username}`: {e}",
                discord_user=discord_user,
            )

    disc.minecraft_account = mc
    await disc.save()
    await row.delete()

    # Refresh mc.guild from the live API before enforcing the baseline so
    # the MEMBER vs REGISTERED decision is based on current truth, not on
    # whatever the activity loop happened to cache last. Best-effort: if the
    # API call fails, we fall back to whatever is stored.
    try:
        fs = await get_player_full_stats(mc_uuid)
        live_guild = fs.guild.name if fs.guild else None
        if mc.guild != live_guild:
            mc.guild = live_guild
            await mc.save(update_fields=["guild"])
    except Exception:  # noqa: BLE001 - third-party API
        logger.warning("try_consume_code: guild refresh failed for %s; using stored value", mc_uuid)

    # Enforce the linked-account role invariant immediately. Without this,
    # a user who linked AFTER joining Returners would never get the MEMBER
    # role, since the activity loop only fires JOINED_VETS for in-game
    # join events. ensure_linked_baseline is idempotent and safe to retry.
    try:
        await _enforce_linked_baseline_for(bot, row.disc_uuid, mc)
    except Exception:  # noqa: BLE001 - role enforcement must never break linking
        logger.exception("try_consume_code: failed to enforce linked baseline for %s", row.disc_uuid)

    return LinkCompletion(
        success=True,
        reason=f"Linked to `{mc.mc_username}` (`{mc.uuid}`).",
        discord_user=discord_user,
        mc_account=mc,
    )


async def _enforce_linked_baseline_for(bot: Bot, disc_uuid: str, mc: MinecraftAccount) -> None:
    """Look up the Discord member across the bot's guilds and call
    ``ensure_linked_baseline`` so they end up with REGISTERED or MEMBER as
    appropriate. No-op if the user is not in any guild the bot can see.
    """
    in_returners = mc.guild == "Returners"
    blocked = await Blocklist.filter(minecraft_account=mc).exists()
    try:
        uid = int(disc_uuid)
    except ValueError:
        return
    for guild in bot.guilds:
        member = guild.get_member(uid)
        if member is None:
            continue
        try:
            await ensure_linked_baseline(
                member,
                in_returners=in_returners,
                blocked=blocked,
                reason="link_completed",
            )
        except discord.HTTPException as e:
            logger.warning(f"ensure_linked_baseline failed for {member} in {guild}: {e}")


async def dm_or_log(
    user: discord.abc.User, content: str, *, fallback_logger: logging.Logger | None = None
) -> bool:
    """Best-effort DM. Returns True on success, False if DMs are closed or any
    other HTTP error occurs. The caller is responsible for surfacing the
    fallback to the user (e.g. ephemeral reply).
    """
    log = fallback_logger or logger
    try:
        dm = await user.create_dm()
        await dm.send(content)
        return True
    except discord.Forbidden:
        log.info(f"DM blocked: {user} ({user.id}) has DMs closed")
        return False
    except discord.HTTPException as e:
        log.warning(f"DM failed for {user} ({user.id}): {e}")
        return False

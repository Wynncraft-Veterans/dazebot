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
import re
import secrets
from typing import TYPE_CHECKING

import discord

from lib.mc.wynn_api.player import get_player_stats
from lib.role_state import ensure_linked_baseline
from orm import Blocklist, DiscordAccount, LinkCode, MinecraftAccount

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger("dazebot.lib.mc.linking")


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

# Compiled once: matches any whitespace-bounded run of CODE_LENGTH chars from
# the alphabet. Used purely for forensic logging in try_consume_code so that a
# user typing a valid-looking code that doesn't match any pending row shows up
# in the logs instead of silently dissolving into a 200 OK.
_CODE_SHAPED_RE = re.compile(rf"(?<![A-Z0-9])[{_CODE_ALPHABET}]{{{_CODE_LENGTH}}}(?![A-Z0-9])")


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

    # Primary path: row matches by username AND message contains the code.
    if row is not None and row.code.upper() in message.upper():
        pass  # fall through to the "consume" block below
    else:
        # Fallback path: the username didn't match (typo in the modal, account
        # rename, capitalisation), but if the message contains a code that
        # uniquely matches exactly one pending LinkCode, we can still trust
        # it -- the 6-char code from _CODE_ALPHABET is effectively a shared
        # secret. The disc_uuid on the row is the source of truth for who
        # gets linked, so the username they typed in the modal is irrelevant.
        msg_upper = message.upper()
        shaped = _CODE_SHAPED_RE.findall(msg_upper)
        if not shaped:
            if row is not None:
                # Row exists for this username but no code in the message --
                # just normal chat from someone with a pending link.
                logger.debug(
                    "try_consume_code: row exists for mc=%s but no shaped code in message (len=%d)",
                    key, len(message),
                )
            return None

        candidates = await LinkCode.filter(code__in=shaped).all()
        if not candidates:
            logger.info(
                "try_consume_code: ignoring shaped code(s) %s from mc=%s mc_uuid=%s "
                "(no LinkCode rows match)",
                shaped, key, mc_uuid,
            )
            return None
        if len(candidates) > 1:
            logger.warning(
                "try_consume_code: ambiguous shaped code(s) %s from mc=%s mc_uuid=%s "
                "matched %d LinkCode rows: %s -- refusing to guess",
                shaped, key, mc_uuid, len(candidates),
                [(c.mc_username, c.disc_uuid) for c in candidates],
            )
            return None

        # Exactly one candidate. Use it, but log loudly that the username
        # didn't match so we can keep an eye on this fallback path.
        row = candidates[0]
        logger.warning(
            "try_consume_code: username fallback -- chat from mc=%s mc_uuid=%s contained "
            "code %s which belongs to a row registered under mc_username=%r (disc=%s). "
            "Accepting via code-uniqueness; user typed wrong/old name in the link modal.",
            key, mc_uuid, row.code, row.mc_username, row.disc_uuid,
        )

    logger.info(
        "link code consumed: mc=%s mc_uuid=%s disc=%s code=%s row_mc_username=%s",
        key, mc_uuid, row.disc_uuid, row.code, row.mc_username,
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

    # ensure_mc_account: creates the row (populating guild from the live API)
    # if we've never seen this UUID, otherwise returns the existing row.
    from lib.mc.resolve import ensure_mc_account

    try:
        mc = await ensure_mc_account(mc_uuid)
    except Exception as e:  # noqa: BLE001  \u2014 third-party API
        logger.exception("try_consume_code: failed to fetch player stats")
        return LinkCompletion(
            success=False,
            reason=f"Failed to fetch Wynncraft stats for `{mc_username}`: {e}",
            discord_user=discord_user,
        )

    disc.minecraft_account = mc
    await disc.save()

    # Refresh mc.guild from the live API before enforcing the baseline so
    # the MEMBER vs REGISTERED decision is based on current truth, not on
    # whatever the activity loop happened to cache last. Best-effort: if the
    # API call fails, we fall back to whatever is stored.
    try:
        fs = await get_player_stats(mc_uuid, full=True)
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
    #
    # IMPORTANT: only delete the LinkCode row after enforcement confirms the
    # member was found in at least one guild and roles applied without
    # error. If enforcement can't find the member (cache cold + REST race,
    # bot recently joined, etc.), we KEEP the row. The data-integrity janitor
    # (cogs/maintenance/janitor.py reconciler B) re-attempts enforcement for
    # kept rows on its interval and applies this same delete/keep contract, so
    # recovery no longer depends solely on the player re-sending the code or a
    # staff /link set (those still work too). Without this, a user could end up
    # with the DiscordAccount<->MinecraftAccount link in the DB but neither the
    # MEMBER nor REGISTERED role -- the exact invariant violation we're
    # protecting against. See .claude/linking.md.
    enforcement_ok = False
    try:
        enforcement_ok = await _enforce_linked_baseline_for(bot, row.disc_uuid, mc)
    except Exception:  # noqa: BLE001 - role enforcement must never break linking
        logger.exception("try_consume_code: failed to enforce linked baseline for %s", row.disc_uuid)

    if enforcement_ok:
        await row.delete()
    else:
        logger.error(
            "try_consume_code: KEEPING LinkCode row for disc=%s mc=%s because role "
            "enforcement did not confirm success. The janitor (cogs/maintenance/"
            "janitor.py reconciler B) will re-attempt this on its interval; a "
            "player code re-send or staff /link set also recovers it. "
            "See .claude/linking.md.",
            row.disc_uuid, key,
        )

    return LinkCompletion(
        success=True,
        reason=f"Linked to `{mc.mc_username}` (`{mc.uuid}`).",
        discord_user=discord_user,
        mc_account=mc,
    )


async def _enforce_linked_baseline_for(bot: Bot, disc_uuid: str, mc: MinecraftAccount) -> bool:
    """Look up the Discord member across the bot's guilds and call
    ``ensure_linked_baseline`` so they end up with REGISTERED or MEMBER as
    appropriate.

    Returns ``True`` iff the member was found in at least one guild AND the
    ``ensure_linked_baseline`` call for every guild they were found in
    completed without raising. ``False`` otherwise -- callers MUST treat
    ``False`` as "don't delete the LinkCode row, retry later".
    """
    in_returners = mc.guild == "Returners"
    in_other_guild = mc.guild is not None and mc.guild != "Returners"
    blocked = await Blocklist.filter(minecraft_account=mc).exists()
    logger.info(
        "enforce_baseline: start disc=%s mc_uuid=%s mc_guild=%r in_returners=%s blocked=%s",
        disc_uuid, mc.uuid, mc.guild, in_returners, blocked,
    )
    try:
        uid = int(disc_uuid)
    except ValueError:
        logger.error("enforce_baseline: disc_uuid %r is not an int, aborting", disc_uuid)
        return False
    found_anywhere = False
    all_applied_cleanly = True
    for guild in bot.guilds:
        member = guild.get_member(uid)
        if member is None:
            # Cache miss. Fall back to a REST fetch so we don't silently skip
            # users whose member objects haven't been chunked yet.
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                logger.debug(
                    "enforce_baseline: %s not in guild %s (%s)",
                    uid, guild.name, guild.id,
                )
                continue
            except discord.HTTPException as e:
                logger.warning(
                    "enforce_baseline: fetch_member(%s) in %s failed: %s",
                    uid, guild.name, e,
                )
                continue
            logger.info(
                "enforce_baseline: cache miss for %s in %s, used REST fetch",
                uid, guild.name,
            )
        found_anywhere = True
        try:
            await ensure_linked_baseline(
                member,
                in_returners=in_returners,
                in_other_guild=in_other_guild,
                blocked=blocked,
                reason="link_completed",
            )
            logger.info(
                "enforce_baseline: applied for %s (%s) in %s",
                member, member.id, guild.name,
            )
        except discord.HTTPException as e:
            logger.warning(f"ensure_linked_baseline failed for {member} in {guild}: {e}")
            all_applied_cleanly = False
    if not found_anywhere:
        logger.error(
            "enforce_baseline: disc %s not found in ANY of %d bot guilds; "
            "user will not receive MEMBER/REGISTERED role!",
            uid, len(bot.guilds),
        )
        return False
    return all_applied_cleanly


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

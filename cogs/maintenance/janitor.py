"""Data-integrity janitor.

A periodic reconciliation loop that detects (and, when
``JANITOR_REPAIR_ENABLED`` is set, repairs) recoverable data problems the
event-driven code paths can miss. Regardless of repair mode it posts a
throttled summary to ``CurrConfig.JANITOR_ALERT_CHANNEL`` so staff notice
even when nobody reads the logs.

Reconcilers (run in this order):

* **(F) ``reconcile_blocklisted_registered_only``** — a blocklisted user's
  linked member must be REGISTERED-only. Runs first so the strongest
  invariant wins.
* **(A) ``reconcile_linked_baseline``** — the "Flame" self-heal, *strictly*:
  only linked members holding **no** membership state at all (none of
  REGISTERED/MEMBER/HIATUS/HONOURARY) get the baseline granted (MEMBER if in
  Returners, else REGISTERED — additive, never a demotion). Members with any
  valid primary role are left **untouched** — HIATUS and HONOURARY are
  legitimate manually-assigned states (e.g. a pre-dazebot member parked on
  HIATUS) and must not be downgraded without positive information (a guild
  change, which the activity loop already handles). Genuine multi-primary
  conflicts (e.g. MEMBER+REGISTERED) are intentionally left for manual review.
* **(D) ``reconcile_waitlist_role``** — keeps the WAITLISTED role in sync with
  the Waitlist DB table (the source of truth): an orphaned role with no DB row
  is removed; a DB row whose member lacks the role gets it. Runs after (A) so a
  just-healed stateless member can be waitlisted.
* **(B) ``reconcile_kept_linkcodes``** — cleans leftover ``LinkCode`` rows.
  Unrecoverable rows are deleted (MC linked elsewhere / user in no guild). For
  a row whose user is linked: if the user already has a valid primary state
  the link plainly succeeded and the row is just a stale leftover → delete it,
  **never touch roles**; only a *stateless* user is a real kept-on-failure →
  additively re-enforce the baseline (same Flame rule as (A)) then clear the
  code.

Operational notes:

* ``JANITOR_ENABLED`` and ``JANITOR_INTERVAL_MINUTES`` are read at
  ``__init__`` / decoration time → toggling them needs a restart.
* ``JANITOR_REPAIR_ENABLED`` is re-read every tick → the live mutation
  kill-switch (log-only vs. enforce).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from bot import Bot
from config import Config, CurrConfig
from lib.auth import is_staff
from lib.mc.linking import _enforce_linked_baseline_for
from lib.mc.wynn_api.guild import get_guild
from lib.mc.wynn_api.player import get_player_stats
from lib.role_state import (
    RoleState,
    Trigger,
    apply_transition,
    fire_trigger_for_mc_uuids,
    force_to_registered_only,
    hiatus_member_uuids,
    resolve_guild_member,
    state_of,
)
from orm import Blocklist, DiscordAccount, JanitorAlert, LinkCode, MinecraftAccount

logger = logging.getLogger("dazebot.cogs.maintenance.janitor")

# Primary (mutually-exclusive) membership-state flags, in the same set the
# role-state machine treats as primary. WAITLISTED is additive and excluded.
_PRIMARY = (RoleState.REGISTERED, RoleState.MEMBER, RoleState.HIATUS, RoleState.HONOURARY)

_LABELS = {
    "blocklist": "(F) Blocklist → REGISTERED",
    "baseline": "(A) No-state self-heal (Flame)",
    "waitlist": "(D) WAITLISTED ↔ DB sync",
    "linkcodes": "(B) Stale / kept LinkCodes",
    "hiatus_guild": "(H) HIATUS holders who are in a guild",
}


@dataclass
class ReconcilerResult:
    name: str
    scanned: int = 0
    flagged: int = 0
    repaired: int = 0
    errors: int = 0
    samples: list[str] = field(default_factory=list)

    @property
    def has_activity(self) -> bool:
        return bool(self.flagged or self.repaired or self.errors)


def _fmt_state(state: RoleState) -> str:
    """Render a (possibly combined) RoleState flag set as ``A|B`` names."""
    parts = [s.name for s in RoleState if s is not RoleState.NONE and s in state]
    return "|".join(parts) if parts else "NONE"


async def _resolve_member(
    bot: Bot, disc_uuid: str
) -> tuple[discord.Member | None, bool]:
    """Resolve a Discord member across the bot's guilds via the shared
    ``resolve_guild_member`` helper (get_member → REST fetch_member fallback).

    Returns ``(member_or_None, found_anywhere)``. ``found_anywhere`` is False
    only when the user is in *no* guild at all (a cache miss is resolved via
    the REST fetch first, so it does not count as "no guild").
    """
    member = await resolve_guild_member(bot, disc_uuid)
    return member, member is not None


# --------------------------------------------------------------------------
# Reconcilers. Uniform signature: (bot, *, enforce) -> ReconcilerResult.
# Each per-item op is wrapped so one bad row never aborts the sweep.
# --------------------------------------------------------------------------


async def reconcile_blocklisted_registered_only(
    bot: Bot, *, enforce: bool
) -> ReconcilerResult:
    """(F) A blocklisted user's linked member must hold REGISTERED only."""
    r = ReconcilerResult(name="blocklist")
    blocks = await Blocklist.all().select_related("minecraft_account")
    for block in blocks:
        try:
            mc = block.minecraft_account
            disc = await DiscordAccount.filter(minecraft_account_id=mc.id).first()
            if disc is None:
                continue  # preemptive block (user not linked yet) — valid, not a violation
            r.scanned += 1
            member, _ = await _resolve_member(bot, disc.disc_uuid)
            if member is None:
                continue  # not in guild → no roles to enforce here
            state = state_of(member)
            violating = any(
                s in state
                for s in (RoleState.MEMBER, RoleState.HIATUS, RoleState.HONOURARY, RoleState.WAITLISTED)
            )
            if not violating:
                continue
            r.flagged += 1
            r.samples.append(
                f"<@{disc.disc_uuid}> `{mc.mc_username}` state={_fmt_state(state)} → REGISTERED"
                + ("" if enforce else " (would fix)")
            )
            if enforce:
                try:
                    await force_to_registered_only(member, reason="janitor:blocklist")
                    r.repaired += 1
                except discord.HTTPException as e:
                    r.errors += 1
                    logger.warning("janitor (F): force_to_registered_only failed for %s: %s", member, e)
        except Exception:  # noqa: BLE001 — one bad row must not abort the sweep
            r.errors += 1
            logger.exception("janitor (F): unexpected error on a blocklist row")
    return r


async def reconcile_linked_baseline(bot: Bot, *, enforce: bool) -> ReconcilerResult:
    """(A) The Flame self-heal, strictly: a linked member holding **no**
    membership state at all (none of REGISTERED/MEMBER/HIATUS/HONOURARY) gets
    the baseline granted. A member with any valid primary role is left
    untouched — HIATUS/HONOURARY are legitimate manual states and must never
    be demoted here; genuine multi-primary conflicts are left for manual
    review. Enforce delegates to ``_enforce_linked_baseline_for``, which for a
    stateless member is purely additive (nothing to strip → never a demotion).
    """
    r = ReconcilerResult(name="baseline")
    discs = await DiscordAccount.filter(
        minecraft_account_id__isnull=False
    ).select_related("minecraft_account")
    for disc in discs:
        try:
            mc = disc.minecraft_account
            r.scanned += 1
            member, _ = await _resolve_member(bot, disc.disc_uuid)
            if member is None:
                continue  # not in guild → ensure_linked_baseline can't act anyway
            state = state_of(member)
            if any(s in state for s in _PRIMARY):
                # Has a valid primary role (REGISTERED/MEMBER/HIATUS/HONOURARY).
                # Leave it ALONE — HIATUS/HONOURARY are legitimate manual
                # states; genuine conflicts are for manual review. We only
                # heal the true Flame class: no membership state at all.
                continue
            r.flagged += 1
            in_returners = mc.guild == "Returners"
            blocked = await Blocklist.filter(minecraft_account_id=mc.id).exists()
            target_name = "MEMBER" if (in_returners and not blocked) else "REGISTERED"
            r.samples.append(
                f"<@{disc.disc_uuid}> `{mc.mc_username}` no membership state "
                f"→ {target_name}" + ("" if enforce else " (would grant)")
            )
            if enforce:
                try:
                    ok = await _enforce_linked_baseline_for(bot, disc.disc_uuid, mc)
                except Exception:  # noqa: BLE001 — enforcement must never abort the sweep
                    ok = False
                    logger.exception("janitor (A): _enforce_linked_baseline_for crashed for %s", disc.disc_uuid)
                if ok:
                    r.repaired += 1
                else:
                    r.errors += 1
                    logger.warning(
                        "janitor (A): baseline enforce still failing for <@%s> (%s)",
                        disc.disc_uuid, mc.mc_username,
                    )
        except Exception:  # noqa: BLE001
            r.errors += 1
            logger.exception("janitor (A): unexpected error on a linked account")
    return r


async def reconcile_waitlist_role(bot: Bot, *, enforce: bool) -> ReconcilerResult:
    """(D) Keep the WAITLISTED role in sync with the Waitlist DB table.

    The Waitlist table is the source of truth for "is on the in-game guild
    waitlist". An orphaned WAITLISTED role (no DB row) or a DB row whose
    member is missing the role is a fixable divergence (e.g. a delete path
    that didn't strip the role, or `/waitlist self` that didn't add it).

    - role held but **no** Waitlist row → remove it (`INACTIVE_WAITLIST`).
    - Waitlist row but role **missing** → add it (`ADDED_TO_WAITLIST`); this
      respects the transition guards (refuses for a MEMBER / no-state user —
      recorded as an error sample, not forced). Runs *after* (A) so a
      stateless user has already been granted REGISTERED and can be waitlisted.

    Scoped to linked members (an unlinked member with a stray WAITLISTED role
    is the separate, de-scoped "unlinked but roled" class).
    """
    r = ReconcilerResult(name="waitlist")
    # disc_uuids that SHOULD hold WAITLISTED: those whose linked MC account
    # has a Waitlist row.
    should = set(
        await DiscordAccount.filter(
            minecraft_account__waitlist__id__isnull=False
        ).values_list("disc_uuid", flat=True)
    )
    discs = await DiscordAccount.filter(
        minecraft_account_id__isnull=False
    ).select_related("minecraft_account")
    for disc in discs:
        try:
            r.scanned += 1
            member, _ = await _resolve_member(bot, disc.disc_uuid)
            if member is None:
                continue
            has_wl = RoleState.WAITLISTED in state_of(member)
            on_list = disc.disc_uuid in should
            if has_wl == on_list:
                continue  # consistent — nothing to do
            r.flagged += 1
            mc = disc.minecraft_account
            if has_wl and not on_list:
                r.samples.append(
                    f"<@{disc.disc_uuid}> `{mc.mc_username}` has WAITLISTED but no DB row "
                    f"→ remove role" + ("" if enforce else " (would fix)")
                )
                if enforce:
                    try:
                        res = await apply_transition(
                            member, Trigger.INACTIVE_WAITLIST, reason="janitor:waitlist-sync"
                        )
                        if res.is_error:
                            r.errors += 1
                        else:
                            r.repaired += 1
                    except discord.HTTPException as e:
                        r.errors += 1
                        logger.warning("janitor (D): strip WAITLISTED failed for %s: %s", member, e)
            else:  # on_list and not has_wl
                r.samples.append(
                    f"<@{disc.disc_uuid}> `{mc.mc_username}` on waitlist DB but missing role "
                    f"→ add role" + ("" if enforce else " (would fix)")
                )
                if enforce:
                    try:
                        res = await apply_transition(
                            member, Trigger.ADDED_TO_WAITLIST, reason="janitor:waitlist-sync"
                        )
                        if res.is_error:
                            r.errors += 1
                            logger.warning(
                                "janitor (D): cannot add WAITLISTED for <@%s>: %s",
                                disc.disc_uuid, res.error,
                            )
                        else:
                            r.repaired += 1
                    except discord.HTTPException as e:
                        r.errors += 1
                        logger.warning("janitor (D): add WAITLISTED failed for %s: %s", member, e)
        except Exception:  # noqa: BLE001
            r.errors += 1
            logger.exception("janitor (D): unexpected error on a linked account")
    return r


async def reconcile_kept_linkcodes(bot: Bot, *, enforce: bool) -> ReconcilerResult:
    """(B) Clean leftover ``LinkCode`` rows.

    A row whose DiscordAccount has no primary link is *pending* (the consume
    never created the link) — left untouched. Unrecoverable rows are deleted
    (MC linked to a different DiscordAccount, or the Discord user is in no
    guild). For a row whose user IS linked: if the user already holds a valid
    primary state the link succeeded and the row is a stale leftover → delete
    it, **never touching roles** (preserves manual HIATUS/HONOURARY); only a
    *stateless* user is a real kept-on-failure → additively re-enforce the
    baseline (same Flame rule as (A)) then clear the code.
    """
    r = ReconcilerResult(name="linkcodes")
    rows = await LinkCode.all()
    for row in rows:
        try:
            r.scanned += 1
            disc = (
                await DiscordAccount.filter(disc_uuid=row.disc_uuid)
                .select_related("minecraft_account")
                .first()
            )
            if disc is None or disc.minecraft_account_id is None:
                continue  # pending row (link never created) — not (B)'s job

            # Unrecoverable-1: an MC by this username linked to a *different*
            # DiscordAccount (mirrors linking.py refuse-and-delete).
            mc_by_name = await MinecraftAccount.filter(
                mc_username__iexact=row.mc_username
            ).first()
            if mc_by_name is not None:
                other = await DiscordAccount.filter(
                    minecraft_account_id=mc_by_name.id
                ).first()
                if other is not None and other.disc_uuid != row.disc_uuid:
                    r.flagged += 1
                    r.samples.append(
                        f"`{row.mc_username}` LinkCode unrecoverable: MC linked elsewhere"
                        + ("" if enforce else " (would delete)")
                    )
                    if enforce:
                        try:
                            await row.delete()
                            r.repaired += 1
                        except Exception:  # noqa: BLE001
                            r.errors += 1
                            logger.exception("janitor (B): delete failed (unrecoverable-1)")
                    continue

            member, found = await _resolve_member(bot, row.disc_uuid)
            if not found:
                # Unrecoverable-2: user in no guild at all (cache miss already
                # resolved via REST inside _resolve_member).
                r.flagged += 1
                r.samples.append(
                    f"<@{row.disc_uuid}> `{row.mc_username}` LinkCode unrecoverable: user in no guild"
                    + ("" if enforce else " (would delete)")
                )
                if enforce:
                    try:
                        await row.delete()
                        r.repaired += 1
                    except Exception:  # noqa: BLE001
                        r.errors += 1
                        logger.exception("janitor (B): delete failed (unrecoverable-2)")
                continue

            # The Discord user IS linked. Two very different cases:
            r.flagged += 1
            state = state_of(member)
            if any(s in state for s in _PRIMARY):
                # Link plainly succeeded (user has a valid primary state,
                # possibly a legitimate manual HIATUS/HONOURARY). This row is
                # just a stale leftover (linked via /link set, a re-issued
                # code, etc.). Safe cleanup ONLY — never touch roles.
                if not enforce:
                    r.samples.append(
                        f"<@{row.disc_uuid}> `{row.mc_username}` stale code "
                        f"(already linked, state={_fmt_state(state)}) — would clear"
                    )
                    continue
                try:
                    await row.delete()
                    r.repaired += 1
                    r.samples.append(
                        f"<@{row.disc_uuid}> `{row.mc_username}` stale code "
                        f"(already linked, state={_fmt_state(state)}) — cleared"
                    )
                except Exception:  # noqa: BLE001 — already-absent row is fine
                    logger.debug("janitor (B): LinkCode row already gone for %s", row.disc_uuid)
                continue

            # Stateless = a real kept-on-enforcement-failure (Flame). The
            # baseline grant is additive for a stateless user (nothing to
            # strip), so this never demotes anyone.
            if not enforce:
                r.samples.append(
                    f"<@{row.disc_uuid}> `{row.mc_username}` no membership state "
                    f"— would re-enforce baseline & clear code"
                )
                continue
            try:
                ok = await _enforce_linked_baseline_for(
                    bot, row.disc_uuid, disc.minecraft_account
                )
            except Exception:  # noqa: BLE001
                ok = False
                logger.exception("janitor (B): _enforce_linked_baseline_for crashed for %s", row.disc_uuid)
            if ok:
                try:
                    await row.delete()
                except Exception:  # noqa: BLE001 — already-absent row is fine
                    logger.debug("janitor (B): LinkCode row already gone for %s", row.disc_uuid)
                r.repaired += 1
                r.samples.append(
                    f"<@{row.disc_uuid}> `{row.mc_username}` no state — re-enforced & code cleared"
                )
            else:
                r.errors += 1
                r.samples.append(
                    f"<@{row.disc_uuid}> `{row.mc_username}` no state — enforce still failing (code kept)"
                )
        except Exception:  # noqa: BLE001
            r.errors += 1
            logger.exception("janitor (B): unexpected error on a LinkCode row")
    return r


# Rotating cursor into the "HIATUS holders not on the Returners roster" list
# for reconciler (H). Module-level because reconcilers are plain functions;
# single-process bot, so a plain int is enough. Resetting to 0 on restart just
# re-checks from the top of the list, which is harmless.
_HIATUS_SWEEP_CURSOR = 0


async def reconcile_hiatus_guild(bot: Bot, *, enforce: bool) -> ReconcilerResult:
    """(H) A HIATUS-role holder must be guildless — *in a guild → never
    Hiatus*. Fires the transition they're actually owed when they aren't.

    Closes the hole documented in ``../../.claude/role_state.md``: nothing in
    the periodic path scans a guild that no Returners member is in, so a
    guildless ex-member who joins some third guild is never observed and keeps
    HIATUS indefinitely. ``lib/mc/hiatus_alerts.py`` catches this at alert
    time, but only on a day the player happens to log in; this sweep closes it
    for everyone else.

    **Rate budget** (dazebot shares one WAPI token, so this is the whole
    design constraint):

    * One GUILD-bucket request per tick — the Returners roster, fetched
      **once** for the entire cohort. Deliberately not ``refresh_mc_guild``
      per player: that helper re-fetches the roster on every call, which would
      turn an N-player sweep into 2N requests instead of 1+N.
    * At most ``JANITOR_HIATUS_SWEEP_MAX`` PLAYER-bucket requests per tick,
      issued at **background** priority so they only drain when no normal- or
      priority-tier call is pending. The cohort is walked in a rotating slice,
      so a cohort of C is fully covered every ``ceil(C / max)`` ticks — at the
      6h default that is a couple of days for a ~135-account cohort, which is
      ample for a role correction.

    Anyone on the roster is resolved for free (set intersection, no extra
    request), which also covers the "stale HIATUS while actually back in
    Returners" case — 6 of the 16 bad alerts measured on 2026-07-27.
    """
    global _HIATUS_SWEEP_CURSOR

    r = ReconcilerResult(name="hiatus_guild")
    hiatus_uuids = await hiatus_member_uuids(bot)  # Discord cache only — free
    if not hiatus_uuids:
        return r
    r.scanned = len(hiatus_uuids)

    try:
        roster = await get_guild("Returners")
    except Exception:  # noqa: BLE001 — third-party API; skip the tick entirely
        r.errors += 1
        logger.warning("janitor (H): Returners roster fetch failed; skipping sweep")
        return r
    returners = {m.uuid for m in roster.members.all_members()}

    # --- free pass: on the roster, so provably not on hiatus ---
    back_in = sorted(hiatus_uuids & returners)
    if back_in:
        r.flagged += len(back_in)
        r.samples.append(
            f"{len(back_in)} HIATUS holder(s) are on the Returners roster → MEMBER"
            + ("" if enforce else " (would fix)")
        )
        if enforce:
            try:
                await fire_trigger_for_mc_uuids(
                    bot, back_in, Trigger.JOINED_VETS, reason="janitor:hiatus-in-returners"
                )
                r.repaired += len(back_in)
            except Exception:  # noqa: BLE001
                r.errors += 1
                logger.exception("janitor (H): JOINED_VETS fanout failed")

    # --- metered pass: everyone else needs one /player call to learn their guild ---
    rest = sorted(hiatus_uuids - returners)
    if not rest:
        _HIATUS_SWEEP_CURSOR = 0
        return r
    cap = max(1, int(getattr(CurrConfig, "JANITOR_HIATUS_SWEEP_MAX", 15)))
    start = _HIATUS_SWEEP_CURSOR % len(rest)
    batch = (rest + rest)[start:start + min(cap, len(rest))]
    _HIATUS_SWEEP_CURSOR = (start + len(batch)) % len(rest)
    logger.info(
        "janitor (H): %d HIATUS holder(s), %d on roster, polling %d/%d others "
        "(cursor %d, background tier)",
        len(hiatus_uuids), len(back_in), len(batch), len(rest), start,
    )

    for uuid in batch:
        try:
            player = await get_player_stats(uuid, background=True)
        except Exception:  # noqa: BLE001 — one bad lookup must not abort the sweep
            r.errors += 1
            logger.warning("janitor (H): player lookup failed for %s", uuid)
            continue
        live = player.guild.name if player.guild else None
        # WYNN-STALE-WORKAROUND (see lib/mc/resolve.refresh_mc_guild): the
        # player endpoint runs ~12h stale, so a "Returners" here contradicts
        # the roster we just fetched — and the roster is authoritative.
        if live == "Returners":
            live = None
        if live is None:
            continue  # genuinely guildless — HIATUS is correct
        r.flagged += 1
        account = await MinecraftAccount.get_or_none(uuid=uuid)
        r.samples.append(
            f"`{account.mc_username if account else uuid}` is in `{live}` "
            "→ REGISTERED" + ("" if enforce else " (would fix)")
        )
        if not enforce:
            continue
        try:
            if account is not None and account.guild != live:
                account.guild = live
                await account.save(update_fields=["guild"])
            await fire_trigger_for_mc_uuids(
                bot, [uuid], Trigger.JOINED_OTHER_GUILD, reason="janitor:hiatus-in-guild"
            )
            r.repaired += 1
        except Exception:  # noqa: BLE001
            r.errors += 1
            logger.exception("janitor (H): repair failed for %s", uuid)

    return r


RECONCILERS = (
    reconcile_blocklisted_registered_only,  # (F) — first: strongest invariant
    reconcile_linked_baseline,              # (A) — the Flame self-heal
    reconcile_waitlist_role,                # (D) — needs A's baseline grants first
    reconcile_kept_linkcodes,               # (B) — sees the post-F/A/D world
    reconcile_hiatus_guild,                 # (H) — last: the only one that spends WAPI budget
)


class Janitor(commands.Cog):
    """Periodic data-integrity reconciliation + staff `/janitor run`."""

    bot: Bot

    def __init__(self, bot: Bot):
        self.bot = bot
        if getattr(CurrConfig, "JANITOR_ENABLED", True):
            self.janitor_loop.start()
            logger.info("Janitor cog initialized (loop started)")
        else:
            logger.info("Janitor cog initialized (JANITOR_ENABLED=False — loop not started)")

    def cog_unload(self):
        try:
            self.janitor_loop.cancel()
        except RuntimeError:
            pass

    @tasks.loop(minutes=Config.JANITOR_INTERVAL_MINUTES)
    async def janitor_loop(self):
        await self._tick(trigger="scheduled")

    @janitor_loop.before_loop
    async def _before_janitor_loop(self):
        # Member cache must be populated before sweeping every linked account.
        await self.bot.wait_until_ready()

    async def _tick(self, *, trigger: str) -> list[ReconcilerResult]:
        """Run all reconcilers once, then alert (throttled). Returns the
        per-reconciler results so callers (e.g. /janitor run) can summarize.
        """
        enforce = bool(getattr(CurrConfig, "JANITOR_REPAIR_ENABLED", False))
        results: list[ReconcilerResult] = []
        for fn in RECONCILERS:
            try:
                results.append(await fn(self.bot, enforce=enforce))
            except Exception:  # noqa: BLE001 — one reconciler must not skip the rest
                logger.exception("janitor: reconciler %s crashed", getattr(fn, "__name__", fn))
                results.append(ReconcilerResult(name=getattr(fn, "__name__", "?"), errors=1))

        active = [r for r in results if r.has_activity]
        mode = "REPAIR" if enforce else "LOG-ONLY"
        if not active:
            logger.info("janitor tick clean (%s, mode=%s) — nothing to report", trigger, mode)
            return results

        # Full summary always goes to the log.
        for r in active:
            logger.warning(
                "janitor [%s] %s: scanned=%d flagged=%d repaired=%d errors=%d",
                mode, _LABELS.get(r.name, r.name), r.scanned, r.flagged, r.repaired, r.errors,
            )

        repaired_any = enforce and any(r.repaired for r in active)
        now = datetime.now(timezone.utc)
        last = await JanitorAlert.all().order_by("-created_at").first()
        throttled = (
            last is not None
            and now - last.created_at < CurrConfig.JANITOR_ALERT_DELTA
        )
        # A tick that actually repaired something is a state change staff must
        # always see — bypass the throttle. The throttle only suppresses
        # "still detecting the same unfixable thing".
        if throttled and not repaired_any:
            logger.info("janitor: alert throttled (last %s ago) — logged only", now - last.created_at)
            return results

        await self._post_summary(active, enforce=enforce)
        await JanitorAlert.create()
        return results

    async def _post_summary(self, results: list[ReconcilerResult], *, enforce: bool):
        channel = self.bot.get_channel(CurrConfig.JANITOR_ALERT_CHANNEL)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(CurrConfig.JANITOR_ALERT_CHANNEL)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                logger.error("janitor: cannot fetch alert channel %s: %s", CurrConfig.JANITOR_ALERT_CHANNEL, e)
                return
        if not isinstance(channel, discord.abc.Messageable):
            logger.error("janitor: alert channel %s is not messageable", CurrConfig.JANITOR_ALERT_CHANNEL)
            return

        any_err = any(r.errors for r in results)
        any_repaired = enforce and any(r.repaired for r in results)
        if any_err:
            colour = discord.Colour.red()
        elif any_repaired:
            colour = discord.Colour.green()
        elif enforce:
            colour = discord.Colour.blue()
        else:
            colour = discord.Colour.orange()

        embed = discord.Embed(
            title=f"🧹 Janitor — {'REPAIR' if enforce else 'LOG-ONLY'} mode",
            colour=colour,
        )
        max_samples = CurrConfig.JANITOR_ALERT_MAX_SAMPLES
        for r in results:
            stats = f"scanned {r.scanned} · flagged {r.flagged} · repaired {r.repaired} · errors {r.errors}"
            shown = r.samples[:max_samples]
            extra = len(r.samples) - len(shown)
            body = stats
            if shown:
                body += "\n" + "\n".join(shown)
            if extra > 0:
                body += f"\n…and {extra} more"
            if len(body) > 1024:
                body = body[:1000] + "\n…(truncated)"
            embed.add_field(name=_LABELS.get(r.name, r.name), value=body, inline=False)

        if not enforce:
            embed.set_footer(
                text="LOG-ONLY — set JANITOR_REPAIR_ENABLED=true via /config to auto-fix "
                "(loop enable/interval changes need a restart)."
            )
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            logger.error("janitor: alert send failed: %s", e)
            # Defensive fallback: a plain truncated line so staff still get *something*.
            try:
                line = " | ".join(
                    f"{_LABELS.get(r.name, r.name)}: f{r.flagged}/r{r.repaired}/e{r.errors}"
                    for r in results
                )
                await channel.send(
                    f"🧹 Janitor ({'REPAIR' if enforce else 'LOG-ONLY'}) — {line}"[:1900],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.error("janitor: fallback alert send also failed")

    @commands.hybrid_group(name="janitor", description="Data-integrity janitor (staff).")
    @is_staff()
    async def janitor_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply("Use `/janitor run` to run one reconciliation pass now.")

    @janitor_group.command(
        name="run",
        description="(Staff) Run one janitor pass immediately (mode follows JANITOR_REPAIR_ENABLED).",
    )
    @is_staff()
    async def janitor_run(self, ctx: commands.Context):
        await ctx.defer()
        results = await self._tick(trigger=f"manual:{ctx.author}")
        enforce = bool(getattr(CurrConfig, "JANITOR_REPAIR_ENABLED", False))
        active = [r for r in results if r.has_activity]
        if not active:
            await ctx.reply(f"✅ Janitor pass complete ({'REPAIR' if enforce else 'LOG-ONLY'}) — nothing to report.")
            return
        lines = [
            f"`{_LABELS.get(r.name, r.name)}` — scanned {r.scanned}, flagged {r.flagged}, "
            f"repaired {r.repaired}, errors {r.errors}"
            for r in active
        ]
        await ctx.reply(
            f"✅ Janitor pass complete ({'REPAIR' if enforce else 'LOG-ONLY'}). "
            f"Summary posted to <#{CurrConfig.JANITOR_ALERT_CHANNEL}> (subject to throttle).\n"
            + "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: Bot):
    await bot.add_cog(Janitor(bot))
    logger.info("Janitor cog loaded successfully")

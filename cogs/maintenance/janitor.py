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
from lib.role_state import RoleState, force_to_registered_only, state_of
from orm import Blocklist, DiscordAccount, JanitorAlert, LinkCode, MinecraftAccount

logger = logging.getLogger("dazebot.cogs.maintenance.janitor")

# Primary (mutually-exclusive) membership-state flags, in the same set the
# role-state machine treats as primary. WAITLISTED is additive and excluded.
_PRIMARY = (RoleState.REGISTERED, RoleState.MEMBER, RoleState.HIATUS, RoleState.HONOURARY)

_LABELS = {
    "blocklist": "(F) Blocklist → REGISTERED",
    "baseline": "(A) No-state self-heal (Flame)",
    "linkcodes": "(B) Stale / kept LinkCodes",
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
    """Resolve a Discord member across the bot's guilds, get_member → REST
    fetch_member fallback (same pattern as ``_enforce_linked_baseline_for``).

    Returns ``(member_or_None, found_anywhere)``. ``found_anywhere`` is False
    only when the user is in *no* guild at all (a cache miss is resolved via
    the REST fetch first, so it does not count as "no guild").
    """
    try:
        uid = int(disc_uuid)
    except ValueError:
        return None, False
    for guild in bot.guilds:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                continue
            except discord.HTTPException as e:
                logger.warning("janitor: fetch_member(%s) in %s failed: %s", uid, guild.name, e)
                continue
        return member, True
    return None, False


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


RECONCILERS = (
    reconcile_blocklisted_registered_only,  # (F) — first: strongest invariant
    reconcile_linked_baseline,              # (A) — the Flame self-heal
    reconcile_kept_linkcodes,               # (B) — sees the post-F/A world
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

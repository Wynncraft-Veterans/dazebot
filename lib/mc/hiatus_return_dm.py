"""The "welcome back from your hiatus" DM, and the buttons under it.

Sibling sink to ``lib/mc/hiatus_alerts.py``. When a HIATUS-role holder is
spotted online, two independent things should happen: staff get the
``#activity`` post (that module), and the player themselves gets a DM
telling them how to get re-invited (this one). They share the guild gate
and the single live ``refresh_mc_guild`` crosscheck that gate needs, and
nothing else -- in particular the channel post's 24h per-UUID cooldown
must not gate the DM, or a snooze ("remind me next time I log in") could
never be honoured inside a day.

**Why the gates are shaped the way they are.** The DM is unsolicited mail
to a real person, sent by an automation that fires off a *polling*
signal. Every one of the gates below exists because some detection path
can fire more than once for what a human would call one event:

* ``login_edge`` -- the load-bearing one. ``server_watcher``'s stat-delta
  branch calls into the alert path on essentially *every* 2-minute tick
  of active play, and ``hiatus_watcher``'s newly-online set is derived
  from an in-memory diff that is empty after every restart. Neither is a
  login. A real login is a hole in ``MinecraftAccount.online_seen_at``
  longer than ``HIATUS_RETURN_DM_LOGOUT_GAP_MINUTES``, which the watchers
  compute and pass in. That is also exactly what the snooze button
  promises, so the button and the gate are the same mechanism.
* the person-level guild check -- step 2 ("check they are not currently
  in vets or another guild") applied to the *person*, not the spotted
  account. A HIATUS holder whose main was kicked while an alt stayed on
  the roster is still in the guild; DMing them "you dropped off the
  roster" would be both wrong and unstoppable, since the alt keeps
  re-stamping the rejoin evidence on every guild tick forever.
* the blocklist -- a blocked user is forced to REGISTERED-only and can
  never be Hiatus, so "welcome back, ask for a re-invite" is the last
  thing we should be telling them. Checked against the DB rather than
  inferred from their roles, because ``/block`` only enforces roles when
  the member is present in the Discord guild and swallows the HTTP error
  if that write fails -- a blocked user holding a stale HIATUS role is a
  reachable state.
* the fleet budget -- per-user gating cannot bound the aggregate rate.
* the CAS claim in :func:`send_return_dm` -- see its docstring.

**The gates run in two phases**, and which phase a check lands in is a
cost decision, not an aesthetic one:

* :func:`plan_return_dm` is pure reads -- no writes, no network. It runs
  *before* ``maybe_alert_hiatus`` decides whether to spend the Wynncraft
  request the shared guild crosscheck costs, so a "no" here stays free.
  Everything it can answer from the DB, it answers here.
* :func:`verify_return_dm` runs after that crosscheck and immediately
  before the send. It re-asks the questions whose stored answer might be
  a lie -- specifically, whether the person is in a guild on some account
  other than the one that was spotted. That is the one cohort whose
  ``guild`` column is *known* to rot (nothing scans a guild no Returners
  member is in), and the shared crosscheck only ever refreshes the
  spotted account, so nothing else in the pipeline can catch it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import uuid as uuid_mod

import discord

from config import CurrConfig
from orm import Blocklist, DiscordAccount, HiatusReturnNotice, MinecraftAccount, MinecraftAlt

logger = logging.getLogger("dazebot.lib.mc.hiatus_return_dm")

MUTE_BUTTON_CUSTOM_ID = "hiatus_return:mute"
SNOOZE_BUTTON_CUSTOM_ID = "hiatus_return:snooze"

# Rolling window of send timestamps backing HIATUS_RETURN_DM_MAX_PER_HOUR.
# In-memory on purpose: it is a burst damper, and the event it damps (a
# restart handing the whole online cohort over at once) is the same event
# that would clear a persisted copy, so persisting it would be a false
# comfort. The per-user floor in the DB is the part that must survive.
_SEND_TIMES: deque[datetime] = deque()


def _budget_remaining() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    while _SEND_TIMES and _SEND_TIMES[0] < cutoff:
        _SEND_TIMES.popleft()
    return int(CurrConfig.HIATUS_RETURN_DM_MAX_PER_HOUR) - len(_SEND_TIMES)


@dataclass(frozen=True, slots=True)
class ReturnDmPlan:
    """A resolved intent to DM one person, plus the witness values needed
    to claim that send atomically later.

    ``notice_id`` is None when the person has no ``HiatusReturnNotice``
    row yet. ``last_sent_at`` / ``snooze_armed`` are the values read at
    plan time and are re-asserted in the UPDATE that claims the send, so
    a concurrent claim loses instead of double-sending.
    """

    disc_uuid: str
    notice_id: uuid_mod.UUID | None
    last_sent_at: datetime | None
    snooze_armed: bool
    via_snooze: bool
    # Every MC account the person owns, resolved once at plan time so the
    # verify pass doesn't re-walk the link tables. Includes the spotted
    # account.
    owned_uuids: frozenset[str] = frozenset()


async def linked_mc_uuids_for_disc(disc_uuid: str) -> set[str]:
    """Every MC uuid belonging to one Discord user -- primary link plus
    ``MinecraftAlt`` rows.

    The inverse of ``lib.role_state.linked_disc_uuids_for_mc``, narrowed
    to a single user. It lives here rather than beside its mirror image
    because it answers a question about *accounts*, not about roles, and
    every ``role_state`` -> ``orm`` reference in that module is already a
    function-local import working around the same cycle.
    """
    out: set[str] = set()
    disc = (
        await DiscordAccount.filter(disc_uuid=disc_uuid)
        .select_related("minecraft_account")
        .first()
    )
    if disc is not None and disc.minecraft_account is not None:
        out.add(disc.minecraft_account.uuid)
    alts = await MinecraftAlt.filter(
        discord_account__disc_uuid=disc_uuid
    ).select_related("minecraft_account")
    out.update(a.minecraft_account.uuid for a in alts)
    return out


async def _owner_of(account: MinecraftAccount) -> DiscordAccount | None:
    """Resolve the Discord user who owns ``account``, primary link first,
    then the alts table. An alt counts -- whoever owns the spotted account
    is who we would be writing to."""
    disc = await DiscordAccount.filter(minecraft_account__uuid=account.uuid).first()
    if disc is not None:
        return disc
    alt = (
        await MinecraftAlt.filter(minecraft_account__uuid=account.uuid)
        .select_related("discord_account")
        .first()
    )
    return alt.discord_account if alt is not None else None


async def plan_return_dm(
    bot, account: MinecraftAccount, *, login_edge: bool
) -> ReturnDmPlan | None:
    """Decide whether ``account``'s owner is owed the DM. **Pure reads.**

    Returns None -- send nothing -- far more often than not. Callers rely
    on a None here to skip the live guild crosscheck entirely, so this
    must stay free of both writes and network calls.
    """
    if not CurrConfig.HIATUS_RETURN_DM_ENABLED:
        return None
    # A sighting that isn't a login is a continuation of a session we were
    # already watching. Cheapest gate, and the one that does the work.
    if not login_edge:
        return None
    if _budget_remaining() <= 0:
        logger.info(
            "hiatus-return DM for %s deferred: fleet budget (%s/h) exhausted",
            account.mc_username, CurrConfig.HIATUS_RETURN_DM_MAX_PER_HOUR,
        )
        return None

    guild = bot.get_guild(CurrConfig.GUILD)
    if guild is None:
        return None

    disc = await _owner_of(account)
    if disc is None:
        return None
    try:
        member = guild.get_member(int(disc.disc_uuid))
    except ValueError:
        return None
    # Deliberately cache-only, no REST fallback: this runs off a 30s poll,
    # `members` intent is on, and a member who isn't cached is a member who
    # left the server -- not someone to spend a REST call confirming.
    if member is None:
        return None
    # The copy says "welcome back from your hiatus". If the roles have
    # moved on since the watcher built its uuid set, it hasn't.
    if not any(r.id == CurrConfig.ROLE_HIATUS for r in member.roles):
        return None

    # Step 2, applied to the person rather than the spotted account. The
    # live crosscheck downstream only ever refreshes ONE account, so it
    # structurally cannot see an alt that is still on a roster.
    owned = await linked_mc_uuids_for_disc(disc.disc_uuid)
    if owned and await MinecraftAccount.filter(
        uuid__in=list(owned), guild__isnull=False
    ).exists():
        logger.info(
            "hiatus-return DM for %s suppressed: another linked account is in a guild",
            member,
        )
        return None

    # Blocklisted people are forced to REGISTERED-only and can never hold
    # HIATUS (membership_spec §2b), so this should be unreachable via the
    # role check above -- except that ``/block`` only enforces roles when
    # the member is in the Discord guild, and logs-and-continues if the
    # role write fails. Ask the table directly. Any linked account being
    # blocked blocks the person: the block is a statement about a human,
    # and staying quiet is the safe direction to err in.
    if owned and await Blocklist.filter(minecraft_account__uuid__in=list(owned)).exists():
        logger.info("hiatus-return DM for %s suppressed: on the blocklist", member)
        return None

    notice = await HiatusReturnNotice.filter(disc_uuid=disc.disc_uuid).first()
    if notice is None:
        # Never written to, so step 3 has no "last time" to be since and
        # passes trivially -- a first-ever spotting is the case the whole
        # feature exists for.
        return ReturnDmPlan(
            disc_uuid=disc.disc_uuid,
            notice_id=None,
            last_sent_at=None,
            snooze_armed=False,
            via_snooze=False,
            owned_uuids=frozenset(owned),
        )
    if notice.muted:
        return None

    now = datetime.now(timezone.utc)
    # Floor from the later of the two, so snoozing a four-day-old DM
    # doesn't make the follow-up instantly due.
    floor_from = max(
        [t for t in (notice.last_sent_at, notice.snoozed_at) if t is not None],
        default=None,
    )
    if floor_from is not None:
        gap = timedelta(hours=float(CurrConfig.HIATUS_RETURN_DM_MIN_GAP_HOURS))
        if now - floor_from < gap:
            return None

    if notice.snooze_armed:
        via_snooze = True
    elif notice.last_sent_at is None:
        via_snooze = False
    else:
        # Step 3 proper: they must have been back in Returners since we
        # last wrote to them -- on any of their accounts, since a rejoin
        # on a main is a rejoin for the person.
        if not owned:
            return None
        if not await MinecraftAccount.filter(
            uuid__in=list(owned), last_in_returners_at__gt=notice.last_sent_at
        ).exists():
            return None
        via_snooze = False

    return ReturnDmPlan(
        disc_uuid=disc.disc_uuid,
        notice_id=notice.id,
        last_sent_at=notice.last_sent_at,
        snooze_armed=notice.snooze_armed,
        via_snooze=via_snooze,
        owned_uuids=frozenset(owned),
    )


async def verify_return_dm(bot, account: MinecraftAccount, plan: ReturnDmPlan) -> bool:
    """Live re-check of the gates whose stored answer could be stale.
    Returns True if the DM should still go out.

    Runs after ``maybe_alert_hiatus``'s shared ``refresh_mc_guild``, which
    has already established that the *spotted* account is genuinely
    guildless. This closes the hole that leaves: the person's **other**
    linked accounts were only checked against their stored ``guild``
    column, and that column is precisely the one known to rot. Nothing in
    the periodic path scans a guild that no Returners member is in, so an
    alt that joined some third guild months ago still reads as guildless
    forever (``.claude/role_state.md`` -- *in a guild -> never Hiatus*).
    Without this pass we would tell someone "welcome back from your
    hiatus" while they are, on another account, in a guild.

    When we do find one, the person doesn't just lose the DM -- they get
    the role transition they were owed all along, via the same
    ``heal_stale_hiatus`` the spotted-account path uses. So the fix is
    permanent: next login they are REGISTERED, out of the watchers' scope,
    and nothing here reconsiders them.

    **Cost.** Zero for the common case (nobody has an alt), and one
    ``refresh_mc_guild`` per additional linked account otherwise. The
    ceiling is the fleet budget -- at most
    ``HIATUS_RETURN_DM_MAX_PER_HOUR`` people reach this per hour, since
    the caller only gets here for a plan that survived every cheap gate.
    """
    # local: import cycle -- hiatus_alerts imports this module.
    from lib.mc.hiatus_alerts import heal_stale_hiatus
    from lib.mc.resolve import refresh_mc_guild

    guild = bot.get_guild(CurrConfig.GUILD)
    member = guild.get_member(int(plan.disc_uuid)) if guild is not None else None
    if member is None:
        return False
    # Re-read rather than trusting the plan: the shared crosscheck above
    # can itself have healed them out of HIATUS between the two phases.
    if not any(r.id == CurrConfig.ROLE_HIATUS for r in member.roles):
        logger.info("hiatus-return DM for %s dropped: no longer holds HIATUS", member)
        return False
    if plan.owned_uuids and await Blocklist.filter(
        minecraft_account__uuid__in=list(plan.owned_uuids)
    ).exists():
        logger.info("hiatus-return DM for %s dropped: on the blocklist", member)
        return False

    others = [u for u in plan.owned_uuids if u != account.uuid]
    if not others:
        return True
    for alt in await MinecraftAccount.filter(uuid__in=others):
        await refresh_mc_guild(alt)  # best-effort; leaves the value alone on API failure
        if alt.guild is not None:
            logger.info(
                "hiatus-return DM for %s dropped: linked account %s is live in guild %r",
                member, alt.mc_username, alt.guild,
            )
            await heal_stale_hiatus(bot, alt)
            return False
    return True


def build_dm_body(username: str) -> str:
    """The message body.

    Deliberately does *not* assert a reason for the departure. HIATUS is
    granted by ``Trigger.BECAME_GUILDLESS``, which fires for anyone who
    leaves the Returners roster for any reason -- an inactivity kick, a
    voluntary leave, a transfer, a chief's decision -- and staff can also
    set it by hand. Naming a cause would be wrong for a good share of an
    unsolicited mailing.
    """
    return (
        "## Welcome back from your hiatus!\n"
        f"While you were gone, `{username}` dropped off the **VETS** roster — if that "
        "was the inactivity sweep, sorry about that! Either way, you're always "
        "welcome back.\n\n"
        "Simply message anyone on `/onlinemembers VETS` for a re-invite!\n"
        "-# Note that `/gu join VETS` is, for some reason, case sensitive."
    )


async def send_return_dm(bot, account: MinecraftAccount, plan: ReturnDmPlan) -> bool:
    """Claim the send, then send it. Returns True if the DM landed.

    **Claim first, send second, and never send without the claim.** The
    obvious ordering -- send, then record that we sent -- is wrong twice
    over. Two coroutines can be in flight for one person at once
    (``server_watcher`` gathers over a whole cohort, and one person's main
    and alt are both in it; the two watcher loops also overlap), so a
    read-then-write would let both through and DM them twice. And if the
    write fails *after* a successful send -- lock contention against a
    single-writer SQLite file, or a full disk, which this VPS has a
    runbook entry for -- then nothing stops the next 30-second tick from
    sending again, and again, indefinitely.

    So the stamp is a compare-and-swap re-asserting the values
    :func:`plan_return_dm` read. A losing claim means another coroutine is
    already handling this person; a *failing* claim means we cannot record
    the send and therefore must not make it. The cost of erring in that
    direction is one missed DM, which the next rejoin -- or the snooze
    button on the DM they did get -- recovers.
    """
    from lib.mc.linking import dm_or_log  # local: import cycle

    guild = bot.get_guild(CurrConfig.GUILD)
    member = guild.get_member(int(plan.disc_uuid)) if guild is not None else None
    if member is None:
        return False

    now = datetime.now(timezone.utc)
    if plan.notice_id is None:
        notice, _ = await HiatusReturnNotice.get_or_create(disc_uuid=plan.disc_uuid)
    else:
        notice = await HiatusReturnNotice.filter(id=plan.notice_id).first()
        if notice is None:  # cleared by /alerts return_dm_clear mid-flight
            return False

    # Re-assert the witness. ``.update()`` returns the affected row count,
    # so a zero is a lost race rather than an error. ``muted=False`` is in
    # the predicate too: they may have hit the mute button in the seconds
    # since the plan was built.
    claim = HiatusReturnNotice.filter(
        id=notice.id, muted=False, snooze_armed=plan.snooze_armed
    )
    if plan.last_sent_at is None:
        claim = claim.filter(last_sent_at__isnull=True)
    else:
        claim = claim.filter(last_sent_at=plan.last_sent_at)
    if not await claim.update(last_sent_at=now, snooze_armed=False, snoozed_at=None):
        logger.info(
            "hiatus-return DM for %s skipped: claim lost (concurrent send, or muted "
            "since the plan was built)", member,
        )
        return False

    _SEND_TIMES.append(now)
    ok = await dm_or_log(
        member,
        build_dm_body(account.mc_username),
        view=HiatusReturnView(),
        fallback_logger=logger,
    )
    logger.info(
        "hiatus-return DM %s for %s (%s)%s",
        "sent" if ok else "not delivered (DMs closed?)",
        member, account.mc_username,
        " [via snooze]" if plan.via_snooze else "",
    )
    return ok


def guild_general_url() -> str:
    return f"https://discord.com/channels/{CurrConfig.GUILD}/{CurrConfig.GUILD_GENERAL_CHANNEL}"


class HiatusReturnView(discord.ui.View):
    """The three controls under the DM.

    Persistent (``timeout=None`` plus fixed ``custom_id`` s) and
    registered in ``bot.setup_hook``. Note which registration does what:
    every ``send`` stores *its own* instance against the new message's id
    and dispatch resolves that first, so the ``setup_hook`` copy is the
    after-a-restart fallback, not the live path.

    The link button carries no ``custom_id`` and doesn't need one --
    ``discord.ui.Button`` special-cases link style in ``is_persistent``
    (``return self.url is not None``), so mixing it into a persistent view
    passes ``add_view``'s check, and ``ViewStore`` simply skips it when
    registering dispatch targets.

    Every callback identifies its subject as ``interaction.user``: this
    view only ever exists in a 1:1 DM, so the clicker is by construction
    the person the message is about. Nothing is baked into a custom_id,
    which is what lets one registered instance serve every recipient.
    """

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Say hi in #guild-general",
                style=discord.ButtonStyle.link,
                url=guild_general_url(),
                row=0,
            )
        )

    @discord.ui.button(
        label="Don't message me again",
        style=discord.ButtonStyle.secondary,
        custom_id=MUTE_BUTTON_CUSTOM_ID,
        row=0,
    )
    async def mute(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ephemeral is a flag on the response, not a channel-type
        # behaviour, and Discord honours it in DMs -- so the same call
        # works here as it would in a guild.
        await interaction.response.defer(ephemeral=True, thinking=True)
        notice, _ = await HiatusReturnNotice.get_or_create(disc_uuid=str(interaction.user.id))
        notice.muted = True
        notice.snooze_armed = False
        notice.snoozed_at = None
        await notice.save(update_fields=["muted", "snooze_armed", "snoozed_at"])
        logger.info("hiatus-return DM muted by %s (%s)", interaction.user, interaction.user.id)
        await interaction.followup.send(
            "🔕 Got it — I won't send you this again. Ask a staff member to run "
            "`/alerts return_dm_clear` if you change your mind.",
            ephemeral=True,
        )
        # Retire the buttons on the message itself, so a later reader of
        # the DM doesn't wonder whether the choice took.
        if interaction.message is not None:
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException as e:
                logger.warning("hiatus-return mute: could not strip the view: %s", e)

    @discord.ui.button(
        label="Remind me next time I log in",
        style=discord.ButtonStyle.primary,
        custom_id=SNOOZE_BUTTON_CUSTOM_ID,
        row=0,
    )
    async def snooze(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        notice, _ = await HiatusReturnNotice.get_or_create(disc_uuid=str(interaction.user.id))
        if notice.muted:
            await interaction.followup.send(
                "You've muted this notification, so there's nothing to snooze. "
                "Ask a staff member to run `/alerts return_dm_clear` to undo that.",
                ephemeral=True,
            )
            return
        notice.snooze_armed = True
        notice.snoozed_at = datetime.now(timezone.utc)
        await notice.save(update_fields=["snooze_armed", "snoozed_at"])
        logger.info("hiatus-return DM snoozed by %s (%s)", interaction.user, interaction.user.id)
        await interaction.followup.send(
            "⏰ Snoozed. I'll send this once more the next time you log in — you "
            "won't hear from me again during this session.",
            ephemeral=True,
        )

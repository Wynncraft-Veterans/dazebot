"""Tests for the hiatus-return DM's gates and its send claim.

Two things are under test, and the second matters as much as the first:

* **Who gets the message.** This is unsolicited mail to a real person, so
  every gate that decides "not this one" is load-bearing: the login edge,
  the person-level guild check, mute, the rejoin requirement, the snooze
  bypass, and the min-gap floor.
* **That it goes out at most once per event.** Two watcher loops overlap,
  ``server_watcher`` gathers over a cohort that can contain one person's
  main *and* their alt, and the bookkeeping write can fail. The claim in
  ``send_return_dm`` is a compare-and-swap for exactly that reason, and a
  lost or failed claim must suppress the send rather than repeat it.

Every seam is patched; nothing here touches the network, the ORM, or
Discord.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid as uuid_mod

import pytest

import lib.mc.hiatus_alerts as ha
import lib.mc.hiatus_return_dm as hrd
import lib.mc.linking as linking
import lib.mc.resolve as resolve
from orm import UNKNOWN_LAST_ONLINE

NOW = datetime.now(timezone.utc)
HIATUS_ROLE = 4242
DISC_UUID = "1741343346288230410"
MAIN = "4580e15d-4914-412a-88ce-c26ef0887da4"
ALT = "9f1c0d22-0000-4000-8000-00000000a17e"


def _qs(*, exists=False, update=0):
    """A stand-in queryset: chainable, with awaitable terminals."""
    q = MagicMock()
    q.filter.return_value = q
    q.select_related.return_value = q
    q.exists = AsyncMock(return_value=exists)
    q.update = AsyncMock(return_value=update)
    return q


class _Rows:
    """A queryset that is itself awaitable, for ``await Model.filter(...)``
    (as opposed to the ``.exists()`` / ``.update()`` terminals above)."""

    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, **kw):
        return self

    def select_related(self, *a, **k):
        return self

    def __await__(self):
        async def _inner():
            return list(self._rows)

        return _inner().__await__()


def _mc_row(uuid, *, guild=None, username="alt"):
    return SimpleNamespace(uuid=uuid, guild=guild, mc_username=username, save=AsyncMock())


def _plan(**over):
    base = dict(
        disc_uuid=DISC_UUID,
        notice_id=None,
        last_sent_at=None,
        snooze_armed=False,
        via_snooze=False,
        owned_uuids=frozenset({MAIN}),
        reserved_at=NOW,
    )
    base.update(over)
    return hrd.ReturnDmPlan(**base)


def _notice(**over):
    row = SimpleNamespace(
        id=uuid_mod.uuid4(),
        disc_uuid=DISC_UUID,
        last_sent_at=None,
        muted=False,
        snooze_armed=False,
        snoozed_at=None,
        save=AsyncMock(),
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


@pytest.fixture(autouse=True)
def _clear_budget():
    hrd._SEND_TIMES.clear()
    yield
    hrd._SEND_TIMES.clear()


@pytest.fixture
def account():
    # Away for two months by default -- the ordinary case, comfortably past
    # HIATUS_RETURN_DM_MIN_AWAY_DAYS. Tests that care about the away gate
    # move it.
    return SimpleNamespace(
        uuid=MAIN,
        mc_username="Arxhe_",
        guild=None,
        last_online=NOW - timedelta(days=60),
    )


@pytest.fixture
def bot():
    member = MagicMock()
    member.roles = [MagicMock(id=HIATUS_ROLE)]
    member.__str__ = lambda self: "arxhe"
    guild = MagicMock()
    guild.get_member.return_value = member
    b = MagicMock()
    b.get_guild.return_value = guild
    b.member = member
    b.guild = guild
    return b


@pytest.fixture
def env(monkeypatch):
    """Patch every seam ``plan_return_dm`` / ``send_return_dm`` touch and
    hand back the knobs plus the spies."""
    cfg = hrd.CurrConfig
    monkeypatch.setattr(cfg, "HIATUS_RETURN_DM_ENABLED", True)
    monkeypatch.setattr(cfg, "HIATUS_RETURN_DM_MAX_PER_HOUR", 8)
    monkeypatch.setattr(cfg, "HIATUS_RETURN_DM_MIN_GAP_HOURS", 6.0)
    monkeypatch.setattr(cfg, "HIATUS_RETURN_DM_MIN_AWAY_DAYS", 9.0)
    monkeypatch.setattr(cfg, "ROLE_HIATUS", HIATUS_ROLE)

    state = SimpleNamespace(
        notice=None,          # what HiatusReturnNotice.filter().first() yields
        in_a_guild=False,     # does the person hold ANY account in a guild (stored)
        rejoined=False,       # last_in_returners_at > last_sent_at on any account
        blocked=False,        # any linked account on the blocklist
        claimed=1,            # rows affected by the CAS update
        owned={MAIN},         # every MC account the person owns
        alt_rows=[],          # rows returned for `await MinecraftAccount.filter(uuid__in=...)`
        live_guilds={},       # what live_guilds_for reports; None = upstream unavailable
    )

    def _mc_filter(**kw):
        if "guild__isnull" in kw:
            return _qs(exists=state.in_a_guild)
        if "last_in_returners_at__gt" in kw:
            return _qs(exists=state.rejoined)
        # verify_return_dm's bare `uuid__in` lookup, awaited directly.
        return _Rows(state.alt_rows)

    claim = _qs()
    claim.update = AsyncMock(side_effect=lambda **kw: state.claimed)

    notice_qs = MagicMock()
    notice_qs.filter.return_value = notice_qs
    notice_qs.first = AsyncMock(side_effect=lambda: state.notice)

    def _notice_filter(**kw):
        # The CAS predicate is the only one keyed on ``muted``.
        return claim if "muted" in kw else notice_qs

    monkeypatch.setattr(hrd.MinecraftAccount, "filter", MagicMock(side_effect=_mc_filter))
    monkeypatch.setattr(hrd.HiatusReturnNotice, "filter", MagicMock(side_effect=_notice_filter))
    monkeypatch.setattr(
        hrd.HiatusReturnNotice,
        "get_or_create",
        AsyncMock(side_effect=lambda **kw: (state.notice or _notice(), state.notice is None)),
    )
    monkeypatch.setattr(
        hrd, "_owner_of", AsyncMock(return_value=SimpleNamespace(disc_uuid=DISC_UUID))
    )
    monkeypatch.setattr(
        hrd, "linked_mc_uuids_for_disc", AsyncMock(side_effect=lambda d: set(state.owned))
    )
    monkeypatch.setattr(
        hrd.Blocklist, "filter", MagicMock(side_effect=lambda **kw: _qs(exists=state.blocked))
    )
    monkeypatch.setattr(hrd, "HiatusReturnView", MagicMock())
    dm = AsyncMock(return_value=True)
    monkeypatch.setattr(linking, "dm_or_log", dm)

    # verify_return_dm reaches these through function-local imports, so
    # they have to be patched on their defining modules.
    live = AsyncMock(side_effect=lambda uuids, **kw: state.live_guilds)
    heal = AsyncMock()
    monkeypatch.setattr(resolve, "live_guilds_for", live)
    monkeypatch.setattr(ha, "heal_stale_hiatus", heal)

    state.dm = dm
    state.claim = claim
    state.live = live
    state.heal = heal
    return state


# ---------------------------------------------------------------------------
# who gets the message
# ---------------------------------------------------------------------------


async def test_first_ever_spotting_sends(bot, env, account):
    plan = await hrd.plan_return_dm(bot, account, login_edge=True)
    assert plan is not None
    # No row yet: step 3 has no "last time" to be since, so it passes.
    assert plan.notice_id is None and plan.via_snooze is False


async def test_kill_switch_suppresses(bot, env, account, monkeypatch):
    monkeypatch.setattr(hrd.CurrConfig, "HIATUS_RETURN_DM_ENABLED", False)
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_non_login_sighting_suppresses(bot, env, account):
    """The single most important gate: server_watcher's stat-delta branch
    re-enters on every tick of active play, and hiatus_watcher's diff is
    empty-initialised after a restart. Neither is a login."""
    assert await hrd.plan_return_dm(bot, account, login_edge=False) is None


async def test_no_hiatus_role_suppresses(bot, env, account):
    bot.member.roles = [MagicMock(id=9999)]
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_member_not_in_discord_suppresses(bot, env, account):
    bot.guild.get_member.return_value = None
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_another_linked_account_in_a_guild_suppresses(bot, env, account):
    """Step 2, applied to the person. Their main was kicked but an alt is
    still on a roster: they are not on hiatus, and the live crosscheck
    downstream only ever refreshes the one spotted account so it cannot
    see this."""
    env.in_a_guild = True
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_a_short_absence_is_not_a_hiatus(bot, env, account):
    """The copy asserts they dropped off the roster and suggests a prune.
    Saying that to someone who was gone two days is just wrong."""
    account.last_online = NOW - timedelta(days=2)
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_the_away_window_matches_the_purge_window(bot, env, account):
    """9 days is `/purgelist`'s default -- the criterion staff actually kick
    on. A DM about being pruned should use the threshold the pruning
    decision used."""
    account.last_online = NOW - timedelta(days=8, hours=23)
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None
    account.last_online = NOW - timedelta(days=9, hours=1)
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is not None


async def test_a_short_absence_costs_no_fleet_slot(bot, env, account):
    """The gate reads a column already in hand, so it sits above the
    reservation."""
    account.last_online = NOW - timedelta(days=2)
    await hrd.plan_return_dm(bot, account, login_edge=True)
    assert len(hrd._SEND_TIMES) == 0


async def test_an_unknowable_absence_suppresses(bot, env, account):
    """Privacy-hidden players with no real lastJoin on record sit at the
    epoch sentinel. We cannot tell how long they were away, so we don't
    claim they were away -- accepting that this silences them for good."""
    account.last_online = UNKNOWN_LAST_ONLINE
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_a_caller_that_clobbers_last_online_passes_the_prior_value(bot, env, account):
    """server_watcher bumps last_online to now on its way in, so the stored
    value would read as a zero-length absence. It hands over the pre-tick
    reading instead."""
    account.last_online = NOW  # already clobbered
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None
    assert await hrd.plan_return_dm(
        bot, account, login_edge=True, away_since=NOW - timedelta(days=30)
    ) is not None


async def test_an_explicit_sentinel_is_still_unknowable(bot, env, account):
    account.last_online = NOW - timedelta(days=60)  # stored value looks fine...
    # ...but the caller knows the pre-tick value was the sentinel.
    assert await hrd.plan_return_dm(
        bot, account, login_edge=True, away_since=UNKNOWN_LAST_ONLINE
    ) is None


async def test_muted_suppresses(bot, env, account):
    env.notice = _notice(muted=True, last_sent_at=NOW - timedelta(days=30))
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_budget_exhausted_suppresses(bot, env, account):
    hrd._SEND_TIMES.extend([NOW] * 8)
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_budget_window_rolls_off(bot, env, account):
    hrd._SEND_TIMES.extend([NOW - timedelta(hours=2)] * 8)
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is not None


# ---------------------------------------------------------------------------
# step 3: they must have been back in vets since we last wrote to them
# ---------------------------------------------------------------------------


async def test_repeat_without_a_rejoin_is_blocked(bot, env, account):
    env.notice = _notice(last_sent_at=NOW - timedelta(days=30))
    env.rejoined = False
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_repeat_after_a_rejoin_sends(bot, env, account):
    env.notice = _notice(last_sent_at=NOW - timedelta(days=30))
    env.rejoined = True
    plan = await hrd.plan_return_dm(bot, account, login_edge=True)
    assert plan is not None and plan.via_snooze is False


# ---------------------------------------------------------------------------
# snooze
# ---------------------------------------------------------------------------


async def test_snooze_bypasses_the_rejoin_gate(bot, env, account):
    env.notice = _notice(
        last_sent_at=NOW - timedelta(days=30),
        snooze_armed=True,
        snoozed_at=NOW - timedelta(days=29),
    )
    env.rejoined = False
    plan = await hrd.plan_return_dm(bot, account, login_edge=True)
    assert plan is not None and plan.via_snooze is True


async def test_snooze_still_needs_a_login(bot, env, account):
    """The button says "next time I log in". Being still-online from the
    session they snoozed in is not that."""
    env.notice = _notice(last_sent_at=NOW - timedelta(days=30), snooze_armed=True)
    assert await hrd.plan_return_dm(bot, account, login_edge=False) is None


async def test_min_gap_floors_the_snooze_from_the_click_not_the_send(bot, env, account):
    """Snoozing a four-day-old DM must not make the follow-up instantly
    due — the floor runs from whichever of the two is later."""
    env.notice = _notice(
        last_sent_at=NOW - timedelta(days=4),
        snooze_armed=True,
        snoozed_at=NOW - timedelta(minutes=2),
    )
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_min_gap_floors_a_rejoin_send(bot, env, account):
    env.notice = _notice(last_sent_at=NOW - timedelta(hours=1))
    env.rejoined = True
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


# ---------------------------------------------------------------------------
# the send claim
# ---------------------------------------------------------------------------


async def test_send_stamps_before_dispatching(bot, env, account):
    env.notice = _notice(last_sent_at=NOW - timedelta(days=30))
    plan = _plan(notice_id=env.notice.id, last_sent_at=env.notice.last_sent_at, snooze_armed=False, via_snooze=False)
    assert await hrd.send_return_dm(bot, account, plan) is True
    env.claim.update.assert_awaited_once()
    kw = env.claim.update.await_args.kwargs
    assert kw["snooze_armed"] is False and kw["snoozed_at"] is None
    env.dm.assert_awaited_once()


async def test_lost_claim_does_not_send(bot, env, account):
    """Two coroutines in flight for one person — main and alt in the same
    ``asyncio.gather``. The loser must not DM them a second time."""
    env.notice = _notice()
    env.claimed = 0
    plan = _plan(notice_id=env.notice.id, last_sent_at=None, snooze_armed=False, via_snooze=False)
    assert await hrd.send_return_dm(bot, account, plan) is False
    env.dm.assert_not_awaited()


async def test_a_failed_dm_stays_stamped(bot, env, account):
    """Closed DMs must not re-arm: the claim already landed, so the next
    30-second tick finds the floor in place rather than trying again."""
    env.notice = _notice()
    env.dm.return_value = False
    plan = _plan(notice_id=env.notice.id, last_sent_at=None, snooze_armed=False, via_snooze=False)
    assert await hrd.send_return_dm(bot, account, plan) is False
    env.claim.update.assert_awaited_once()


async def test_the_plan_reserves_the_slot_not_the_send(bot, env, account):
    """The reservation has to be taken before the verify pass, or the cap
    bounds delivered DMs while the expensive path — verifying someone who
    is then rejected — runs unmetered."""
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is not None
    assert len(hrd._SEND_TIMES) == 1


async def test_a_cheap_rejection_hands_the_slot_back(bot, env, account):
    """Nothing was spent, so nothing should be counted."""
    bot.member.roles = [MagicMock(id=9999)]
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None
    assert len(hrd._SEND_TIMES) == 0


async def test_the_reservation_is_taken_before_any_await(bot, env, account):
    """server_watcher gathers its whole cohort, so a batch resumes in one
    event-loop turn: a read-then-later-write budget would let every
    coroutine see the same last free slot."""
    import asyncio

    hrd._SEND_TIMES.extend([NOW] * 7)  # one slot left
    a, b = await asyncio.gather(
        hrd.plan_return_dm(bot, account, login_edge=True),
        hrd.plan_return_dm(bot, account, login_edge=True),
    )
    assert [a, b].count(None) == 1
    assert len(hrd._SEND_TIMES) == 8


async def test_send_is_a_noop_when_the_member_left(bot, env, account):
    bot.guild.get_member.return_value = None
    plan = _plan(notice_id=None, last_sent_at=None, snooze_armed=False, via_snooze=False)
    assert await hrd.send_return_dm(bot, account, plan) is False
    env.dm.assert_not_awaited()


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


def test_body_interpolates_the_username():
    """Guards a real footgun: the username sits on one line of a multi-line
    implicit concatenation, so an f-prefix on the wrong line ships
    "`{username}`" to every recipient verbatim."""
    body = hrd.build_dm_body("Arxhe_")
    assert "Arxhe_" in body
    assert "{username}" not in body


def test_body_carries_the_re_invite_instructions():
    """The part of the message that actually does something for the reader."""
    body = hrd.build_dm_body("Arxhe_")
    assert "/onlinemembers VETS" in body
    assert "case sensitive" in body


def test_body_hedges_the_cause():
    """HIATUS is granted by BECAME_GUILDLESS, which fires for a voluntary
    leave or a transfer as readily as for an inactivity prune. Naming the
    prune is fair -- it is much the most common cause -- but it has to stay
    hedged rather than asserted."""
    body = hrd.build_dm_body("Arxhe_").lower()
    assert "seems" in body


# ---------------------------------------------------------------------------
# blocklist
# ---------------------------------------------------------------------------


async def test_blocked_user_is_never_messaged(bot, env, account):
    """A blocklisted person is forced to REGISTERED-only and can never be
    Hiatus, so "ask anyone for a re-invite" is the last thing to tell
    them."""
    env.blocked = True
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_the_block_query_asks_about_every_linked_account(bot, env, account):
    """``/block`` only enforces roles when the member is in the Discord
    guild, and logs-and-continues if the role write fails — so the role
    check alone would let a blocked user through, and the table has to be
    asked about the whole person rather than just the spotted account."""
    env.owned = {MAIN, ALT}
    await hrd.plan_return_dm(bot, account, login_edge=True)
    kwargs = hrd.Blocklist.filter.call_args.kwargs
    assert set(kwargs["minecraft_account__uuid__in"]) == {MAIN, ALT}


async def test_a_block_landing_between_the_two_phases_still_stops_it(bot, env, account):
    plan = _plan(notice_id=None, last_sent_at=None, snooze_armed=False, via_snooze=False, owned_uuids=frozenset({MAIN}),
    )
    env.blocked = True
    assert await hrd.verify_return_dm(bot, account, plan) is False


# ---------------------------------------------------------------------------
# the live verify pass: is the HIATUS role actually correct?
# ---------------------------------------------------------------------------


async def test_verify_makes_no_requests_when_there_are_no_other_accounts(bot, env, account):
    """The common case. The spotted account was already refreshed by the
    caller, so there is nothing left to ask about."""
    assert await hrd.verify_return_dm(bot, account, _plan()) is True
    env.live.assert_not_awaited()


async def test_an_alt_live_in_another_guild_stops_the_dm_and_heals(bot, env, account):
    """The gap the stored-column check cannot close: nothing scans a guild
    no Returners member is in, so the alt's `guild` reads NULL forever."""
    env.live_guilds = {ALT: "KongoBoys"}
    env.alt_rows = [_mc_row(ALT)]
    plan = _plan(owned_uuids=frozenset({MAIN, ALT}))

    assert await hrd.verify_return_dm(bot, account, plan) is False
    # They don't just lose the DM — they get the transition they were owed,
    # which drops them out of the watchers' scope for good.
    env.heal.assert_awaited_once()
    healed = env.heal.await_args.args[1]
    assert healed.uuid == ALT
    # ...and we persist what we learned, so the next detection dies at the
    # cheap stored-column gate instead of paying for this again.
    assert healed.guild == "KongoBoys"
    healed.save.assert_awaited_once()


async def test_the_spotted_account_is_never_re_checked(bot, env, account):
    """maybe_alert_hiatus already refreshed it; asking again would be a
    wasted roster fetch."""
    env.live_guilds = {ALT: None}
    env.alt_rows = [_mc_row(ALT)]
    await hrd.verify_return_dm(bot, account, _plan(owned_uuids=frozenset({MAIN, ALT})))
    assert list(env.live.await_args.args[0]) == [ALT]


async def test_alt_lookups_are_one_batch_at_background_priority(bot, env, account):
    """The repo's standing contract for this shape (see the janitor's
    hiatus sweep): one roster call for the whole set rather than one per
    account, and player calls that cannot preempt interactive lookups."""
    alts = {ALT, "c0000000-0000-4000-8000-00000000000c"}
    env.live_guilds = dict.fromkeys(alts)
    env.alt_rows = [_mc_row(u) for u in alts]
    await hrd.verify_return_dm(
        bot, account, _plan(owned_uuids=frozenset({MAIN}) | frozenset(alts))
    )
    env.live.assert_awaited_once()
    assert set(env.live.await_args.args[0]) == alts
    assert env.live.await_args.kwargs["background"] is True


async def test_an_unreachable_api_stops_the_dm(bot, env, account):
    """Fails CLOSED. Every account reaching here is stored as guildless by
    construction, so "API down" and "confirmed guildless" are the same
    observation — treating them alike would switch the gate off exactly
    when it is least able to check."""
    env.live_guilds = None
    env.alt_rows = [_mc_row(ALT)]
    plan = _plan(owned_uuids=frozenset({MAIN, ALT}))
    assert await hrd.verify_return_dm(bot, account, plan) is False
    env.heal.assert_not_awaited()


async def test_a_returners_hit_wins_over_another_guild(bot, env, account):
    """The two produce different transitions (MEMBER vs REGISTERED), so the
    choice has to come from the data rather than from row order."""
    other = "c0000000-0000-4000-8000-00000000000c"
    env.live_guilds = {other: "KongoBoys", ALT: "Returners"}
    env.alt_rows = [_mc_row(other), _mc_row(ALT)]
    plan = _plan(owned_uuids=frozenset({MAIN, ALT, other}))

    assert await hrd.verify_return_dm(bot, account, plan) is False
    assert env.heal.await_args.args[1].uuid == ALT


async def test_a_genuinely_guildless_alt_lets_the_dm_through(bot, env, account):
    env.live_guilds = {ALT: None}
    env.alt_rows = [_mc_row(ALT)]
    plan = _plan(owned_uuids=frozenset({MAIN, ALT}))
    assert await hrd.verify_return_dm(bot, account, plan) is True
    env.heal.assert_not_awaited()


async def test_an_expensive_rejection_keeps_its_slot(bot, env, account):
    """It already spent the requests the cap exists to bound."""
    hrd._SEND_TIMES.append(NOW)
    env.live_guilds = {ALT: "KongoBoys"}
    env.alt_rows = [_mc_row(ALT)]
    await hrd.verify_return_dm(bot, account, _plan(owned_uuids=frozenset({MAIN, ALT})))
    assert len(hrd._SEND_TIMES) == 1


async def test_a_free_rejection_releases_its_slot(bot, env, account):
    hrd._SEND_TIMES.append(NOW)
    bot.member.roles = [MagicMock(id=9999)]
    await hrd.verify_return_dm(bot, account, _plan())
    assert len(hrd._SEND_TIMES) == 0


async def test_verify_rechecks_the_hiatus_role(bot, env, account):
    """The shared crosscheck can itself heal them out of HIATUS between the
    plan and the send."""
    bot.member.roles = [MagicMock(id=9999)]
    plan = _plan(notice_id=None, last_sent_at=None, snooze_armed=False, via_snooze=False, owned_uuids=frozenset({MAIN}),
    )
    assert await hrd.verify_return_dm(bot, account, plan) is False


async def test_verify_drops_a_member_who_left_the_server(bot, env, account):
    bot.guild.get_member.return_value = None
    plan = _plan(notice_id=None, last_sent_at=None, snooze_armed=False, via_snooze=False, owned_uuids=frozenset({MAIN}),
    )
    assert await hrd.verify_return_dm(bot, account, plan) is False

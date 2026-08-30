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
    return SimpleNamespace(uuid=MAIN, mc_username="Arxhe_", guild=None)


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
    monkeypatch.setattr(cfg, "ROLE_HIATUS", HIATUS_ROLE)

    state = SimpleNamespace(
        notice=None,          # what HiatusReturnNotice.filter().first() yields
        in_a_guild=False,     # does the person hold ANY account in a guild (stored)
        rejoined=False,       # last_in_returners_at > last_sent_at on any account
        blocked=False,        # any linked account on the blocklist
        claimed=1,            # rows affected by the CAS update
        owned={MAIN},         # every MC account the person owns
        alt_rows=[],          # rows returned for `await MinecraftAccount.filter(uuid__in=...)`
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
    refresh = AsyncMock(side_effect=lambda mc: mc)
    heal = AsyncMock()
    monkeypatch.setattr(resolve, "refresh_mc_guild", refresh)
    monkeypatch.setattr(ha, "heal_stale_hiatus", heal)

    state.dm = dm
    state.claim = claim
    state.refresh = refresh
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
    plan = hrd.ReturnDmPlan(
        disc_uuid=DISC_UUID, notice_id=env.notice.id,
        last_sent_at=env.notice.last_sent_at, snooze_armed=False, via_snooze=False,
    )
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
    plan = hrd.ReturnDmPlan(
        disc_uuid=DISC_UUID, notice_id=env.notice.id,
        last_sent_at=None, snooze_armed=False, via_snooze=False,
    )
    assert await hrd.send_return_dm(bot, account, plan) is False
    env.dm.assert_not_awaited()


async def test_a_failed_dm_stays_stamped(bot, env, account):
    """Closed DMs must not re-arm: the claim already landed, so the next
    30-second tick finds the floor in place rather than trying again."""
    env.notice = _notice()
    env.dm.return_value = False
    plan = hrd.ReturnDmPlan(
        disc_uuid=DISC_UUID, notice_id=env.notice.id,
        last_sent_at=None, snooze_armed=False, via_snooze=False,
    )
    assert await hrd.send_return_dm(bot, account, plan) is False
    env.claim.update.assert_awaited_once()


async def test_send_consumes_fleet_budget(bot, env, account):
    env.notice = _notice()
    plan = hrd.ReturnDmPlan(
        disc_uuid=DISC_UUID, notice_id=env.notice.id,
        last_sent_at=None, snooze_armed=False, via_snooze=False,
    )
    await hrd.send_return_dm(bot, account, plan)
    assert len(hrd._SEND_TIMES) == 1


async def test_send_is_a_noop_when_the_member_left(bot, env, account):
    bot.guild.get_member.return_value = None
    plan = hrd.ReturnDmPlan(
        disc_uuid=DISC_UUID, notice_id=None,
        last_sent_at=None, snooze_armed=False, via_snooze=False,
    )
    assert await hrd.send_return_dm(bot, account, plan) is False
    env.dm.assert_not_awaited()


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


def test_body_names_the_account_and_asserts_no_cause():
    """HIATUS is granted by BECAME_GUILDLESS, which fires for a voluntary
    leave and a transfer as readily as for an inactivity sweep. The copy
    must not state a cause as fact."""
    body = hrd.build_dm_body("Arxhe_")
    assert "Arxhe_" in body
    assert "/onlinemembers VETS" in body
    assert "case sensitive" in body
    assert "kicked" not in body.lower()


# ---------------------------------------------------------------------------
# blocklist
# ---------------------------------------------------------------------------


async def test_blocked_user_is_never_messaged(bot, env, account):
    """A blocklisted person is forced to REGISTERED-only and can never be
    Hiatus, so "ask anyone for a re-invite" is the last thing to tell
    them."""
    env.blocked = True
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_block_is_read_from_the_table_not_the_roles(bot, env, account):
    """``/block`` only enforces roles when the member is in the Discord
    guild, and logs-and-continues when the role write fails — so a blocked
    user holding a stale HIATUS role is reachable, and the role check
    alone would let them through."""
    env.blocked = True
    bot.member.roles = [MagicMock(id=HIATUS_ROLE)]  # stale role still held
    assert await hrd.plan_return_dm(bot, account, login_edge=True) is None


async def test_a_block_landing_between_the_two_phases_still_stops_it(bot, env, account):
    plan = hrd.ReturnDmPlan(
        disc_uuid=DISC_UUID, notice_id=None, last_sent_at=None,
        snooze_armed=False, via_snooze=False, owned_uuids=frozenset({MAIN}),
    )
    env.blocked = True
    assert await hrd.verify_return_dm(bot, account, plan) is False


# ---------------------------------------------------------------------------
# the live verify pass: is the HIATUS role actually correct?
# ---------------------------------------------------------------------------


async def test_verify_passes_when_there_are_no_other_accounts(bot, env, account):
    plan = hrd.ReturnDmPlan(
        disc_uuid=DISC_UUID, notice_id=None, last_sent_at=None,
        snooze_armed=False, via_snooze=False, owned_uuids=frozenset({MAIN}),
    )
    assert await hrd.verify_return_dm(bot, account, plan) is True
    # The spotted account was already refreshed by the caller; don't pay twice.
    env.refresh.assert_not_awaited()


async def test_an_alt_live_in_another_guild_stops_the_dm_and_heals(bot, env, account):
    """The gap the stored-column check cannot close: nothing scans a guild
    no Returners member is in, so the alt's `guild` reads NULL forever."""
    env.owned = {MAIN, ALT}
    env.alt_rows = [_mc_row(ALT)]

    async def _joined(mc):
        mc.guild = "KongoBoys"
        return mc

    env.refresh.side_effect = _joined

    plan = hrd.ReturnDmPlan(
        disc_uuid=DISC_UUID, notice_id=None, last_sent_at=None,
        snooze_armed=False, via_snooze=False, owned_uuids=frozenset({MAIN, ALT}),
    )
    assert await hrd.verify_return_dm(bot, account, plan) is False
    env.refresh.assert_awaited_once()
    # They don't just lose the DM — they get the transition they were owed,
    # which drops them out of the watchers' scope for good.
    env.heal.assert_awaited_once()
    assert env.heal.await_args.args[1].uuid == ALT


async def test_a_genuinely_guildless_alt_lets_the_dm_through(bot, env, account):
    env.owned = {MAIN, ALT}
    env.alt_rows = [_mc_row(ALT)]
    plan = hrd.ReturnDmPlan(
        disc_uuid=DISC_UUID, notice_id=None, last_sent_at=None,
        snooze_armed=False, via_snooze=False, owned_uuids=frozenset({MAIN, ALT}),
    )
    assert await hrd.verify_return_dm(bot, account, plan) is True
    env.refresh.assert_awaited_once()
    env.heal.assert_not_awaited()


async def test_verify_rechecks_the_hiatus_role(bot, env, account):
    """The shared crosscheck can itself heal them out of HIATUS between the
    plan and the send."""
    bot.member.roles = [MagicMock(id=9999)]
    plan = hrd.ReturnDmPlan(
        disc_uuid=DISC_UUID, notice_id=None, last_sent_at=None,
        snooze_armed=False, via_snooze=False, owned_uuids=frozenset({MAIN}),
    )
    assert await hrd.verify_return_dm(bot, account, plan) is False


async def test_verify_drops_a_member_who_left_the_server(bot, env, account):
    bot.guild.get_member.return_value = None
    plan = hrd.ReturnDmPlan(
        disc_uuid=DISC_UUID, notice_id=None, last_sent_at=None,
        snooze_armed=False, via_snooze=False, owned_uuids=frozenset({MAIN}),
    )
    assert await hrd.verify_return_dm(bot, account, plan) is False

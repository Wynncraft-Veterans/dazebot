"""Tortoise ORM models that live in ``returns.db``, separate from the
main ``dazebot.db``.

Returners weekly events are ad-hoc, one-shot affairs — every week_N picks
a different game or scoring quirk and may invent a table just for that
week. Keeping these ephemeral tables in the same DB as the carefully-
modelled dazebot core (accounts, link codes, donations, CTP, cults,
apartments) means a regression in a single ``cogs/events/returns/week_N.py``
can corrupt the well-thought-out schema. The split also makes "wipe last
year of stale returns data" a file-level rather than per-table operation.

What stays in ``dazebot.db`` (NOT here):
- Week 0 / cults: ``Cult``, ``CultMembership``, ``IntercultMessage``,
  ``RecruitmentQuery`` — these are permanent infrastructure used across
  weeks for cooldowns and cult bookkeeping.
- ``Apartment`` — community feature, not week-scoped.
- ``WeeklyEvent`` / ``Score`` — scoring scaffold every return uses.

What lives here:
- ``ReturnGuess`` (week 75 — emerald-price guessing)
- ``StorySegment`` (week 73 — collaborative story)
- ``Return3*`` (week 3 — four-faction territorial conquest: game state,
  tiles, votes, alert subscriptions, dashboard message pointers)

The models intentionally do **not** FK to ``DiscordAccount``: SQLite has
no cross-database FK enforcement, so the relation would be unenforceable
anyway. The established workspace pattern for "reference a Discord user
without enforcing FK" is to store the snowflake ``disc_uuid`` directly as
a CharField — see ``IntercultMessage.sender_disc_uuid``,
``RecruitmentQuery.requester_disc_uuid``, ``DMSentLog.disc_uuid``, etc.
in ``orm.py``.
"""

from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


class ReturnGuess(Model):
    """One user's emerald-price guess for a ``/return 75`` day-N slot.

    Used by ``cogs/events/returns/week_75.py``: each accepted submission
    appends one row. Unique per (``week``, ``day``, ``disc_uuid``) —
    re-submission for the same day is rejected (the user must ask staff
    if they want their guess cleared).

    Keyed by ``week`` so a future price-guessing week picks a new int
    with no schema change. ``day`` is 1-7. ``price`` is the guess in raw
    emeralds; the manage view re-renders it as stx/le/e.
    """

    id = fields.UUIDField(pk=True)
    week = fields.IntField(index=True)
    day = fields.IntField()
    disc_uuid = fields.CharField(max_length=255, index=True)
    price = fields.IntField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "return_guesses"
        unique_together = (("week", "day", "disc_uuid"),)


class StorySegment(Model):
    """One fragment of a collaborative ``/return`` story event.

    Used by ``cogs/events/returns/week_73.py``: each accepted submission
    appends one row. The full story is reconstructed by ordering rows
    for a ``week`` by ``created_at`` and concatenating ``content``.

    Keyed by ``week`` so a future story week just picks a new int — no
    schema change. Scoring still goes through ``WeeklyEvent``/``Score``
    in ``orm.py`` (those stay in dazebot.db). The back-to-back guard
    reads the most recent row's ``disc_uuid`` to forbid the same author
    twice running.
    """

    id = fields.UUIDField(pk=True)
    week = fields.IntField(index=True)
    disc_uuid = fields.CharField(max_length=255, index=True)
    content = fields.TextField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "story_segments"


# ---------------------------------------------------------------------------
# Return 3 — four-faction territorial conquest
# ---------------------------------------------------------------------------
#
# A 5x5 tile map. Four cult corners as capitals. Each turn lasts 24h; turn
# order is set once at launch by a lots-drawing minigame. One vote per user
# per turn across three action types: reinforce, march, attack. See
# ``cogs/events/returns/week_3.py`` for the rules and dispatcher.


class Return3GameState(Model):
    """Singleton row holding the in-progress Return 3 game.

    ``id`` is always 1; the manage ``start`` subcommand refuses to clobber an
    existing row unless ``--force`` is passed. Drafting-phase lots are stored
    here (4 nullable ints) rather than in their own table — there are only
    four of them and they exist for at most 6 hours.

    ``turn_order_csv`` is the comma-separated cult name sequence determined
    by drafting (highest lot first). ``current_turn_number`` is 0-indexed
    against this list; the active cult is
    ``turn_order_csv.split(",")[n % 4]`` and the current loop is ``n // 4``.
    """

    id = fields.IntField(pk=True)
    phase = fields.CharField(max_length=16)  # "drafting" | "active" | "ended"
    started_at = fields.DatetimeField()
    drafting_deadline = fields.DatetimeField(null=True)
    lot_naz = fields.IntField(null=True)
    lot_deer = fields.IntField(null=True)
    lot_wen = fields.IntField(null=True)
    lot_fish = fields.IntField(null=True)
    turn_order_csv = fields.CharField(max_length=64, null=True)
    current_turn_number = fields.IntField(default=0)
    turn_started_at = fields.DatetimeField(null=True)
    dominance_streak_loops = fields.IntField(default=0)
    dominance_leader_cult = fields.CharField(max_length=32, null=True)
    winner_cult = fields.CharField(max_length=32, null=True)
    ended_at = fields.DatetimeField(null=True)

    class Meta:
        table = "return_3_game_state"


class Return3Tile(Model):
    """One tile on the 5x5 board. ``id`` == row * 5 + col (0..24)."""

    id = fields.IntField(pk=True)
    controlling_cult = fields.CharField(max_length=32, null=True)
    army_count = fields.IntField(default=0)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "return_3_tiles"


class Return3Vote(Model):
    """One vote in one turn. UNIQUE on (turn_number, voter) — vote changes
    are UPSERTs, not appends, so the user's *latest* selection is the one
    that counts at the deadline.

    ``source_tile_id``/``target_tile_id`` semantics depend on ``action_kind``:
    reinforce uses ``source_tile_id`` as the target (no second tile); march
    and attack use both as (source, dest).
    """

    id = fields.UUIDField(pk=True)
    turn_number = fields.IntField(index=True)
    voter_disc_uuid = fields.CharField(max_length=255, index=True)
    action_kind = fields.CharField(max_length=16)  # "reinforce" | "march" | "attack"
    source_tile_id = fields.IntField()
    target_tile_id = fields.IntField(null=True)
    voted_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "return_3_votes"
        unique_together = (("turn_number", "voter_disc_uuid"),)


class Return3Subscription(Model):
    """Opt-in turn-alert subscription. Cult is resolved at notify-time via
    ``CultMembership`` in dazebot.db, so a user who changes cults mid-event
    automatically follows their current cult.
    """

    id = fields.UUIDField(pk=True)
    disc_uuid = fields.CharField(max_length=255, unique=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "return_3_subscriptions"


class Return3Dashboard(Model):
    """One row per cult: which message in which thread holds that cult's
    fog-of-war dashboard. Keyed by ``cult`` so look-up is direct.
    """

    cult = fields.CharField(max_length=32, pk=True)
    thread_id = fields.BigIntField()
    message_id = fields.BigIntField()

    class Meta:
        table = "return_3_dashboards"

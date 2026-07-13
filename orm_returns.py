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


# ---------------------------------------------------------------------------
# Return 81 — team photo bingo
# ---------------------------------------------------------------------------
#
# Three players form a team via ``/return 81`` (caller + two invitees). Once
# both invitees accept, a private thread is created under the r81 parent
# channel and the team picks a fourth wildcard teammate from a shortlist of
# ten drawn from the in-game guild. The team then works through a 4x4 bingo
# card (A1..D4) by posting photos with ``~return 81 submit <cell>``; each
# accepted submission scores the whole team via ``WeeklyEvent(week=81)``.
# Bingo lines (rows/cols/diagonals) unlock optional "extra card" / "extra
# teammate" bonuses. See ``cogs/events/returns/week_81.py`` for the flow.


class BingoTeam(Model):
    """One r81 team. Grows through phases: pending (invites out) → picking
    (wildcard shortlist posted) → playing (bingo card live) → disbanded.

    ``team_number`` is the monotonic integer used in the private thread's
    display name (``r81-Team-<n>``). ``thread_id`` and the five ``*_msg_id``
    columns are populated during ``_post_pinned_dashboard`` and let the
    submission listener edit the right embed in place without walking
    Discord history.
    """

    id = fields.UUIDField(pk=True)
    week = fields.IntField(index=True)
    team_number = fields.IntField(unique=True)
    creator_disc_uuid = fields.CharField(max_length=255, index=True)
    state = fields.CharField(max_length=16)  # "pending" | "picking" | "playing" | "disbanded"
    thread_id = fields.BigIntField(null=True)
    status_msg_id = fields.BigIntField(null=True)
    embed_msg_1_id = fields.BigIntField(null=True)
    embed_msg_2_id = fields.BigIntField(null=True)
    embed_msg_3_id = fields.BigIntField(null=True)
    embed_msg_4_id = fields.BigIntField(null=True)
    picker_msg_id = fields.BigIntField(null=True, index=True)
    # Ordered 10-element JSON list of disc_uuids; slot i is the ith button
    # on the picker post. Populated when the team enters "picking".
    picker_candidates_json = fields.TextField(null=True)
    # Id of the shared invite post in INVITE_THREAD_ID that both invitees
    # click Accept/Decline on. Persistent view dispatch resolves the team
    # from this + the clicker's id.
    pending_invite_msg_id = fields.BigIntField(null=True, index=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    invites: fields.ReverseRelation["BingoInvite"]
    members: fields.ReverseRelation["BingoTeamMember"]
    cells: fields.ReverseRelation["BingoCellState"]
    submissions: fields.ReverseRelation["BingoSubmission"]
    bingos: fields.ReverseRelation["BingoBingoEvent"]

    class Meta:
        table = "return_81_teams"


class BingoInvite(Model):
    """One outstanding (or resolved) invite to join a specific team. Both
    invitees start ``pending``; the team advances out of ``pending`` only
    when *both* rows for it are ``accepted``.
    """

    id = fields.UUIDField(pk=True)
    team = fields.ForeignKeyField(
        "returns.BingoTeam", related_name="invites", on_delete=fields.CASCADE
    )
    invitee_disc_uuid = fields.CharField(max_length=255, index=True)
    state = fields.CharField(max_length=16)  # "pending" | "accepted" | "declined"
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "return_81_invites"
        unique_together = (("team", "invitee_disc_uuid"),)


class BingoTeamMember(Model):
    """One confirmed member of a team. ``role`` records how they joined:
    ``creator`` (invoked ``/return 81``), ``invitee`` (accepted invite),
    ``wildcard`` (picked from the random shortlist), ``forced`` (added by
    staff via ``~manage_return 81 forceAdd``). More ``wildcard`` rows can
    appear later if the team redeems ``extra_member`` bonuses.
    """

    id = fields.UUIDField(pk=True)
    team = fields.ForeignKeyField(
        "returns.BingoTeam", related_name="members", on_delete=fields.CASCADE
    )
    disc_uuid = fields.CharField(max_length=255, index=True)
    role = fields.CharField(max_length=16)  # "creator" | "invitee" | "wildcard" | "forced"
    joined_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "return_81_team_members"
        unique_together = (("team", "disc_uuid"),)


class BingoCellState(Model):
    """Per-cell placeholder caption, seeded once when the team enters
    ``playing``. Mutating this row via ``~manage_return 81 regen`` re-rolls
    the caption without touching submissions or scores.
    """

    id = fields.UUIDField(pk=True)
    team = fields.ForeignKeyField(
        "returns.BingoTeam", related_name="cells", on_delete=fields.CASCADE
    )
    cell = fields.CharField(max_length=2)  # "A1".."D4"
    caption = fields.TextField()

    class Meta:
        table = "return_81_cell_states"
        unique_together = (("team", "cell"),)


class BingoSubmission(Model):
    """One accepted photo submission for a given team+cell. Overwrites are
    refused at the listener; staff must clear the row first with
    ``~manage_return 81 clear`` (or ``regen``, which combines the clear
    with a caption reroll).
    """

    id = fields.UUIDField(pk=True)
    team = fields.ForeignKeyField(
        "returns.BingoTeam", related_name="submissions", on_delete=fields.CASCADE
    )
    cell = fields.CharField(max_length=2)  # "A1".."D4"
    submitter_disc_uuid = fields.CharField(max_length=255)
    image_url = fields.TextField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "return_81_submissions"
        unique_together = (("team", "cell"),)


class BingoBingoEvent(Model):
    """One completed bingo line for a team. ``line_key`` is the canonical
    pipe-joined cell list (e.g. ``"A1|A2|A3|A4"``) so the same line can't
    fire twice. ``bonus_choice`` is set the first time the team clicks
    either follow-up button and the ``BingoBonusView`` becomes idempotent
    against further clicks.
    """

    id = fields.UUIDField(pk=True)
    team = fields.ForeignKeyField(
        "returns.BingoTeam", related_name="bingos", on_delete=fields.CASCADE
    )
    line_key = fields.CharField(max_length=32)
    bonus_choice = fields.CharField(max_length=16, null=True)  # "extra_card" | "extra_member"
    bonus_msg_id = fields.BigIntField(null=True, index=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "return_81_bingo_events"
        unique_together = (("team", "line_key"),)

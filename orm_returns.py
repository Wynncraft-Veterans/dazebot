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

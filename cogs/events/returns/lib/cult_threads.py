"""Legacy seed map: cult-name -> private-thread-id under cult parent
channel 1313786225735237654.

**Authoritative storage is now ``Cult.thread_id``** (the DB column added
in the Return overhaul). This map is consulted exactly once per row by
:func:`cogs.events.returns.week_0.backfill_thread_ids_from_dict` to seed
the new column for the six grandfathered cults on first deploy. Nothing
else imports it.

Once every cult row has ``thread_id`` populated in production, this
module — and the backfill call in ``Returns.cog_load`` — can be deleted.

Bot needs Manage Messages (or Manage Threads) on the parent channel to
add/remove non-invited members on private threads.
"""

from __future__ import annotations


CULT_THREADS: dict[str, int] = {
    "wencult":    1501233308284092546,
    "deercult":   1501233117829140480,
    "nazcult":    1501233190285738115,
    "fishcult":   1501232813943292026,
    "brycult":    1501233371802767420,
    "xandercult": 1501233419135221860,
}

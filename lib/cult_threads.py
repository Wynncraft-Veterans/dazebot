"""Static cult-name -> private-thread-id map under the cult parent channel
1313786225735237654. Bot needs Manage Messages (or Manage Threads) on the
parent to add/remove non-invited members on private threads.

Lives at the lib/ level (not inside cogs/events/returns/week_0.py) so the
intercult view can import it without a cycle: week_0 imports the view,
the view imports this map.
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

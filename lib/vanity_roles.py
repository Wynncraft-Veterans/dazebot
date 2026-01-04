"""Resolve datetime to vanity role.

`get_vanity_role_id` accepts `datetime.datetime` and `datetime.date`
Returns none of the user joined after the guild's extended definition of 'legacy'.
Everything is done in utc.
"""
from __future__ import annotations

from datetime import datetime, date, timezone
from typing import Optional, Union

__all__ = ["get_vanity_role_id"]

# Cutoffs and role IDs, ordered ascending.
_CUTOFFS = [
    (datetime(2013, 5, 7), "1318063966420729866"),
    (datetime(2013, 6, 29), "1318064262681464882"),
    (datetime(2013, 10, 30), "1318072464982675456"),
    (datetime(2014, 8, 1), "1318072904474165298"),
    (datetime(2014, 12, 22), "1318073239683207219"),
    (datetime(2015, 12, 20), "1318073513453682698"),
    (datetime(2017, 4, 7), "1318073571477815357"),
    (datetime(2017, 12, 15), "1318073572031205376"),
    (datetime(2019, 1, 18), "1318073572777918554"),
    (datetime(2019, 12, 8), "1318073573667246151"),
]


def _normalize_to_naive_utc(dt: Union[datetime, date]) -> datetime:
    """Cleans inputs to be utc midnight"""
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return datetime(dt.year, dt.month, dt.day)

    if dt.tzinfo is not None:
        # Utc, get rid of timezone info.
        return dt.astimezone(timezone.utc).replace(tzinfo=None)

    return dt  # naive datetime


def get_vanity_role_id(ts: Union[datetime, date]) -> Optional[str]:
    """Return role based on datetime

    Examples:
        >>> from datetime import datetime
        >>> get_vanity_role_id(datetime(2013, 1, 1))
        '1318063966420729866'
        >>> get_vanity_role_id(datetime(2020, 1, 1)) is None
        True

    Returns:
        The role ID string if a matching interval is found, otherwise ``None``.
    """
    dt = _normalize_to_naive_utc(ts)

    for cutoff, role_id in _CUTOFFS:
        if dt < cutoff:
            return role_id

    return None

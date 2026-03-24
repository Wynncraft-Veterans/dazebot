from __future__ import annotations

from datetime import datetime, date, timezone


from config import Config


def _normalize_to_naive_utc(dt: datetime | date) -> datetime:
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return datetime(dt.year, dt.month, dt.day)

    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)

    return dt


def get_vanity_role_id(ts: datetime | date, config: Config) -> str | None:
    # dt = _normalize_to_naive_utc(ts) # shouldnt be needed, just always make sure everything is timezone aware!

    for cutoff, role_id in config.VanityRolesConfig.CUTOFFS:
        if ts < cutoff:
            return role_id

    return None

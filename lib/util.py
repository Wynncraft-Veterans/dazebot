"""Tiny generic helpers that don't belong to any single domain.

Was previously ``lib/lib.py``; renamed for clarity. Keep this file small;
new code with a clear domain (mc, staff, discord_utils, …) belongs in
that package instead.
"""

from enum import Enum


def unwrap[T](v: T | None) -> T:
    assert v is not None
    return v


class ProfCategory(Enum):
    PLEB = "pleb"
    VOID = "void"
    DERNIC = "dernic"
    TITANIUM = "titanium"
    CINNABAR = "cinnabar"

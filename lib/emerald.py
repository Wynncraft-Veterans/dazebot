"""Emerald-value parser and stx formatter for ``~donations``.

Wynncraft emerald-currency conventions::

    1 e   = 1 emerald (base unit)
    1 eb  = 64 e          (an emerald block)
    1 le  = 4096 e        (a liquid emerald)
    1 stx = 262144 e      (a stack of liquid emeralds)

The parser accepts (case-insensitive, whitespace tolerated)::

    "5436584"             -> 5436584
    "5436584e"            -> 5436584
    "20stx47le18eb40e"    -> 5436584
    "20stx 47le 18eb 40e" -> 5436584
    "20stx47.29le"        -> 5436584   (decimals legal on any unit; floored)
    "20STX"               -> 20 * 262144

The formatter renders integer raw emeralds as decimal stx with exactly two
decimal places::

    5_436_584 -> "20.74 stx"
    0         -> "0.00 stx"

This is intentionally a separate module from the week_75 ``_format_emeralds``
which produces the different ``{stx}stx {decimal_le}le`` shape used by the
day-N price guess UI. Don't try to share — they're different display formats
for different consumers.
"""

from __future__ import annotations

import re

E_PER_EB = 64
E_PER_LE = 4096
E_PER_STX = 262144


class EmeraldParseError(ValueError):
    """Raised when ``parse_emeralds`` can't make sense of its input.

    Carries the original token in ``.token`` so the cog can echo it back in
    its error reply.
    """

    def __init__(self, message: str, token: str | None = None) -> None:
        super().__init__(message)
        self.token = token


_UNIT_MULTIPLIER = {
    "stx": E_PER_STX,
    "le": E_PER_LE,
    "eb": E_PER_EB,
    "e": 1,
}

# Alternation order matters: stx → le → eb → e so the regex engine doesn't
# match "e" inside "le"/"eb"/"stx".
_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(stx|le|eb|e)")
_BARE_RE = re.compile(r"^\d+(?:\.\d+)?$")


def parse_emeralds(s: str) -> int:
    """Parse an emerald-value string into raw integer emeralds.

    See the module docstring for the accepted formats. A bare number with
    no unit is treated as raw ``e``. Decimal values are floored after
    multiplication.

    Raises ``EmeraldParseError`` on empty input, negatives, unknown units,
    or any leftover unparsed characters.
    """
    if not isinstance(s, str):
        raise EmeraldParseError(f"expected a string, got {type(s).__name__}")
    cleaned = s.strip().lower()
    if not cleaned:
        raise EmeraldParseError("empty value", token=s)
    if cleaned.startswith("-"):
        raise EmeraldParseError(f"negative values are not allowed: {s!r}", token=s)

    if _BARE_RE.match(cleaned):
        return int(float(cleaned))

    total = 0
    pos = 0
    saw_any_token = False
    while pos < len(cleaned):
        while pos < len(cleaned) and cleaned[pos].isspace():
            pos += 1
        if pos >= len(cleaned):
            break
        m = _TOKEN_RE.match(cleaned, pos)
        if not m:
            raise EmeraldParseError(
                f"could not parse emerald value {s!r} at offset {pos}",
                token=s,
            )
        value_str, unit = m.group(1), m.group(2)
        total += int(float(value_str) * _UNIT_MULTIPLIER[unit])
        pos = m.end()
        saw_any_token = True

    if not saw_any_token:
        raise EmeraldParseError(f"no recognised emerald tokens in {s!r}", token=s)
    return total


def format_emeralds_as_stx(n: int) -> str:
    """Render ``n`` raw emeralds as ``"{x.xx} stx"``.

    Always two decimal places (trailing zeros preserved). ``n=0`` returns
    ``"0.00 stx"``. Negative values render with a leading minus.
    """
    return f"{n / E_PER_STX:.2f} stx"

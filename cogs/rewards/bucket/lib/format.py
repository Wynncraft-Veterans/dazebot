"""Line / message builders for the ``~bucket`` cog.

Kept separate from the cog so the wording can be sanity-checked in
isolation. The ``check`` wording is taken verbatim from the user's
spec — preserve the singular noun ("3 bucket 1 pull") even when the
count is > 1.
"""

from __future__ import annotations

from datetime import datetime, timezone

from orm import BucketPull


def format_check_lines(counts: dict[int, int]) -> list[str]:
    """Render the body of ``~bucket check``.

    ``counts`` is ``{tier: outstanding_pull_count}``. An empty dict gives
    the "nothing to claim" sentence; otherwise the multi-line outstanding
    block. Tiers are listed in numeric order regardless of how many
    pulls each holds.
    """
    if not counts:
        return ["You have no outstanding bucket prizes to claim!"]
    lines = ["You have the following outstanding bucket prizes to claim!"]
    for tier in sorted(counts):
        lines.append(f"{counts[tier]} bucket {tier} pull")
    lines.append("")
    lines.append("Please talk to staff about claiming them: your pulls expire in 6mo.")
    return lines


def format_history_line(pull: BucketPull) -> str:
    """One row for ``~bucket info`` — state + tier + id + reason.

    State is one of ``REDEEMED <ts>`` / ``EXPIRED`` /
    ``outstanding (expires <ts>)``. The ``<t:...:R>`` markers render as
    Discord-local relative timestamps on the client side.
    """
    now = datetime.now(timezone.utc)
    if pull.redeemed_at is not None:
        state = f"REDEEMED <t:{int(pull.redeemed_at.timestamp())}:R>"
    elif pull.expires_at <= now:
        state = "EXPIRED"
    else:
        state = f"outstanding (expires <t:{int(pull.expires_at.timestamp())}:R>)"
    return f"`#{pull.id}` **bucket {pull.tier}** — {state} — {pull.reason}"

"""Week 3: four-faction territorial conquest.

A long-running (~4mo) one-off event where the four cult threads
(nazcult, deercult, wencult, fishcult) fight over a shared 5x5 map.
Each turn lasts 24h; turn order is set once at launch by a lots-drawing
minigame and fixed for the rest of the game. Each cult's pinned thread
post is a fog-of-war dashboard that's edited in place — never re-posted
— with three action selects (Reinforce / March / Attack) and an alerts
toggle button. Only the active cult's selects accept votes. At the
deadline the top-voted action is applied; ties broken randomly; zero
votes triggers a random legal action.

Hard rules:

* Tiles are 5x5 (cols A-E, rows 1-5), orthogonal adjacency. Capitals at
  the four corners. ``TILE_LABELS`` is the source-of-truth mapping
  between ``tile_id`` and human label.
* Reinforce: +``REINFORCE_AMOUNT`` armies on a friendly tile.
* March: friendly → friendly adjacent. Source sends ``armies - 1``
  (keeps 1 garrison); destination gains that many.
* Attack: friendly → enemy/neutral adjacent. Source sends ``armies - 1``;
  bigger wins. Attacker wins → tile flips with
  ``|attacker_sent - defender|`` survivors. Defender wins (incl. ties)
  → tile stays, defender reduced to ``max(0, defender - attacker_sent)``.
* Passive growth: +``LOOP_GROWTH`` to every owned tile when a new loop
  starts (``current_turn_number % 4 == 0``).
* Win: dominance (≥``DOMINANCE_TILE_THRESHOLD`` tiles at the end of
  ``DOMINANCE_LOOP_STREAK`` consecutive loops) OR elimination (single
  cult with any tiles).

State persistence lives in ``orm_returns.py`` (Return3*); the game can
survive bot restarts because the tick loop reconstructs everything from
DB rows at each 60s sweep.

Persistent views are registered in ``bot.py:setup_hook`` — see
:class:`Return3ActionView` and :class:`Return3DraftView`.
"""

from __future__ import annotations

import logging
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import discord
from discord.ext import commands

from cogs.events.returns import (
    Tier,
    register,
    register_manage,
    register_startup,
    register_tick,
)
from cogs.events.returns._common import is_persist_context, send_feedback
from config import CurrConfig
from lib.mc.linking import dm_or_log
from orm import Cult, CultMembership, DiscordAccount
from orm_returns import (
    Return3Dashboard,
    Return3GameState,
    Return3Subscription,
    Return3Tile,
    Return3Vote,
)

logger = logging.getLogger("dazebot.cogs.events.returns.week_3")

WEEK = 3

# Hardcoded per the operator's preference: don't pull from
# lib/cult_threads.py because those are operator-rotated as cults are
# created/retired, and Return 3 needs a fixed roster for its lifetime.
CULT_THREADS: dict[str, int] = {
    "nazcult":  1501233190285738115,
    "deercult": 1501233117829140480,
    "wencult":  1501233308284092546,
    "fishcult": 1501232813943292026,
}
THREAD_TO_CULT: dict[int, str] = {tid: cult for cult, tid in CULT_THREADS.items()}
ALL_CULTS: list[str] = list(CULT_THREADS.keys())  # stable iteration order

# In-lore colors (see project memory): nazcult red, deercult yellow,
# wencult purple, fishcult blue. Used in the map grid; neutral visible
# is white, fog is black.
CULT_EMOJI: dict[str, str] = {
    "nazcult":  "🟥",
    "deercult": "🟨",
    "wencult":  "🟪",
    "fishcult": "🟦",
}
NEUTRAL_EMOJI = "⬜"
FOG_EMOJI = "⬛"

# Lot field on the singleton GameState row. Keep in sync with the model.
_LOT_FIELDS = {
    "nazcult":  "lot_naz",
    "deercult": "lot_deer",
    "wencult":  "lot_wen",
    "fishcult": "lot_fish",
}

# Grid geometry. 5x5 = 25 tiles.
GRID_W = 5
GRID_H = 5
TILE_COUNT = GRID_W * GRID_H

# Tile labels: A1..E5. Column letter (A..E) is the x coord; row digit
# (1..5) is the y coord. tile_id == row * 5 + col.
_COL_LETTERS = "ABCDE"

# Capitals — one per cult, at a corner.
CAPITAL_TILE: dict[str, int] = {
    "nazcult":  0,   # A1 (row 0 col 0)
    "deercult": 4,   # E1 (row 0 col 4)
    "wencult":  24,  # E5 (row 4 col 4)
    "fishcult": 20,  # A5 (row 4 col 0)
}

# Tunables — surfaced at module top so they can be tweaked without
# restructuring code if pacing diverges from the ~4mo target.
TURN_DURATION = timedelta(hours=24)
DRAFTING_DURATION = timedelta(hours=6)
REINFORCE_AMOUNT = 2
LOOP_GROWTH = 1
DOMINANCE_TILE_THRESHOLD = 16  # 64% of 25
DOMINANCE_LOOP_STREAK = 2
STARTING_CAPITAL_ARMIES = 3
VOTE_TALLY_TOP_N = 5  # how many action lines to surface in the dashboard

# Discord Select hard cap; we truncate option lists at this.
_SELECT_MAX_OPTIONS = 25

# The one-time announcement is the user-supplied paragraph, verbatim.
_ANNOUNCEMENT_BODY = (
    "**Return 3 — territorial conquest.**\n"
    "Each cult will be fighting for control of a shared map over the coming "
    "months. Watch this channel's pinned post: it shows your territory, your "
    "armies, and your immediate surroundings. When it's your cult's turn, the "
    "buttons go live and a 24-hour clock starts. Tap a button to cast your "
    "vote. Whichever action has the most votes when the clock hits zero is "
    "what your cult does. If nobody votes, an action will be randomly "
    "selected. Then the turn passes on, and you wait for the map to come "
    "back around. Hold the most ground — or wipe out the other cults — to "
    "win. Wen anticipates this will last for about 4 months, but who knows."
)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def tile_id(row: int, col: int) -> int:
    return row * GRID_W + col


def tile_rc(tid: int) -> tuple[int, int]:
    return divmod(tid, GRID_W)


def tile_label(tid: int) -> str:
    row, col = tile_rc(tid)
    return f"{_COL_LETTERS[col]}{row + 1}"


def label_to_tile(label: str) -> Optional[int]:
    label = label.strip().upper()
    if len(label) < 2:
        return None
    col_ch, row_str = label[0], label[1:]
    if col_ch not in _COL_LETTERS:
        return None
    try:
        row = int(row_str) - 1
    except ValueError:
        return None
    if not (0 <= row < GRID_H):
        return None
    return tile_id(row, _COL_LETTERS.index(col_ch))


def neighbors(tid: int) -> list[int]:
    row, col = tile_rc(tid)
    out: list[int] = []
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nr, nc = row + dr, col + dc
        if 0 <= nr < GRID_H and 0 <= nc < GRID_W:
            out.append(tile_id(nr, nc))
    return out


TILE_LABELS: list[str] = [tile_label(i) for i in range(TILE_COUNT)]


# ---------------------------------------------------------------------------
# Game-state helpers
# ---------------------------------------------------------------------------


async def _load_state() -> Optional[Return3GameState]:
    return await Return3GameState.filter(id=1).first()


async def _load_tiles() -> list[Return3Tile]:
    rows = await Return3Tile.all()
    rows.sort(key=lambda t: t.id)
    return rows


def _current_cult(state: Return3GameState) -> Optional[str]:
    """Return the active cult during ``active`` phase, else None."""
    if state.phase != "active" or not state.turn_order_csv:
        return None
    order = state.turn_order_csv.split(",")
    if not order:
        return None
    return order[state.current_turn_number % len(order)]


def _turn_deadline(state: Return3GameState) -> Optional[datetime]:
    if state.phase == "active" and state.turn_started_at is not None:
        return state.turn_started_at + TURN_DURATION
    if state.phase == "drafting":
        return state.drafting_deadline
    return None


def _fog_of_war_visible(tiles: list[Return3Tile], cult: str) -> set[int]:
    """Tiles a given cult can see: own tiles + their orthogonal neighbors."""
    visible: set[int] = set()
    for t in tiles:
        if t.controlling_cult == cult:
            visible.add(t.id)
            visible.update(neighbors(t.id))
    return visible


def _tile_index(tiles: list[Return3Tile]) -> dict[int, Return3Tile]:
    return {t.id: t for t in tiles}


def _legal_reinforces(tiles_by_id: dict[int, Return3Tile], cult: str) -> list[int]:
    return sorted(tid for tid, t in tiles_by_id.items() if t.controlling_cult == cult)


def _legal_marches(
    tiles_by_id: dict[int, Return3Tile], cult: str
) -> list[tuple[int, int]]:
    """All ordered ``(src, dst)`` where src is friendly with armies ≥ 2 and
    dst is a friendly neighbor. Source must have ≥2 because march sends
    ``armies - 1`` (keeps 1 garrison) — sending 0 is a no-op.
    """
    out: list[tuple[int, int]] = []
    for tid, t in tiles_by_id.items():
        if t.controlling_cult != cult or t.army_count < 2:
            continue
        for n in neighbors(tid):
            nt = tiles_by_id.get(n)
            if nt is None or nt.controlling_cult != cult:
                continue
            out.append((tid, n))
    out.sort()
    return out


def _legal_attacks(
    tiles_by_id: dict[int, Return3Tile], cult: str
) -> list[tuple[int, int]]:
    """All ordered ``(src, dst)`` where src is friendly with armies ≥ 2
    and dst is a non-friendly neighbor (enemy or neutral). Source must
    have ≥2 because attacks send ``armies - 1``.
    """
    out: list[tuple[int, int]] = []
    for tid, t in tiles_by_id.items():
        if t.controlling_cult != cult or t.army_count < 2:
            continue
        for n in neighbors(tid):
            nt = tiles_by_id.get(n)
            if nt is None or nt.controlling_cult == cult:
                continue
            out.append((tid, n))
    out.sort()
    return out


def _legal_actions(
    tiles_by_id: dict[int, Return3Tile], cult: str
) -> tuple[list[int], list[tuple[int, int]], list[tuple[int, int]]]:
    """Returns ``(reinforce_targets, march_pairs, attack_pairs)``."""
    return (
        _legal_reinforces(tiles_by_id, cult),
        _legal_marches(tiles_by_id, cult),
        _legal_attacks(tiles_by_id, cult),
    )


def _any_legal(
    reinforces: list[int],
    marches: list[tuple[int, int]],
    attacks: list[tuple[int, int]],
) -> list[tuple[str, int, Optional[int]]]:
    """Flatten the three lists into uniform ``(kind, src, dst)`` triples
    for random fallback selection.
    """
    out: list[tuple[str, int, Optional[int]]] = []
    for tid in reinforces:
        out.append(("reinforce", tid, None))
    for src, dst in marches:
        out.append(("march", src, dst))
    for src, dst in attacks:
        out.append(("attack", src, dst))
    return out


# ---------------------------------------------------------------------------
# Vote resolution + action application
# ---------------------------------------------------------------------------


async def _resolve_turn_vote(
    state: Return3GameState,
    tiles_by_id: dict[int, Return3Tile],
    active_cult: str,
) -> tuple[str, int, Optional[int]]:
    """Pick the winning action for the turn that's ending.

    1. Aggregate this turn's votes by ``(kind, src, dst)`` and pick the
       max-tally bucket; random tiebreak among the maxima.
    2. If zero votes: uniform-random legal action.
    3. If the chosen action is no longer legal (state may have shifted
       under it since the vote): fall through to a random legal action.
    """
    votes = await Return3Vote.filter(turn_number=state.current_turn_number).all()
    reinforces, marches, attacks = _legal_actions(tiles_by_id, active_cult)
    legal_triples = _any_legal(reinforces, marches, attacks)
    legal_set = set(legal_triples)

    if not votes:
        if not legal_triples:
            # Pathological: cult has no friendly tiles with ≥2 armies and
            # no reinforce targets (no friendly tiles at all). They'll be
            # eliminated on the next win-check; the action this turn is a
            # no-op.
            logger.warning(
                "return_3: turn=%s cult=%s has no votes AND no legal actions; "
                "no-op", state.current_turn_number, active_cult,
            )
            return ("noop", -1, None)
        return random.choice(legal_triples)

    tally: Counter[tuple[str, int, Optional[int]]] = Counter()
    for v in votes:
        tally[(v.action_kind, v.source_tile_id, v.target_tile_id)] += 1
    top = max(tally.values())
    candidates = [k for k, c in tally.items() if c == top and k in legal_set]
    if candidates:
        return random.choice(candidates)
    # All top-tallied actions are no-longer-legal; fall back to random
    # legal action so the turn doesn't dead-end.
    if legal_triples:
        return random.choice(legal_triples)
    return ("noop", -1, None)


def _apply_action(
    tiles_by_id: dict[int, Return3Tile],
    kind: str,
    src: int,
    dst: Optional[int],
    cult: str,
) -> str:
    """Mutate ``tiles_by_id`` and return a one-line description for logs."""
    if kind == "noop":
        return f"{cult} had no legal action this turn."
    if kind == "reinforce":
        t = tiles_by_id[src]
        t.army_count += REINFORCE_AMOUNT
        return (
            f"{cult} reinforced {tile_label(src)} "
            f"(+{REINFORCE_AMOUNT} → {t.army_count})."
        )
    if kind == "march":
        assert dst is not None
        s = tiles_by_id[src]
        d = tiles_by_id[dst]
        sent = max(0, s.army_count - 1)
        s.army_count -= sent
        d.army_count += sent
        return (
            f"{cult} marched {sent} from {tile_label(src)} → "
            f"{tile_label(dst)} (now {s.army_count} / {d.army_count})."
        )
    if kind == "attack":
        assert dst is not None
        s = tiles_by_id[src]
        d = tiles_by_id[dst]
        sent = max(0, s.army_count - 1)
        s.army_count -= sent
        defender = d.army_count
        if sent > defender:
            # Attacker wins; tile flips with surviving attackers.
            survivors = sent - defender
            d.controlling_cult = cult
            d.army_count = survivors
            return (
                f"{cult} attacked {tile_label(dst)} from {tile_label(src)} "
                f"({sent} vs {defender}) — captured with {survivors}."
            )
        # Defender wins (incl. ties); defender reduced; tile stays.
        d.army_count = max(0, defender - sent)
        return (
            f"{cult} attacked {tile_label(dst)} from {tile_label(src)} "
            f"({sent} vs {defender}) — repelled, defender now {d.army_count}."
        )
    return f"Unknown action {kind!r} — no-op."


# ---------------------------------------------------------------------------
# Win check
# ---------------------------------------------------------------------------


def _tile_counts(tiles: Iterable[Return3Tile]) -> Counter[str]:
    c: Counter[str] = Counter()
    for t in tiles:
        if t.controlling_cult is not None:
            c[t.controlling_cult] += 1
    return c


def _check_win(
    state: Return3GameState, tiles: list[Return3Tile]
) -> Optional[str]:
    """Returns the winning cult name, or None.

    Called *after* a loop boundary has just been crossed (i.e. the just-
    completed turn was the last of a loop). The dominance streak field on
    ``state`` is updated in place; the caller is responsible for saving.
    """
    counts = _tile_counts(tiles)
    cults_with_tiles = [c for c, n in counts.items() if n > 0]
    if len(cults_with_tiles) == 1:
        return cults_with_tiles[0]  # elimination winner

    if not counts:
        return None
    leader, leader_count = counts.most_common(1)[0]
    if leader_count >= DOMINANCE_TILE_THRESHOLD:
        if state.dominance_leader_cult == leader:
            state.dominance_streak_loops += 1
        else:
            state.dominance_leader_cult = leader
            state.dominance_streak_loops = 1
        if state.dominance_streak_loops >= DOMINANCE_LOOP_STREAK:
            return leader
    else:
        state.dominance_leader_cult = None
        state.dominance_streak_loops = 0
    return None


# ---------------------------------------------------------------------------
# Cult-membership lookup
# ---------------------------------------------------------------------------


async def _user_cult(user_id: int) -> Optional[str]:
    """Return the cult-name the user is in, or None. Uses CultMembership."""
    disc = await DiscordAccount.filter(disc_uuid=str(user_id)).first()
    if disc is None:
        return None
    mem = await CultMembership.filter(discord_account=disc).first()
    if mem is None:
        return None
    cult = await Cult.get(id=mem.cult_id)
    return cult.name


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_grid_emoji(tiles_by_id: dict[int, Return3Tile], visible: set[int]) -> str:
    rows: list[str] = []
    for r in range(GRID_H):
        cells: list[str] = []
        for c in range(GRID_W):
            tid = tile_id(r, c)
            if tid not in visible:
                cells.append(FOG_EMOJI)
                continue
            t = tiles_by_id[tid]
            if t.controlling_cult is None:
                cells.append(NEUTRAL_EMOJI)
            else:
                cells.append(CULT_EMOJI[t.controlling_cult])
        rows.append("".join(cells))
    return "\n".join(rows)


def _render_armies(tiles_by_id: dict[int, Return3Tile], visible: set[int]) -> str:
    """Right-padded pairs in a code block, one row per grid row."""
    rows: list[str] = []
    for r in range(GRID_H):
        cells: list[str] = []
        for c in range(GRID_W):
            tid = tile_id(r, c)
            if tid not in visible:
                cells.append("  ?  ")
                continue
            t = tiles_by_id[tid]
            cells.append(f"{tile_label(tid)}:{t.army_count:<2d}")
        rows.append(" ".join(cells))
    return "\n".join(rows)


async def _render_vote_tallies(state: Return3GameState) -> list[str]:
    votes = await Return3Vote.filter(turn_number=state.current_turn_number).all()
    if not votes:
        return ["_No votes yet._"]
    tally: Counter[tuple[str, int, Optional[int]]] = Counter()
    for v in votes:
        tally[(v.action_kind, v.source_tile_id, v.target_tile_id)] += 1
    out: list[str] = []
    for (kind, src, dst), n in tally.most_common(VOTE_TALLY_TOP_N):
        if kind == "reinforce":
            out.append(f"• Reinforce {tile_label(src)} — **{n}**")
        elif kind == "march":
            out.append(
                f"• March {tile_label(src)} → {tile_label(dst)} — **{n}**"
                if dst is not None
                else f"• March {tile_label(src)} — **{n}**"
            )
        elif kind == "attack":
            out.append(
                f"• Attack {tile_label(src)} → {tile_label(dst)} — **{n}**"
                if dst is not None
                else f"• Attack {tile_label(src)} — **{n}**"
            )
    return out


def _lots_status(state: Return3GameState) -> tuple[int, dict[str, Optional[int]]]:
    drawn: dict[str, Optional[int]] = {}
    for cult, attr in _LOT_FIELDS.items():
        drawn[cult] = getattr(state, attr)
    n = sum(1 for v in drawn.values() if v is not None)
    return n, drawn


async def _render_dashboard_embed(
    state: Return3GameState,
    tiles: list[Return3Tile],
    cult: str,
) -> discord.Embed:
    tiles_by_id = _tile_index(tiles)
    visible = _fog_of_war_visible(tiles, cult) or {CAPITAL_TILE[cult]}

    title = f"{CULT_EMOJI[cult]} {cult} — Return 3"

    if state.phase == "drafting":
        n_drawn, drawn = _lots_status(state)
        lines = [
            f"**Drawing lots** — {n_drawn}/4 cults have drawn.",
        ]
        if state.drafting_deadline:
            ts = int(state.drafting_deadline.timestamp())
            lines.append(f"Drafting deadline: <t:{ts}:R>")
        lots_lines = []
        for c, v in drawn.items():
            mark = "✅" if v is not None else "—"
            shown = f"`{v:>3}`" if v is not None else "`---`"
            lots_lines.append(f"{CULT_EMOJI[c]} `{c}` {mark} {shown}")
        body = "\n".join(lines + [""] + lots_lines)
        embed = discord.Embed(title=title, description=body, color=0x808080)
    elif state.phase == "ended":
        winner = state.winner_cult
        if winner:
            color = 0x2ecc71 if winner == cult else 0xc0392b
            body = f"**Game over — `{winner}` wins.** {CULT_EMOJI[winner]}"
        else:
            color = 0x808080
            body = "**Game over.**"
        embed = discord.Embed(title=title, description=body, color=color)
    else:
        # phase == "active"
        active = _current_cult(state)
        loop_num = state.current_turn_number // 4 + 1
        if active == cult:
            header = f"**Your turn — turn {state.current_turn_number + 1}, loop {loop_num}.**"
        elif active is not None:
            header = (
                f"**{CULT_EMOJI[active]} `{active}` is taking turn "
                f"{state.current_turn_number + 1} (loop {loop_num}).** "
                "Wait for your turn."
            )
        else:
            header = "**Awaiting first turn.**"
        deadline = _turn_deadline(state)
        if deadline is not None:
            ts = int(deadline.timestamp())
            header += f"\nTurn deadline: <t:{ts}:R>"
        embed = discord.Embed(title=title, description=header, color=0x3498db)

    # Map grid + visible armies (shown in all phases for context).
    grid = _render_grid_emoji(tiles_by_id, visible)
    armies = _render_armies(tiles_by_id, visible)
    embed.add_field(name="Map (fog-of-war)", value=grid, inline=False)
    embed.add_field(name="Armies", value=f"```\n{armies}\n```", inline=False)

    # Vote tallies are intel — only the active cult sees its own votes.
    # Showing them on opponent dashboards leaks the planned move and
    # defeats the fog-of-war.
    if state.phase == "active" and _current_cult(state) == cult:
        tallies = await _render_vote_tallies(state)
        embed.add_field(
            name="Current votes",
            value="\n".join(tallies),
            inline=False,
        )

    # Footer reminds players about the alerts opt-in.
    embed.set_footer(text="Tap the 🔔 button to toggle DM alerts for when your cult's turn comes up.")
    return embed


# ---------------------------------------------------------------------------
# View construction (display-only; the persistent registered Views handle
# dispatch — these instances populate option lists for the current state).
# ---------------------------------------------------------------------------


def _placeholder_option(text: str) -> discord.SelectOption:
    # Discord requires ≥1 option on every Select; this is what we show
    # when there's no legal action of a given kind, or the cult is
    # inactive. The value is never read in those paths (the Select is
    # disabled when this is the only option).
    return discord.SelectOption(label=text, value="_none_", default=False)


def _reinforce_options(legal: list[int]) -> list[discord.SelectOption]:
    if not legal:
        return [_placeholder_option("— no legal reinforce —")]
    legal = legal[:_SELECT_MAX_OPTIONS]
    return [
        discord.SelectOption(
            label=f"Reinforce {tile_label(tid)} (+{REINFORCE_AMOUNT})",
            value=str(tid),
        )
        for tid in legal
    ]


def _pair_options(
    legal: list[tuple[int, int]],
    label_fmt: str,
    *,
    tiles_by_id: Optional[dict[int, Return3Tile]] = None,
) -> list[discord.SelectOption]:
    if not legal:
        return [_placeholder_option(f"— no legal {label_fmt.split(' ', 1)[0].lower()} —")]
    # When over the cap, prioritise pairs whose source has more armies
    # (more impactful moves), then by ascending tile id.
    if tiles_by_id is not None and len(legal) > _SELECT_MAX_OPTIONS:
        legal = sorted(
            legal,
            key=lambda p: (-tiles_by_id[p[0]].army_count, p[0], p[1]),
        )[:_SELECT_MAX_OPTIONS]
    else:
        legal = legal[:_SELECT_MAX_OPTIONS]
    out = []
    for src, dst in legal:
        label = label_fmt.format(src=tile_label(src), dst=tile_label(dst))
        out.append(discord.SelectOption(label=label, value=f"{src}-{dst}"))
    return out


def _build_action_view(
    state: Return3GameState,
    tiles: list[Return3Tile],
    cult: str,
) -> discord.ui.View:
    """Display-only view for ``cult``'s dashboard during the active phase
    (or during ended state — components are disabled).
    """
    active = _current_cult(state)
    is_active = (state.phase == "active") and (active == cult)
    tiles_by_id = _tile_index(tiles)
    rein, marches, attacks = _legal_actions(tiles_by_id, cult)

    view = discord.ui.View(timeout=None)

    r_select = _ActionSelect(
        kind="reinforce",
        custom_id="return3:reinforce",
        placeholder=f"Reinforce (+{REINFORCE_AMOUNT} on a friendly tile)",
        options=_reinforce_options(rein),
        disabled=not is_active or not rein,
        row=0,
    )
    m_select = _ActionSelect(
        kind="march",
        custom_id="return3:march",
        placeholder="March (reposition between friendly tiles)",
        options=_pair_options(marches, "March {src} → {dst}", tiles_by_id=tiles_by_id),
        disabled=not is_active or not marches,
        row=1,
    )
    a_select = _ActionSelect(
        kind="attack",
        custom_id="return3:attack",
        placeholder="Attack (send armies − 1, keep 1 garrison)",
        options=_pair_options(attacks, "Attack {src} → {dst}", tiles_by_id=tiles_by_id),
        disabled=not is_active or not attacks,
        row=2,
    )

    view.add_item(r_select)
    view.add_item(m_select)
    view.add_item(a_select)
    view.add_item(_AlertsButton(row=3))
    return view


def _build_draft_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(_DrawLotButton())
    return view


# ---------------------------------------------------------------------------
# Persistent View classes — registered once each via bot.add_view.
# ---------------------------------------------------------------------------


class _ActionSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        kind: str,
        custom_id: str,
        placeholder: str,
        options: list[discord.SelectOption],
        disabled: bool,
        row: int,
    ):
        super().__init__(
            custom_id=custom_id,
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=row,
        )
        self._kind = kind

    async def callback(self, interaction: discord.Interaction):
        # ``self._kind`` may be wrong on the registered/dispatched
        # instance because Discord calls back on the same View object we
        # registered (which was built with a default ``kind``). Resolve
        # from custom_id instead so dispatch is correct regardless of
        # which instance handles the click.
        cid = self.custom_id or ""
        kind = cid.rsplit(":", 1)[-1] if cid.startswith("return3:") else self._kind
        raw = (interaction.data or {}).get("values", ["_none_"])[0]  # type: ignore[union-attr]
        await _handle_action_vote(interaction, kind, raw)


class _AlertsButton(discord.ui.Button):
    def __init__(self, *, row: int = 3):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Toggle turn alerts",
            emoji="🔔",
            custom_id="return3:alerts",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await _handle_alerts_toggle(interaction)


class _DrawLotButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Draw a lot for your cult",
            emoji="🎲",
            custom_id="return3:draw",
        )

    async def callback(self, interaction: discord.Interaction):
        await _handle_draw_lot(interaction)


class Return3ActionView(discord.ui.View):
    """Persistent view for the active phase. Registered in ``bot.py:setup_hook``.

    The instance registered via ``add_view`` is the dispatch target for
    all clicks across all 4 dashboards. Display-time instances (built by
    :func:`_build_action_view`) populate cult-specific options and pass
    through to the same callbacks.
    """

    def __init__(self):
        super().__init__(timeout=None)
        # Default placeholder option lists; replaced at render time.
        self.add_item(_ActionSelect(
            kind="reinforce", custom_id="return3:reinforce",
            placeholder="Reinforce", options=[_placeholder_option("—")],
            disabled=True, row=0,
        ))
        self.add_item(_ActionSelect(
            kind="march", custom_id="return3:march",
            placeholder="March", options=[_placeholder_option("—")],
            disabled=True, row=1,
        ))
        self.add_item(_ActionSelect(
            kind="attack", custom_id="return3:attack",
            placeholder="Attack", options=[_placeholder_option("—")],
            disabled=True, row=2,
        ))
        self.add_item(_AlertsButton(row=3))


class Return3DraftView(discord.ui.View):
    """Persistent view for the drafting phase."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(_DrawLotButton())


# ---------------------------------------------------------------------------
# Interaction handlers
# ---------------------------------------------------------------------------


async def _handle_action_vote(
    interaction: discord.Interaction, kind: str, raw_value: str,
) -> None:
    # 1. Resolve dashboard cult from channel.
    dash_cult = THREAD_TO_CULT.get(interaction.channel_id or -1)
    if dash_cult is None:
        await interaction.response.send_message(
            "Return 3 dashboards only work in their pinned thread.",
            ephemeral=True,
        )
        return

    state = await _load_state()
    if state is None or state.phase != "active":
        await interaction.response.send_message(
            "Return 3 isn't in the active phase right now.", ephemeral=True,
        )
        return

    active_cult = _current_cult(state)
    if active_cult != dash_cult:
        await interaction.response.send_message(
            f"It's `{active_cult}`'s turn — your cult can't vote until "
            "the map comes back around.",
            ephemeral=True,
        )
        return

    user_cult = await _user_cult(interaction.user.id)
    if user_cult != dash_cult:
        await interaction.response.send_message(
            f"Only members of `{dash_cult}` can vote on this turn. "
            "Join the cult via `/return 0` if you'd like to participate.",
            ephemeral=True,
        )
        return

    # 2. Parse value and re-validate legality.
    tiles = await _load_tiles()
    tiles_by_id = _tile_index(tiles)
    rein, marches, attacks = _legal_actions(tiles_by_id, dash_cult)

    parsed: Optional[tuple[str, int, Optional[int]]] = None
    if raw_value == "_none_":
        await interaction.response.send_message(
            "That option isn't an action — the cult has no legal "
            f"{kind} this turn.", ephemeral=True,
        )
        return
    try:
        if kind == "reinforce":
            tid = int(raw_value)
            if tid in rein:
                parsed = ("reinforce", tid, None)
        elif kind in ("march", "attack"):
            src_s, dst_s = raw_value.split("-", 1)
            src, dst = int(src_s), int(dst_s)
            pool = marches if kind == "march" else attacks
            if (src, dst) in pool:
                parsed = (kind, src, dst)
    except (ValueError, KeyError):
        parsed = None

    if parsed is None:
        await interaction.response.send_message(
            "That action is no longer legal — the board state changed. "
            "Pick another option.", ephemeral=True,
        )
        return

    # 3. UPSERT the vote.
    p_kind, p_src, p_dst = parsed
    await Return3Vote.update_or_create(
        turn_number=state.current_turn_number,
        voter_disc_uuid=str(interaction.user.id),
        defaults={
            "action_kind": p_kind,
            "source_tile_id": p_src,
            "target_tile_id": p_dst,
        },
    )

    if p_kind == "reinforce":
        desc = f"Reinforce {tile_label(p_src)}"
    elif p_kind == "march":
        desc = f"March {tile_label(p_src)} → {tile_label(p_dst)}"
    else:
        desc = f"Attack {tile_label(p_src)} → {tile_label(p_dst)}"

    await interaction.response.send_message(
        f"✅ Vote recorded: **{desc}**. You can change your vote before "
        "the turn ends.",
        ephemeral=True,
    )

    # 4. Re-render all 4 dashboards (every cult sees the active tally).
    await _refresh_all_dashboards(interaction.client)


async def _handle_alerts_toggle(interaction: discord.Interaction) -> None:
    uid = str(interaction.user.id)
    existing = await Return3Subscription.filter(disc_uuid=uid).first()
    if existing is not None:
        await existing.delete()
        await interaction.response.send_message(
            "🔕 Turn alerts **off**. You won't be DM'd when your cult's "
            "turn comes up.", ephemeral=True,
        )
        return
    await Return3Subscription.create(disc_uuid=uid)
    await interaction.response.send_message(
        "🔔 Turn alerts **on**. I'll DM you when your cult's turn starts "
        "(or ping you in <#" + str(CurrConfig.LINK_FALLBACK_CHANNEL) + "> if "
        "your DMs are closed). Tap again to turn off.",
        ephemeral=True,
    )


async def _handle_draw_lot(interaction: discord.Interaction) -> None:
    dash_cult = THREAD_TO_CULT.get(interaction.channel_id or -1)
    if dash_cult is None:
        await interaction.response.send_message(
            "Return 3 dashboards only work in their pinned thread.",
            ephemeral=True,
        )
        return

    state = await _load_state()
    if state is None or state.phase != "drafting":
        await interaction.response.send_message(
            "Drafting is closed.", ephemeral=True,
        )
        return

    user_cult = await _user_cult(interaction.user.id)
    if user_cult != dash_cult:
        await interaction.response.send_message(
            f"Only members of `{dash_cult}` can draw their cult's lot.",
            ephemeral=True,
        )
        return

    attr = _LOT_FIELDS[dash_cult]
    if getattr(state, attr) is not None:
        await interaction.response.send_message(
            f"`{dash_cult}` has already drawn: `{getattr(state, attr)}`. "
            "Wait for the other cults.", ephemeral=True,
        )
        return

    roll = random.randint(1, 100)
    setattr(state, attr, roll)
    await state.save(update_fields=[attr])

    await interaction.response.send_message(
        f"🎲 You drew **{roll}** for `{dash_cult}`.", ephemeral=True,
    )

    # If all four are drawn now, advance to active (which also refreshes).
    # Otherwise refresh ALL dashboards — the lots table on every cult's
    # dashboard reflects all four cults' state, not just the clicker's.
    state = await _load_state()  # re-read with the new value
    assert state is not None
    n_drawn, _ = _lots_status(state)
    if n_drawn == 4:
        await _transition_to_active(interaction.client, state)
    else:
        await _refresh_all_dashboards(interaction.client)


# ---------------------------------------------------------------------------
# Dashboard refresh
# ---------------------------------------------------------------------------


async def _resolve_thread(
    bot: discord.Client, thread_id: int
) -> Optional[discord.Thread]:
    ch = bot.get_channel(thread_id)
    if isinstance(ch, discord.Thread):
        return ch
    try:
        ch = await bot.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logger.warning("return_3: thread %s not fetchable: %s", thread_id, e)
        return None
    return ch if isinstance(ch, discord.Thread) else None


async def _build_view_for_phase(
    state: Return3GameState,
    tiles: list[Return3Tile],
    cult: str,
) -> discord.ui.View:
    if state.phase == "drafting":
        return _build_draft_view()
    return _build_action_view(state, tiles, cult)


async def _refresh_dashboard(bot: discord.Client, cult: str) -> None:
    dash = await Return3Dashboard.filter(cult=cult).first()
    if dash is None:
        return
    state = await _load_state()
    tiles = await _load_tiles()
    if state is None or not tiles:
        return
    embed = await _render_dashboard_embed(state, tiles, cult)
    view = await _build_view_for_phase(state, tiles, cult)

    thread = await _resolve_thread(bot, dash.thread_id)
    if thread is None:
        return
    try:
        msg = await thread.fetch_message(dash.message_id)
    except (discord.NotFound, discord.Forbidden):
        # Message gone — repost and re-pin.
        try:
            msg = await thread.send(embed=embed, view=view)
        except discord.HTTPException as e:
            logger.warning("return_3 repost in %s failed: %s", cult, e)
            return
        try:
            await msg.pin(reason="return_3 dashboard repost")
        except discord.HTTPException:
            pass
        dash.message_id = msg.id
        await dash.save(update_fields=["message_id"])
        return
    except discord.HTTPException as e:
        logger.warning("return_3 fetch_message in %s failed: %s", cult, e)
        return
    try:
        await msg.edit(embed=embed, view=view)
    except discord.HTTPException as e:
        logger.warning("return_3 edit in %s failed: %s", cult, e)


async def _refresh_all_dashboards(bot: discord.Client) -> None:
    for cult in ALL_CULTS:
        await _refresh_dashboard(bot, cult)


# ---------------------------------------------------------------------------
# Phase transitions
# ---------------------------------------------------------------------------


def _resolve_turn_order(state: Return3GameState) -> list[str]:
    """Sort cults by lot value DESC, random tiebreak.

    Any cult with a null lot at call time gets a random fallback in [1,100].
    """
    rolls: dict[str, int] = {}
    for cult, attr in _LOT_FIELDS.items():
        v = getattr(state, attr)
        if v is None:
            v = random.randint(1, 100)
            setattr(state, attr, v)
        rolls[cult] = v
    # Random tiebreak via secondary key.
    order = sorted(rolls.items(), key=lambda kv: (-kv[1], random.random()))
    return [cult for cult, _ in order]


async def _transition_to_active(
    bot: discord.Client, state: Return3GameState
) -> None:
    order = _resolve_turn_order(state)
    state.turn_order_csv = ",".join(order)
    state.phase = "active"
    state.turn_started_at = datetime.now(timezone.utc)
    state.current_turn_number = 0
    await state.save()
    logger.info("return_3: drafting → active, order=%s", order)
    await _refresh_all_dashboards(bot)
    await _notify_subscribers(bot, order[0])


async def _notify_subscribers(bot: discord.Client, active_cult: str) -> None:
    """DM every alert-subscribed user whose CultMembership matches the
    newly-active cult; batch DM failures into one ping in
    LINK_FALLBACK_CHANNEL.
    """
    subs = await Return3Subscription.all()
    if not subs:
        return

    sub_ids = [s.disc_uuid for s in subs]
    discs = await DiscordAccount.filter(disc_uuid__in=sub_ids)
    if not discs:
        return
    cult_row = await Cult.filter(name=active_cult).first()
    if cult_row is None:
        logger.warning("return_3 notify: cult %s has no Cult row", active_cult)
        return
    member_account_ids = set(
        await CultMembership.filter(
            discord_account_id__in=[d.id for d in discs],
            cult_id=cult_row.id,
        ).values_list("discord_account_id", flat=True)
    )
    targets = [d for d in discs if d.id in member_account_ids]
    if not targets:
        return

    body = (
        f"⏰ It's `{active_cult}`'s turn in Return 3 — vote in "
        f"<#{CULT_THREADS[active_cult]}>. You have 24 hours."
    )

    failed_uids: list[str] = []
    for d in targets:
        try:
            user = await bot.fetch_user(int(d.disc_uuid))
        except (ValueError, discord.HTTPException) as e:
            logger.warning("return_3 notify: fetch_user(%s) failed: %s", d.disc_uuid, e)
            failed_uids.append(d.disc_uuid)
            continue
        if not await dm_or_log(user, body):
            failed_uids.append(d.disc_uuid)

    if failed_uids:
        ch = bot.get_channel(CurrConfig.LINK_FALLBACK_CHANNEL)
        if ch is None:
            try:
                ch = await bot.fetch_channel(CurrConfig.LINK_FALLBACK_CHANNEL)
            except discord.HTTPException as e:
                logger.warning("return_3 notify: fallback channel fetch failed: %s", e)
                return
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            mentions = " ".join(f"<@{u}>" for u in failed_uids)
            try:
                await ch.send(
                    f"{mentions}\n{body}",
                    allowed_mentions=discord.AllowedMentions(
                        users=[discord.Object(int(u)) for u in failed_uids]
                    ),
                )
            except discord.HTTPException as e:
                logger.warning("return_3 notify: fallback post failed: %s", e)


async def _advance_one_turn(bot: discord.Client, state: Return3GameState) -> None:
    """Resolve the just-expired turn and roll over."""
    tiles = await _load_tiles()
    tiles_by_id = _tile_index(tiles)
    active = _current_cult(state)
    if active is None:
        logger.warning("return_3 advance: no active cult")
        return

    kind, src, dst = await _resolve_turn_vote(state, tiles_by_id, active)
    log_line = _apply_action(tiles_by_id, kind, src, dst, active)
    logger.info("return_3 turn %s: %s", state.current_turn_number, log_line)
    # Persist any mutated tiles. Update_fields keeps the writes tight.
    for t in tiles:
        if t.id in tiles_by_id and tiles_by_id[t.id] is t:
            await t.save(update_fields=["controlling_cult", "army_count"])

    # Clear any votes for the turn we just resolved (votes for future
    # turns are gated by turn_number so this isn't strictly needed, but
    # keeps the row count tidy).
    await Return3Vote.filter(turn_number=state.current_turn_number).delete()

    state.current_turn_number += 1
    state.turn_started_at = datetime.now(timezone.utc)

    new_loop_started = state.current_turn_number % 4 == 0
    if new_loop_started:
        # Passive growth: +LOOP_GROWTH on every owned tile.
        fresh_tiles = await _load_tiles()
        for t in fresh_tiles:
            if t.controlling_cult is not None:
                t.army_count += LOOP_GROWTH
                await t.save(update_fields=["army_count"])

        # Win check (only relevant at loop boundaries because dominance
        # streak counts in loops).
        winner = _check_win(state, fresh_tiles)
        if winner is not None:
            state.phase = "ended"
            state.winner_cult = winner
            state.ended_at = datetime.now(timezone.utc)
            await state.save()
            logger.info("return_3 ended; winner=%s", winner)
            await _refresh_all_dashboards(bot)
            return

    await state.save()
    await _refresh_all_dashboards(bot)

    next_active = _current_cult(state)
    if next_active is not None:
        await _notify_subscribers(bot, next_active)


# ---------------------------------------------------------------------------
# Tick — registered to the shared 60s loop in return_cmd
# ---------------------------------------------------------------------------


@register_startup(WEEK)
async def startup(bot: discord.Client) -> None:
    """Re-render every dashboard on bot start.

    Without this, a restart that happened between (say) a lot-draw and
    the next state change would leave the pinned posts stuck on the
    pre-draw render — Discord doesn't re-edit messages on its own. Cheap
    enough (4 edits) to do unconditionally.
    """
    if await _load_state() is None:
        return
    await _refresh_all_dashboards(bot)


@register_tick(WEEK)
async def tick(bot: discord.Client) -> None:
    state = await _load_state()
    if state is None or state.phase == "ended":
        return
    now = datetime.now(timezone.utc)

    if state.phase == "drafting":
        if state.drafting_deadline is not None and now >= state.drafting_deadline:
            await _transition_to_active(bot, state)
        return

    # phase == active
    if state.turn_started_at is None:
        # Shouldn't happen, but recover gracefully.
        state.turn_started_at = now
        await state.save(update_fields=["turn_started_at"])
        return
    if now - state.turn_started_at < TURN_DURATION:
        return

    await _advance_one_turn(bot, state)


# ---------------------------------------------------------------------------
# /return 3 — read-only status surface
# ---------------------------------------------------------------------------


@register(WEEK, tier=Tier.REGISTERED)
async def handle(ctx: commands.Context) -> None:
    """Ephemeral status reply for /return 3.

    The action UI lives in each cult's pinned thread post; this command
    is just a status panel so anyone can see what's going on at a glance.
    """
    state = await _load_state()
    if state is None:
        await ctx.reply(
            "Return 3 hasn't been started yet.", ephemeral=True,
        )
        return

    tiles = await _load_tiles()
    counts = _tile_counts(tiles)

    lines = [f"**Return 3 — phase: `{state.phase}`**"]
    if state.phase == "drafting":
        n, drawn = _lots_status(state)
        lines.append(f"Drafting lots: {n}/4 cults drawn.")
        for c, v in drawn.items():
            shown = f"`{v}`" if v is not None else "—"
            lines.append(f"  {CULT_EMOJI[c]} `{c}`: {shown}")
        if state.drafting_deadline:
            ts = int(state.drafting_deadline.timestamp())
            lines.append(f"Deadline: <t:{ts}:R>")
    elif state.phase == "active":
        active = _current_cult(state)
        loop = state.current_turn_number // 4 + 1
        lines.append(
            f"Turn {state.current_turn_number + 1} (loop {loop}) — "
            f"active: {CULT_EMOJI.get(active, '')} `{active}`"
            if active else f"Turn {state.current_turn_number + 1}"
        )
        deadline = _turn_deadline(state)
        if deadline:
            lines.append(f"Turn deadline: <t:{int(deadline.timestamp())}:R>")
    else:
        if state.winner_cult:
            lines.append(
                f"Winner: {CULT_EMOJI.get(state.winner_cult, '')} "
                f"`{state.winner_cult}`"
            )

    lines.append("\n**Tile counts:**")
    for c in ALL_CULTS:
        lines.append(f"  {CULT_EMOJI[c]} `{c}`: {counts.get(c, 0)} / {TILE_COUNT}")
    lines.append(f"  ⬜ neutral: {TILE_COUNT - sum(counts.values())}")

    if state.dominance_leader_cult and state.dominance_streak_loops > 0:
        lines.append(
            f"\nDominance streak: `{state.dominance_leader_cult}` × "
            f"{state.dominance_streak_loops} loop(s) "
            f"(needs {DOMINANCE_LOOP_STREAK} to win)."
        )

    await ctx.reply("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# ~manage_return 3 — operator commands
# ---------------------------------------------------------------------------


async def _wipe_state() -> None:
    """DELETE every Return 3 row. Used by ``start --force``."""
    await Return3Vote.all().delete()
    await Return3Subscription.all().delete()
    await Return3Dashboard.all().delete()
    await Return3Tile.all().delete()
    await Return3GameState.all().delete()


async def _seed_tiles() -> None:
    """Create the 25 tile rows. Capitals own their cult with starting
    armies; the rest are neutral with 0.
    """
    rows: list[Return3Tile] = []
    for tid in range(TILE_COUNT):
        cult: Optional[str] = None
        armies = 0
        for c, cap in CAPITAL_TILE.items():
            if cap == tid:
                cult = c
                armies = STARTING_CAPITAL_ARMIES
                break
        rows.append(Return3Tile(id=tid, controlling_cult=cult, army_count=armies))
    await Return3Tile.bulk_create(rows)


@register_manage(
    WEEK, "start", tier=Tier.OPERATOR,
    help="Bootstrap Return 3: seed tiles, post & pin dashboards in each "
         "cult thread, post the one-time announcement. Pass `--force` to "
         "wipe an existing game first.",
    usage="[--force]",
)
async def _manage_start(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    force = "--force" in args

    existing = await _load_state()
    if existing is not None and not force:
        await send_feedback(
            ctx,
            "Return 3 is already started (phase: "
            f"`{existing.phase}`). Pass `--force` to wipe and restart.",
            persist=persist,
        )
        return
    if existing is not None and force:
        logger.warning("return_3: --force wipe by %s (%s)", ctx.author, ctx.author.id)
        await _wipe_state()

    # Seed.
    now = datetime.now(timezone.utc)
    state = await Return3GameState.create(
        id=1,
        phase="drafting",
        started_at=now,
        drafting_deadline=now + DRAFTING_DURATION,
    )
    await _seed_tiles()

    # Post per-thread: announcement + dashboard + pin dashboard.
    posted: list[str] = []
    failed: list[str] = []
    for cult in ALL_CULTS:
        thread_id = CULT_THREADS[cult]
        thread = await _resolve_thread(ctx.bot, thread_id)
        if thread is None:
            failed.append(cult)
            continue
        try:
            await thread.send(
                _ANNOUNCEMENT_BODY,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            tiles = await _load_tiles()
            embed = await _render_dashboard_embed(state, tiles, cult)
            view = _build_draft_view()
            msg = await thread.send(embed=embed, view=view)
            try:
                await msg.pin(reason="return_3 dashboard")
            except discord.HTTPException as e:
                logger.warning("return_3 start: pin in %s failed: %s", cult, e)
            await Return3Dashboard.create(
                cult=cult, thread_id=thread_id, message_id=msg.id,
            )
            posted.append(cult)
        except discord.HTTPException as e:
            logger.warning("return_3 start: post in %s failed: %s", cult, e)
            failed.append(cult)

    lines = [
        f"Return 3 seeded. Drafting until <t:{int(state.drafting_deadline.timestamp())}:R>.",
        f"Posted dashboards: {', '.join(posted) or '(none)'}",
    ]
    if failed:
        lines.append(f"⚠️ Failed in: {', '.join(failed)}")
    await send_feedback(ctx, "\n".join(lines), persist=persist)


@register_manage(
    WEEK, "status", tier=Tier.STAFF,
    help="Show phase, current turn, tile counts, dominance streak, subscriber count.",
)
async def _manage_status(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    state = await _load_state()
    if state is None:
        await send_feedback(ctx, "Return 3 isn't started.", persist=persist)
        return
    tiles = await _load_tiles()
    counts = _tile_counts(tiles)
    sub_count = await Return3Subscription.all().count()

    lines = [
        f"**Return 3 — phase: `{state.phase}`**",
        f"Started: <t:{int(state.started_at.timestamp())}:f>",
    ]
    if state.phase == "drafting":
        n, drawn = _lots_status(state)
        lines.append(f"Lots drawn: {n}/4 — {drawn}")
        if state.drafting_deadline:
            lines.append(f"Drafting deadline: <t:{int(state.drafting_deadline.timestamp())}:R>")
    elif state.phase == "active":
        active = _current_cult(state)
        lines.append(
            f"Turn {state.current_turn_number + 1} (loop "
            f"{state.current_turn_number // 4 + 1}) — active: `{active}`"
        )
        if state.turn_order_csv:
            lines.append(f"Order: {state.turn_order_csv}")
        deadline = _turn_deadline(state)
        if deadline:
            lines.append(f"Turn deadline: <t:{int(deadline.timestamp())}:R>")
        lines.append(
            f"Dominance: leader=`{state.dominance_leader_cult}` "
            f"streak={state.dominance_streak_loops}/{DOMINANCE_LOOP_STREAK}"
        )
    else:
        lines.append(f"Winner: `{state.winner_cult}`")

    lines.append("Tile counts: " + ", ".join(
        f"`{c}`={counts.get(c, 0)}" for c in ALL_CULTS
    ) + f", neutral={TILE_COUNT - sum(counts.values())}")
    lines.append(f"Alert subscribers: {sub_count}")
    await send_feedback(ctx, "\n".join(lines), persist=persist)


@register_manage(
    WEEK, "forceTick", tier=Tier.OPERATOR,
    help="Pretend the current deadline (drafting or turn) just passed; run "
         "one tick step. For testing or unsticking a stalled game.",
)
async def _manage_force_tick(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    state = await _load_state()
    if state is None:
        await send_feedback(ctx, "Return 3 isn't started.", persist=persist)
        return
    if state.phase == "ended":
        await send_feedback(ctx, "Return 3 has ended.", persist=persist)
        return

    # Backdate so the tick treats the deadline as expired.
    if state.phase == "drafting":
        state.drafting_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await state.save(update_fields=["drafting_deadline"])
    elif state.phase == "active":
        state.turn_started_at = datetime.now(timezone.utc) - TURN_DURATION - timedelta(seconds=1)
        await state.save(update_fields=["turn_started_at"])

    await tick(ctx.bot)
    await send_feedback(ctx, "Tick run. Check the dashboards.", persist=persist)


@register_manage(
    WEEK, "setTile", tier=Tier.OPERATOR,
    help="Emergency state surgery: set a tile's controller and armies. "
         "Pass `none` (literal) for the cult to clear it.",
    usage="<label A1..E5> <cult|none> <armies>",
)
async def _manage_set_tile(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if len(args) != 3:
        await send_feedback(
            ctx, "Usage: `~manage_return 3 setTile <label> <cult|none> <armies>`",
            persist=persist,
        )
        return
    label, cult_arg, armies_arg = args
    tid = label_to_tile(label)
    if tid is None:
        await send_feedback(ctx, f"`{label}` isn't a valid tile (A1..E5).", persist=persist)
        return
    try:
        armies = int(armies_arg)
    except ValueError:
        await send_feedback(ctx, f"`{armies_arg}` isn't an int.", persist=persist)
        return
    if armies < 0:
        await send_feedback(ctx, "Armies must be ≥ 0.", persist=persist)
        return

    if cult_arg.lower() == "none":
        cult_val: Optional[str] = None
    elif cult_arg in CULT_THREADS:
        cult_val = cult_arg
    else:
        await send_feedback(
            ctx, f"`{cult_arg}` isn't a cult ({', '.join(ALL_CULTS)} or `none`).",
            persist=persist,
        )
        return

    tile = await Return3Tile.filter(id=tid).first()
    if tile is None:
        await send_feedback(ctx, "Return 3 hasn't seeded tiles.", persist=persist)
        return
    tile.controlling_cult = cult_val
    tile.army_count = armies
    await tile.save(update_fields=["controlling_cult", "army_count"])
    await _refresh_all_dashboards(ctx.bot)
    await send_feedback(
        ctx,
        f"Set tile {label} → cult={cult_val} armies={armies}.",
        persist=persist,
    )


@register_manage(
    WEEK, "forceLot", tier=Tier.OPERATOR,
    help="Drafting-phase only: manually set a cult's lot value (1-100). "
         "Use this to break a stalled draft without waiting 6h.",
    usage="<cult> <1-100>",
)
async def _manage_force_lot(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    if len(args) != 2:
        await send_feedback(
            ctx, "Usage: `~manage_return 3 forceLot <cult> <1-100>`",
            persist=persist,
        )
        return
    cult, val_arg = args
    if cult not in CULT_THREADS:
        await send_feedback(ctx, f"`{cult}` isn't a cult.", persist=persist)
        return
    try:
        val = int(val_arg)
    except ValueError:
        await send_feedback(ctx, f"`{val_arg}` isn't an int.", persist=persist)
        return
    if not 1 <= val <= 100:
        await send_feedback(ctx, "Value must be 1..100.", persist=persist)
        return

    state = await _load_state()
    if state is None or state.phase != "drafting":
        await send_feedback(ctx, "Not in drafting phase.", persist=persist)
        return
    attr = _LOT_FIELDS[cult]
    setattr(state, attr, val)
    await state.save(update_fields=[attr])

    n_drawn, _ = _lots_status(state)
    if n_drawn == 4:
        await _transition_to_active(ctx.bot, state)
        await send_feedback(
            ctx, f"Set `{cult}`={val}. All four drawn — game is active.",
            persist=persist,
        )
    else:
        await _refresh_all_dashboards(ctx.bot)
        await send_feedback(
            ctx, f"Set `{cult}`={val}. {n_drawn}/4 drawn.", persist=persist,
        )


@register_manage(
    WEEK, "end", tier=Tier.OPERATOR,
    help="Force-end the game. Pass a cult name to declare a winner, or "
         "leave blank for no winner.",
    usage="[winning-cult]",
)
async def _manage_end(ctx: commands.Context, args: list[str]) -> None:
    persist = is_persist_context(ctx)
    state = await _load_state()
    if state is None:
        await send_feedback(ctx, "Return 3 isn't started.", persist=persist)
        return
    winner: Optional[str] = None
    if args:
        if args[0] in CULT_THREADS:
            winner = args[0]
        else:
            await send_feedback(
                ctx, f"`{args[0]}` isn't a cult — pass one of {ALL_CULTS}.",
                persist=persist,
            )
            return
    state.phase = "ended"
    state.winner_cult = winner
    state.ended_at = datetime.now(timezone.utc)
    await state.save()
    await _refresh_all_dashboards(ctx.bot)
    await send_feedback(
        ctx,
        f"Return 3 ended. Winner: {winner or '(none declared)'}.",
        persist=persist,
    )

"""Random text resolver for /return event templates.

Picks (or accepts) a template from ``resources/main.yaml`` and substitutes
``%name%`` / ``%name:subtype%`` placeholders with random picks from the
sibling resource files. Substitutions can themselves contain
placeholders; the resolver iterates to a fixed point (depth cap 5).

Callers pass a pre-sized ``players`` list; each ``%player%`` occurrence
pops from the head. Apartment owners are fetched from the DB lazily,
only when the template (or a possible expansion of it) references them.
"""

from __future__ import annotations

import functools
import random
import re
from pathlib import Path
from typing import Any, Callable

import yaml

_RES_DIR = Path(__file__).parent.parent / "resources"
_TOKEN_RE = re.compile(r"%([a-z_]+)(?::([a-z_]+))?%")

_BARE_MERCHANT_KEYS = frozenset({"blacksmith", "item_identifier", "powder_master"})


@functools.lru_cache(maxsize=None)
def _load(name: str) -> Any:
    return yaml.safe_load((_RES_DIR / name).read_text(encoding="utf-8"))


def _fmt_coord(entry: dict) -> str:
    return f"({entry['x']}, {entry['y']}, {entry['z']})"


def _lowercase_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


def _merchant(subtype: str | None) -> str:
    data = _load("merchants.yaml")
    if subtype is None:
        key = random.choice(list(data.keys()))
    else:
        key = subtype if subtype in data else f"{subtype}_merchant"
    coord = _fmt_coord(random.choice(data[key]))
    if key in _BARE_MERCHANT_KEYS:
        return f"the {key.replace('_', ' ')} at {coord}"
    friendly = key.removesuffix("_merchant").replace("_", " ")
    return f"the {friendly} merchant at {coord}"


def _profspot(subtype: str | None) -> str:
    data = _load("profspots.yaml")
    key = subtype if subtype is not None else random.choice(list(data.keys()))
    return f"the {key} table at {_fmt_coord(random.choice(data[key]))}"


def _mount() -> str:
    data = _load("mount.yaml")
    noun = random.choice(list(data.keys()))
    modifier = random.choice(data[noun])
    return f"{_article_for(modifier)} {modifier} {noun}"


def _article_for(word: str) -> str:
    return "an" if word and word[0].lower() in "aeiou" else "a"


def _pack_adjective() -> str:
    adj = random.choice(_load("pack_adjective.yaml"))
    return f"{_article_for(adj)} {adj}"


def _colour_or_food() -> str:
    if random.random() < 0.5:
        return random.choice(_load("colours.yaml")).lower()
    return random.choice(_load("foods.yaml"))


def _pop_player(ctx: dict) -> str:
    pool = ctx["players"]
    if not pool:
        raise ValueError("resolver: %player% pool exhausted (caller passed too few players)")
    return pool.pop(0)


def _pick_apartment_member(ctx: dict) -> str:
    pool = ctx.get("apartment_members")
    if pool is None:
        raise RuntimeError("resolver: apartment_members not pre-fetched — internal bug")
    if not pool:
        raise ValueError("resolver: no apartments have owners")
    return random.choice(pool)


_HANDLERS: dict[str, Callable[[str | None, dict], str]] = {
    "player": lambda sub, ctx: _pop_player(ctx),
    "apartment_member": lambda sub, ctx: _pick_apartment_member(ctx),
    "adjective": lambda sub, ctx: random.choice(_load("adjectives.yaml")),
    "boss": lambda sub, ctx: random.choice(_load("bosses.yaml")),
    "cave": lambda sub, ctx: random.choice(_load("caves.yaml")),
    "colour": lambda sub, ctx: random.choice(_load("colours.yaml")).lower(),
    "colour_or_food": lambda sub, ctx: _colour_or_food(),
    "cult": lambda sub, ctx: random.choice(_load("cults.yaml")),
    "dungeon": lambda sub, ctx: random.choice(_load("dungeon_raid.yaml")["dungeons"]),
    "raid": lambda sub, ctx: random.choice(_load("dungeon_raid.yaml")["raids"]),
    "food": lambda sub, ctx: random.choice(_load("foods.yaml")),
    "general_action": lambda sub, ctx: _lowercase_first(random.choice(_load("general_actions.yaml"))),
    # Suffix " (gisland)" so readers know the location is on the Guild Island —
    # otherwise "The Music Stage" et al. read as unfamiliar Wynncraft place names.
    "guild_island": lambda sub, ctx: f"{random.choice(_load('guild_island_places.yaml'))} (gisland)",
    "id": lambda sub, ctx: random.choice(_load("ids.yaml")),
    "merchant": lambda sub, ctx: _merchant(sub),
    "pack_adjective": lambda sub, ctx: _pack_adjective(),
    "profspot": lambda sub, ctx: _profspot(sub),
    "territory": lambda sub, ctx: random.choice(_load("territories.yaml")),
    "world_event": lambda sub, ctx: random.choice(_load("world_events.yaml")),
    "worthless_item": lambda sub, ctx: random.choice(_load("worthless.yaml")),
    "mount": lambda sub, ctx: _mount(),
}


def _dispatch(name: str, subtype: str | None, ctx: dict) -> str:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"resolver: unknown placeholder %{name}%")
    return handler(subtype, ctx)


async def _fetch_apartment_members() -> list[str]:
    from orm import Apartment
    rows = await Apartment.filter(owner_mc_username__isnull=False).all()
    return [r.owner_mc_username for r in rows]


def _finalise(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _may_need_apartments(template: str) -> bool:
    # %apartment_member% may appear directly, or inside a %guild_island%
    # expansion (guild_island_places.yaml is the only known source).
    return "%apartment_member%" in template or "%guild_island%" in template


async def resolve(
    template: str | None = None,
    *,
    players: list[str],
    max_depth: int = 5,
) -> str:
    """Substitute %placeholders% in ``template`` (or a random main.yaml pick)
    and return a finalised sentence.

    ``players`` is popped from the head for each %player% occurrence — pass
    enough for the template you're resolving (oversize when in doubt;
    ValueError raised on exhaustion).
    """
    if template is None:
        template = random.choice(_load("main.yaml"))

    ctx: dict[str, Any] = {"players": list(players)}
    if _may_need_apartments(template):
        ctx["apartment_members"] = await _fetch_apartment_members()

    text = template
    for _ in range(max_depth):
        new = _TOKEN_RE.sub(lambda m: _dispatch(m.group(1), m.group(2), ctx), text)
        if new == text:
            break
        text = new
    else:
        raise RuntimeError(f"resolver: max_depth={max_depth} exceeded on {template!r}")

    return _finalise(text)

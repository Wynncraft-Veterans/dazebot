"""Tests for ``cogs.events.returns.lib.resolver``.

No live Tortoise connection: every test monkeypatches
``_fetch_apartment_members`` to return a fixed list. The DB fetch itself
is a two-liner that just runs a filter query; the resolver logic is
what's under test here.
"""

from __future__ import annotations

import random
import re

import pytest

from cogs.events.returns.lib import resolver


@pytest.fixture(autouse=True)
def stub_apartment_fetch(monkeypatch):
    """Every test uses the same monkeypatched apartment pool. Override in
    individual tests if you want empty-pool behaviour.
    """
    async def _stub() -> list[str]:
        return ["ApartmentOwnerA", "ApartmentOwnerB", "ApartmentOwnerC"]

    monkeypatch.setattr(resolver, "_fetch_apartment_members", _stub)


# ---------------------------------------------------------------------------
# Parsing sanity — every loader
# ---------------------------------------------------------------------------


def test_loads_every_resource_file():
    """Every YAML resource parses and produces non-empty output. Catches
    accidental file deletions or a malformed edit.
    """
    lists = [
        "adjectives.yaml", "bosses.yaml", "caves.yaml", "colours.yaml",
        "cults.yaml", "foods.yaml", "general_actions.yaml",
        "guild_island_places.yaml", "ids.yaml", "main.yaml",
        "pack_adjective.yaml", "territories.yaml", "world_events.yaml",
        "worthless.yaml",
    ]
    for name in lists:
        data = resolver._load(name)
        assert isinstance(data, list) and len(data) > 0, name

    dicts = ["dungeon_raid.yaml", "merchants.yaml", "mount.yaml", "profspots.yaml"]
    for name in dicts:
        data = resolver._load(name)
        assert isinstance(data, dict) and len(data) > 0, name


def test_merchants_has_bare_and_suffixed_keys():
    """Bare merchant keys (blacksmith, etc.) live alongside the
    ``_merchant``-suffixed ones. Both formatting paths need coverage."""
    data = resolver._load("merchants.yaml")
    assert "blacksmith" in data
    assert "armour_merchant" in data


def test_mount_sections_are_the_three_expected():
    assert set(resolver._load("mount.yaml").keys()) == {"Horse", "Wyvern", "Adasaur"}


def test_dungeon_raid_has_both_keys():
    d = resolver._load("dungeon_raid.yaml")
    assert set(d.keys()) == {"dungeons", "raids"}


# ---------------------------------------------------------------------------
# Full-resolution sweep — every main.yaml template
# ---------------------------------------------------------------------------


async def test_every_main_template_resolves_fully():
    """For each template, resolve produces text with no unresolved
    tokens, ends with a period, and doesn't leak stray braces."""
    for i, template in enumerate(resolver._load("main.yaml")):
        random.seed(i)
        result = await resolver.resolve(
            template,
            players=["Alice", "Bob", "Charlie", "Dave", "Eve"] * 5,
        )
        assert "%" not in result, f"template {i} left %tokens: {result!r}"
        assert "{" not in result, f"template {i} left {{tokens: {result!r}}}"
        assert result.endswith("."), f"template {i} missing period: {result!r}"


async def test_none_template_picks_from_main():
    """Passing ``template=None`` selects a random main.yaml entry."""
    random.seed(0)
    result = await resolver.resolve(
        players=["Alice", "Bob", "Charlie", "Dave", "Eve"] * 5,
    )
    assert "%" not in result
    assert result.endswith(".")


# ---------------------------------------------------------------------------
# Explicit case coverage — one per non-trivial handler
# ---------------------------------------------------------------------------


async def test_merchant_bare_key_drops_merchant_suffix():
    """``%merchant:blacksmith%`` reads "the blacksmith at (X, Y, Z)" — not
    "the blacksmith merchant at ...". Bare keys skip the suffix."""
    result = await resolver.resolve("%merchant:blacksmith%", players=[])
    assert re.match(r"^the blacksmith at \(-?\d+, -?\d+, -?\d+\)\.$", result), result


async def test_merchant_suffixed_key_keeps_merchant_word():
    """``%merchant:armour%`` reads "the armour merchant at (X, Y, Z)"."""
    result = await resolver.resolve("%merchant:armour%", players=[])
    assert re.match(r"^the armour merchant at \(-?\d+, -?\d+, -?\d+\)\.$", result), result


async def test_bare_merchant_no_subtype_picks_any_type():
    """``%merchant%`` (no subtype) picks a random top-level type."""
    random.seed(0)
    result = await resolver.resolve("%merchant%", players=[])
    assert re.search(r"\(-?\d+, -?\d+, -?\d+\)\.$", result), result
    assert result.startswith("the "), result


async def test_world_event_is_name_only_no_coords():
    """``%world_event%`` produces a readable name — no coord tuple."""
    result = await resolver.resolve("%world_event%", players=[])
    assert "(" not in result
    assert ")" not in result


async def test_mount_includes_article_and_agrees_with_modifier():
    """``%mount%`` produces "(a|an) <Modifier> (Horse|Wyvern|Adasaur)"
    with the article chosen by whether the modifier starts with a vowel
    (so we get "an Ash Horse" not "a Ash Horse")."""
    for i in range(50):
        random.seed(i)
        result = await resolver.resolve("%mount%", players=[])
        match = re.match(r"^(an?) (\S+) (Horse|Wyvern|Adasaur)\.$", result)
        assert match, f"iter {i}: bad shape {result!r}"
        article, modifier, _noun = match.groups()
        starts_vowel = modifier[0].lower() in "aeiou"
        expected = "an" if starts_vowel else "a"
        assert article == expected, f"iter {i}: got {article!r} for modifier {modifier!r} in {result!r}"


async def test_colour_is_lowercased():
    result = await resolver.resolve("wearing %colour%", players=[])
    match = re.match(r"^wearing (\S+)\.$", result)
    assert match, result
    assert match.group(1).islower(), result


async def test_general_action_first_letter_lowercased():
    """The file stores gerunds Title-Cased for readability; substituted
    values must lowercase the first letter so mid-sentence usage reads
    naturally (``player sneaking around`` not ``player Sneaking around``).
    """
    for i in range(20):
        random.seed(i)
        result = await resolver.resolve("Alice %general_action%", players=[])
        after_alice = result[len("Alice "):]
        # After trimming leading "Alice ", first char should be lowercase
        # (or a special char like `?` for the ?Spinning/?Staring entries).
        first = after_alice[0]
        assert first.islower() or first == "?", (
            f"iter {i}: expected lowercase or `?` first, got {result!r}"
        )


async def test_cave_resolves_to_a_named_cave_no_coords():
    """Caves resolve to just the wiki-sourced name — no coord tuple."""
    result = await resolver.resolve("%cave%", players=[])
    assert "(" not in result and ")" not in result, result
    # Every named cave is one of the entries in caves.yaml
    assert result.rstrip(".") in resolver._load("caves.yaml"), result


async def test_guild_island_appends_gisland_suffix():
    """Every %guild_island% substitution ends with ' (gisland)' so the
    reader knows the location is on the Guild Island."""
    result = await resolver.resolve("%guild_island%", players=[])
    assert result.endswith(" (gisland).") or result.endswith(" (gisland)"), result


async def test_food_returns_a_food():
    result = await resolver.resolve("%food%", players=[])
    assert result.rstrip(".") in resolver._load("foods.yaml"), result


async def test_colour_or_food_returns_one_of_either(monkeypatch):
    """50/50 pick between colours and foods. Force both paths and verify
    the results come from the corresponding pool."""
    colours = [c.lower() for c in resolver._load("colours.yaml")]
    foods = resolver._load("foods.yaml")

    monkeypatch.setattr(random, "random", lambda: 0.0)
    result = await resolver.resolve("%colour_or_food%", players=[])
    assert result.rstrip(".") in colours, f"expected colour, got {result!r}"

    monkeypatch.setattr(random, "random", lambda: 0.9)
    result = await resolver.resolve("%colour_or_food%", players=[])
    assert result.rstrip(".") in foods, f"expected food, got {result!r}"


async def test_pack_adjective_includes_article_and_agrees():
    """``%pack_adjective%`` produces "an outrageous", "a moody", etc."""
    for i in range(50):
        random.seed(i)
        result = await resolver.resolve("%pack_adjective%", players=[])
        match = re.match(r"^(an?) (.+)\.$", result)
        assert match, result
        article, word = match.groups()
        expected = "an" if word[0].lower() in "aeiou" else "a"
        assert article == expected, f"iter {i}: {result!r}"


async def test_profspot_bare_and_keyed():
    """Both ``%profspot%`` and ``%profspot:TYPE%`` produce
    "the TYPE table at (X, Y, Z)"."""
    keyed = await resolver.resolve("%profspot:jeweling%", players=[])
    assert re.match(r"^the jeweling table at \(-?\d+, -?\d+, -?\d+\)\.$", keyed), keyed
    bare = await resolver.resolve("%profspot%", players=[])
    assert re.match(r"^the \w+ table at \(-?\d+, -?\d+, -?\d+\)\.$", bare), bare


# ---------------------------------------------------------------------------
# Recursion & nested substitution
# ---------------------------------------------------------------------------


async def test_general_action_recurses_into_nested_placeholders():
    """``general_actions.yaml`` contains entries like "Riding a %mount%"
    and "Conducting %cult% activities" — the resolver must recurse to
    substitute those too."""
    for i in range(50):
        random.seed(i)
        result = await resolver.resolve("Alice %general_action%", players=[])
        assert "%" not in result, f"iter {i}: unresolved token in {result!r}"


async def test_guild_island_recurses_into_apartment_member():
    """``guild_island_places.yaml`` has "%apartment_member%'s Apartment"
    — must recurse."""
    for i in range(50):
        random.seed(i)
        result = await resolver.resolve("Alice visits %guild_island%", players=[])
        assert "%" not in result, f"iter {i}: unresolved token in {result!r}"


async def test_max_depth_raises_on_pathological_template(monkeypatch):
    """Synthetic unbounded growth: each iteration produces a longer
    string that still contains the token. Fixed-point detection can't
    save us; max_depth cap must trip."""
    monkeypatch.setitem(
        resolver._HANDLERS, "adjective",
        lambda sub, ctx: "GROW %adjective%",
    )
    with pytest.raises(RuntimeError, match="max_depth"):
        await resolver.resolve("%adjective%", players=[], max_depth=3)


# ---------------------------------------------------------------------------
# Player pool semantics
# ---------------------------------------------------------------------------


async def test_player_pops_in_order():
    """First %player% → head of pool, second → next."""
    result = await resolver.resolve(
        "%player% then %player%",
        players=["First", "Second", "Third"],
    )
    assert result == "First then Second."


async def test_player_exhaustion_raises():
    with pytest.raises(ValueError, match=r"%player%"):
        await resolver.resolve(
            "%player% and %player%",
            players=["OnlyOne"],
        )


async def test_caller_players_list_not_mutated():
    """Resolver copies the players list — caller's list is untouched
    even after multiple pops."""
    caller_list = ["Alice", "Bob"]
    await resolver.resolve("%player% and %player%", players=caller_list)
    assert caller_list == ["Alice", "Bob"]


# ---------------------------------------------------------------------------
# Finalise pass
# ---------------------------------------------------------------------------


async def test_finalise_appends_period():
    result = await resolver.resolve("Alice does something", players=[])
    assert result == "Alice does something."


async def test_finalise_preserves_existing_terminal_punct():
    result = await resolver.resolve("Alice does something!", players=[])
    assert result == "Alice does something!"


async def test_finalise_collapses_whitespace():
    result = await resolver.resolve("Alice  does   something", players=[])
    assert result == "Alice does something."


async def test_leading_question_mark_preserved():
    """``?Spinning`` / ``?Staring`` are literal outputs (they reference
    the dyno ``?`` prefix). The finalise pass must not strip them."""
    result = await resolver.resolve("Alice ?spinning around", players=[])
    assert result == "Alice ?spinning around."


# ---------------------------------------------------------------------------
# Loader cache
# ---------------------------------------------------------------------------


def test_loader_returns_cached_identity():
    """``_load`` is ``lru_cache``-d — repeated calls return the same
    object, not a fresh parse each time."""
    a = resolver._load("caves.yaml")
    b = resolver._load("caves.yaml")
    assert a is b


# ---------------------------------------------------------------------------
# Unknown placeholder
# ---------------------------------------------------------------------------


async def test_unknown_placeholder_raises():
    with pytest.raises(ValueError, match=r"unknown placeholder"):
        await resolver.resolve("%does_not_exist%", players=[])


# ---------------------------------------------------------------------------
# Apartment prefetch is skipped when template doesn't need it
# ---------------------------------------------------------------------------


async def test_apartment_fetch_skipped_when_no_reference(monkeypatch):
    """If the template can't possibly produce %apartment_member%, we
    don't hit the DB. Verifies the ``_may_need_apartments`` guard."""
    called = False

    async def _spy() -> list[str]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(resolver, "_fetch_apartment_members", _spy)

    await resolver.resolve("%boss%", players=[])
    assert called is False


async def test_apartment_fetch_triggered_by_guild_island(monkeypatch):
    """``%guild_island%`` may expand to ``%apartment_member%'s Apartment``
    — so the guard must pre-fetch even when the token isn't literally
    present in the top-level template."""
    called = False

    async def _spy() -> list[str]:
        nonlocal called
        called = True
        return ["Owner"]

    monkeypatch.setattr(resolver, "_fetch_apartment_members", _spy)

    await resolver.resolve("Alice visits %guild_island%", players=[])
    assert called is True

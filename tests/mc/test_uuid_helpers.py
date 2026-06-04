"""Tests for ``_to_dashed_uuid`` and ``_looks_like_uuid``.

These two gate the name-vs-uuid branch in ``get_mc_uuid``. A regression in
``_looks_like_uuid`` sends UUIDs to the name endpoints (which 404), and a
regression in ``_to_dashed_uuid`` writes badly-formed UUIDs into the
``MojangNameCache`` row.
"""

from __future__ import annotations

import pytest

from lib.mc.mojang import _looks_like_uuid, _to_dashed_uuid


# ---------------------------------------------------------------------------
# _to_dashed_uuid
# ---------------------------------------------------------------------------


def test_to_dashed_uuid_undashed_canonicalized():
    assert (
        _to_dashed_uuid("aabbccdd00112233445566778899aabb")
        == "aabbccdd-0011-2233-4455-66778899aabb"
    )


def test_to_dashed_uuid_uppercase_undashed_lowercased():
    assert (
        _to_dashed_uuid("AABBCCDD00112233445566778899AABB")
        == "aabbccdd-0011-2233-4455-66778899aabb"
    )


def test_to_dashed_uuid_already_dashed_passthrough():
    canonical = "aabbccdd-0011-2233-4455-66778899aabb"
    assert _to_dashed_uuid(canonical) == canonical


def test_to_dashed_uuid_dashed_uppercase_lowercased():
    assert (
        _to_dashed_uuid("AABBCCDD-0011-2233-4455-66778899AABB")
        == "aabbccdd-0011-2233-4455-66778899aabb"
    )


@pytest.mark.parametrize("bad", ["", None])
def test_to_dashed_uuid_empty_or_none_returns_none(bad):
    assert _to_dashed_uuid(bad) is None


def test_to_dashed_uuid_too_short_returns_none():
    assert _to_dashed_uuid("aabbccdd") is None


def test_to_dashed_uuid_too_long_returns_none():
    assert _to_dashed_uuid("a" * 33) is None


def test_to_dashed_uuid_non_hex_returns_none():
    # 32 chars but contains 'z' — not hex.
    assert _to_dashed_uuid("aabbccdd00112233445566778899aabZ") is None


# ---------------------------------------------------------------------------
# _looks_like_uuid
# ---------------------------------------------------------------------------


def test_looks_like_uuid_true_for_undashed_32_hex():
    assert _looks_like_uuid("aabbccdd00112233445566778899aabb") is True


def test_looks_like_uuid_true_for_dashed_36_hex():
    assert _looks_like_uuid("aabbccdd-0011-2233-4455-66778899aabb") is True


def test_looks_like_uuid_true_for_uppercase_undashed():
    assert _looks_like_uuid("AABBCCDD00112233445566778899AABB") is True


def test_looks_like_uuid_false_for_short_string():
    assert _looks_like_uuid("aabbccdd") is False


def test_looks_like_uuid_false_for_normal_username():
    # Typical Wynncraft username — short, alpha, not hex-shaped.
    assert _looks_like_uuid("Wenweia") is False


def test_looks_like_uuid_false_for_non_hex_32_chars():
    # 32 chars but with non-hex 'g'.
    assert _looks_like_uuid("aabbccdd00112233445566778899aabg") is False

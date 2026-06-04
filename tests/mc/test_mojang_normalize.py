"""Tests for the Mojang UUID normalization helper.

``_normalize_uuid`` is the canonicalization gate ahead of every Mojang
endpoint (which expects dashless lowercase). Trivial code but easy to
regress when refactoring the surrounding lookup chain.
"""

from __future__ import annotations

from lib.mc.mojang import _normalize_uuid


def test_strips_dashes():
    assert _normalize_uuid("12345678-1234-1234-1234-123456789abc") == "1234567812341234123412345678" + "9abc"


def test_lowercases_uppercase_hex():
    assert _normalize_uuid("AABBCCDD-EEFF-1122-3344-556677889900") == "aabbccddeeff11223344556677889900"


def test_already_normalized_passes_through():
    norm = "1234567812341234123412345678abcd"
    assert _normalize_uuid(norm) == norm


def test_mixed_case_undashed():
    assert _normalize_uuid("AaBbCcDd") == "aabbccdd"

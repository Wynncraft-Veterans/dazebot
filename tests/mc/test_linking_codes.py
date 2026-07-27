"""Tests for the link-code alphabet, generator, and shape regex.

These pieces are pure and gate the username-fallback path in
``try_consume_code``: the regex is what decides whether an unknown
username's message contains a "code-shaped" run that warrants a
cross-row lookup. A too-permissive regex matches gibberish; a
too-strict one drops valid codes.
"""

from __future__ import annotations

from lib.mc.linking import _CODE_ALPHABET, _CODE_LENGTH, _CODE_SHAPED_RE, _generate_code


CONFUSABLE_EXCLUDED = set("0O1IL")


# ---------------------------------------------------------------------------
# _generate_code
# ---------------------------------------------------------------------------


def test_generate_code_length():
    for _ in range(100):
        assert len(_generate_code()) == _CODE_LENGTH


def test_generate_code_alphabet_membership():
    alphabet = set(_CODE_ALPHABET)
    for _ in range(100):
        assert set(_generate_code()) <= alphabet


def test_generate_code_excludes_confusable_chars():
    # Belt-and-suspenders: the alphabet itself must not contain them,
    # AND no generated code should either.
    assert CONFUSABLE_EXCLUDED.isdisjoint(_CODE_ALPHABET)
    for _ in range(100):
        assert CONFUSABLE_EXCLUDED.isdisjoint(_generate_code())


# ---------------------------------------------------------------------------
# _CODE_SHAPED_RE
# ---------------------------------------------------------------------------


def test_regex_matches_bare_generated_code():
    code = _generate_code()
    # Production code uppercases the message before matching; mirror that.
    assert _CODE_SHAPED_RE.findall(code.upper()) == [code]


def test_regex_matches_code_with_whitespace_padding():
    code = _generate_code()
    msg = f"hello {code} world".upper()
    assert _CODE_SHAPED_RE.findall(msg) == [code]


def test_regex_matches_code_after_punctuation():
    code = _generate_code()
    msg = f"my code is:{code}.".upper()
    assert _CODE_SHAPED_RE.findall(msg) == [code]


def test_regex_rejects_codes_with_excluded_chars():
    # All five excluded chars appear in a 6-char window — none should match.
    for confusable in CONFUSABLE_EXCLUDED:
        msg = ("ABCDE" + confusable).upper()
        assert _CODE_SHAPED_RE.findall(msg) == [], f"unexpectedly matched {msg!r}"


def test_regex_rejects_code_with_alnum_prefix():
    code = _generate_code()
    msg = ("X" + code).upper()
    # Leading alnum boundary fails the negative lookbehind.
    assert _CODE_SHAPED_RE.findall(msg) == []


def test_regex_rejects_code_with_alnum_suffix():
    code = _generate_code()
    msg = (code + "X").upper()
    # Trailing alnum boundary fails the negative lookahead.
    assert _CODE_SHAPED_RE.findall(msg) == []


def test_regex_rejects_code_embedded_in_word():
    code = _generate_code()
    msg = ("PREFIX" + code + "SUFFIX").upper()
    assert _CODE_SHAPED_RE.findall(msg) == []


def test_regex_finds_multiple_distinct_codes():
    a = _generate_code()
    b = _generate_code()
    # Even if they happen to collide, findall returns one entry per match,
    # so the count we expect is exactly two when separated by whitespace.
    msg = f"{a} and {b}".upper()
    assert _CODE_SHAPED_RE.findall(msg) == [a, b]


def test_regex_returns_empty_on_chatty_message_without_code():
    msg = "anyone in EU37 wanna do a raid".upper()
    assert _CODE_SHAPED_RE.findall(msg) == []


def test_regex_returns_empty_on_short_message():
    # Five chars from the alphabet is one short of CODE_LENGTH.
    assert _CODE_SHAPED_RE.findall("ABCDE") == []


# --------------------------------------------------------------------------
# In-game acknowledgement
# --------------------------------------------------------------------------


def test_contains_code_shape_spots_a_code_attempt():
    """Picolimbo acks every line with "Code sent.", so a wrong code reads
    as success unless we can tell it apart from ordinary chat."""
    from lib.mc.linking import contains_code_shape

    assert contains_code_shape("ABC234")
    assert contains_code_shape("my code is abc234 thanks")


def test_contains_code_shape_ignores_ordinary_chat():
    from lib.mc.linking import contains_code_shape

    assert not contains_code_shape("hello everyone")
    assert not contains_code_shape("")
    # Excluded characters (0/O/1/I/L) can't appear in an issued code.
    assert not contains_code_shape("HELLO1")
    # Wrong length either side of six.
    assert not contains_code_shape("ABC23")
    assert not contains_code_shape("ABC2345")

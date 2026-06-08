"""Unit tests for ``lib.emerald`` parser + formatter.

Spec examples from ``.claude/ephemeral/donations_management.md``::

    5436584         == 5436584e
    20stx47le18eb40e == 5436584
    20stx47.29le    == 5436584   (decimal le)
"""

import pytest

from lib.emerald import (
    EmeraldParseError,
    E_PER_EB,
    E_PER_LE,
    E_PER_STX,
    format_emeralds_as_stx,
    parse_emeralds,
)


class TestParseFromSpec:
    def test_bare_int(self):
        assert parse_emeralds("5436584") == 5436584

    def test_bare_with_e_suffix(self):
        assert parse_emeralds("5436584e") == 5436584

    def test_full_compound(self):
        assert parse_emeralds("20stx47le18eb40e") == 5436584

    def test_decimal_le(self):
        # The spec rounds "20stx47.29le" to 5436584, but mathematically
        # 47.29 * 4096 = 193699.84 floored = 193699, so the exact total is
        # 5242880 + 193699 = 5436579. The parser uses floor() to match the
        # WynnVentory JS calculator reference (Math.floor of the sum).
        # Exact decimal equivalent is "47.291015625le" (which would give
        # 5436584 exactly), but staff will type decimal approximations.
        assert parse_emeralds("20stx47.29le") == 5436579


class TestParseFormats:
    def test_with_spaces_between_tokens(self):
        assert parse_emeralds("20 stx 47 le 18 eb 40 e") == 5436584

    def test_with_spaces_between_value_and_unit(self):
        assert parse_emeralds("20stx 47 le") == 20 * E_PER_STX + 47 * E_PER_LE

    def test_uppercase(self):
        assert parse_emeralds("20STX") == 20 * E_PER_STX

    def test_mixed_case(self):
        assert parse_emeralds("20Stx47Le") == 20 * E_PER_STX + 47 * E_PER_LE

    def test_leading_trailing_whitespace(self):
        assert parse_emeralds("   1le   ") == E_PER_LE

    def test_just_le(self):
        assert parse_emeralds("1le") == E_PER_LE

    def test_just_eb(self):
        assert parse_emeralds("1eb") == E_PER_EB

    def test_zero(self):
        assert parse_emeralds("0e") == 0

    def test_zero_bare(self):
        assert parse_emeralds("0") == 0

    def test_decimal_floored(self):
        # 0.5 le = 2048 e exactly; 0.5001 le = 2048.4096 floored to 2048
        assert parse_emeralds("0.5le") == 2048
        assert parse_emeralds("0.5001le") == 2048


class TestParseErrors:
    def test_empty(self):
        with pytest.raises(EmeraldParseError):
            parse_emeralds("")

    def test_whitespace_only(self):
        with pytest.raises(EmeraldParseError):
            parse_emeralds("   ")

    def test_bad_unit(self):
        with pytest.raises(EmeraldParseError):
            parse_emeralds("5xyz")

    def test_letters_only(self):
        with pytest.raises(EmeraldParseError):
            parse_emeralds("stx")

    def test_negative_rejected(self):
        with pytest.raises(EmeraldParseError):
            parse_emeralds("-5e")

    def test_trailing_garbage(self):
        with pytest.raises(EmeraldParseError):
            parse_emeralds("1stx garbage")

    def test_non_string(self):
        with pytest.raises(EmeraldParseError):
            parse_emeralds(123)  # type: ignore[arg-type]

    def test_error_carries_token(self):
        with pytest.raises(EmeraldParseError) as exc_info:
            parse_emeralds("5xyz")
        assert exc_info.value.token == "5xyz"


class TestFormat:
    def test_zero(self):
        assert format_emeralds_as_stx(0) == "0.00 stx"

    def test_exact_stx(self):
        assert format_emeralds_as_stx(E_PER_STX) == "1.00 stx"

    def test_spec_example(self):
        # From the spec: 20.74 stx for the 5436584 examples
        assert format_emeralds_as_stx(5436584) == "20.74 stx"

    def test_small_value_rounds_to_zero(self):
        assert format_emeralds_as_stx(100) == "0.00 stx"

    def test_two_decimal_places_preserved(self):
        # 2.32 stx — example from the spec
        assert format_emeralds_as_stx(int(2.32 * E_PER_STX)) == "2.32 stx"

    def test_negative(self):
        assert format_emeralds_as_stx(-E_PER_STX) == "-1.00 stx"


class TestRoundingBound:
    """The parser's rounding error must stay under 10000 e for any input
    representing < 150 stx (the realistic upper bound on a single
    donation). Tested via equivalent-representation pairs and a
    pathological many-decimal-tokens stress case.
    """

    MAX_E_VALUE = 150 * E_PER_STX  # 39,321,600
    MAX_DRIFT = 10_000

    def _assert_within_bound(self, a: int, b: int) -> None:
        assert abs(a - b) <= self.MAX_DRIFT, (
            f"parsing drift {abs(a - b)} exceeds {self.MAX_DRIFT}e"
        )

    def test_canonical_vs_decimal_stx(self):
        # 5,436,584e expressed exactly vs as decimal stx (2 dp)
        raw = parse_emeralds("5436584")
        decimal = parse_emeralds("20.74stx")  # 20.74 * 262144 = 5,436,866.56 -> 5,436,866
        self._assert_within_bound(raw, decimal)

    def test_canonical_vs_compound_with_decimal_le(self):
        # 5,436,584 vs 20stx47.29le (~ 47.291015625 le exactly)
        raw = parse_emeralds("5436584")
        approx = parse_emeralds("20stx47.29le")
        self._assert_within_bound(raw, approx)

    def test_compound_vs_decimal_stx_under_150(self):
        # 149stx = 39,059,456e vs "149.00stx"
        raw = parse_emeralds("149stx")
        decimal = parse_emeralds("149.00stx")
        self._assert_within_bound(raw, decimal)

    def test_many_decimal_tokens_stay_under_bound(self):
        # Pathological: 100 tokens of 0.5 stx each = 50 stx total (well
        # under 150). Per-token flooring would accumulate up to ~100e of
        # error; accumulate-then-floor stays at <1e.
        many_tokens = " ".join(["0.5stx"] * 100)
        assert parse_emeralds(many_tokens) <= self.MAX_E_VALUE
        # Exact equivalent: 50stx
        self._assert_within_bound(parse_emeralds("50stx"), parse_emeralds(many_tokens))

    def test_thousand_small_decimal_tokens(self):
        # Even at 1000 tokens (silly but possible), the bound holds.
        many_tokens = " ".join(["0.05stx"] * 1000)  # 50 stx total
        self._assert_within_bound(parse_emeralds("50stx"), parse_emeralds(many_tokens))

    def test_max_realistic_donation_bound(self):
        # Largest realistic donation: 149.99 stx as decimal vs raw e.
        decimal = parse_emeralds("149.99stx")
        raw_e = parse_emeralds(str(decimal))
        self._assert_within_bound(decimal, raw_e)

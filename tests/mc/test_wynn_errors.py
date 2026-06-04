"""Tests for ``raise_for_error_envelope``.

The Wynncraft v3 API returns an error envelope (``{"error": "...", "code": 4xx}``
or sometimes ``{"Error": "..."}``) instead of the documented payload on
rejection. Without this guard, callers feed the envelope into pydantic
and surface a wall of unrelated "Field required" validation errors.
"""

from __future__ import annotations

import pytest

from lib.mc.wynn_api.errors import WynnApiError, raise_for_error_envelope


# ---------------------------------------------------------------------------
# Happy path (no-op)
# ---------------------------------------------------------------------------


def test_normal_dict_passthrough():
    """A normal payload (no error/Error key) must not raise."""
    raise_for_error_envelope({"username": "Wenweia", "uuid": "abc"})


def test_list_passthrough():
    """Lists (e.g. /v3/guild/list) never carry the error envelope."""
    raise_for_error_envelope([{"name": "Returners"}])


def test_none_passthrough():
    """Pre-pydantic guard; non-dicts in general must be a no-op."""
    raise_for_error_envelope(None)


def test_string_passthrough():
    raise_for_error_envelope("not a dict")


# ---------------------------------------------------------------------------
# Error envelope detection
# ---------------------------------------------------------------------------


def test_lowercase_error_key_raises():
    with pytest.raises(WynnApiError) as exc_info:
        raise_for_error_envelope({"error": "Player not found", "code": 404})
    assert exc_info.value.code == 404
    assert exc_info.value.message == "Player not found"


def test_capitalised_Error_key_also_raises():
    """The API has been observed to send both ``error`` and ``Error`` —
    both shapes must trip the guard."""
    with pytest.raises(WynnApiError) as exc_info:
        raise_for_error_envelope({"Error": "Rate limited", "Code": 429})
    assert exc_info.value.code == 429
    assert exc_info.value.message == "Rate limited"


def test_error_without_code_defaults_to_zero():
    with pytest.raises(WynnApiError) as exc_info:
        raise_for_error_envelope({"error": "something"})
    assert exc_info.value.code == 0


def test_non_int_code_coerces_to_zero():
    """If ``code`` arrives as a non-numeric string, fall back to 0 rather
    than crashing the guard itself."""
    with pytest.raises(WynnApiError) as exc_info:
        raise_for_error_envelope({"error": "oops", "code": "not-a-number"})
    assert exc_info.value.code == 0


def test_url_included_in_exception_message():
    """The url kwarg is for log context — it must end up in ``str(exc)``."""
    url = "https://api.wynncraft.com/v3/player/Wenweia"
    with pytest.raises(WynnApiError) as exc_info:
        raise_for_error_envelope({"error": "Player not found"}, url=url)
    assert url in str(exc_info.value)
    assert exc_info.value.url == url


def test_string_code_that_parses_as_int_is_preserved():
    """``"404"`` is a common JSON quirk — keep the value rather than
    discarding it as 0."""
    with pytest.raises(WynnApiError) as exc_info:
        raise_for_error_envelope({"error": "not found", "code": "404"})
    assert exc_info.value.code == 404

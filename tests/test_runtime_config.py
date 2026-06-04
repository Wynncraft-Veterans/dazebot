"""Tests for the pure helpers in ``lib/runtime_config.py``.

Every ``/config`` write goes through ``_coerce`` (string → typed), then
``_serialize`` (typed → JSON). The bool-before-int precedence is the
fragile bit: bool is an ``int`` subclass, so a careless reorder makes
``_coerce("true", BOOL_KEY)`` return ``1`` instead of ``True``, silently
corrupting overrides.

``_is_overridable`` is the gate that decides which ``Config`` attributes
are user-facing — private, lowercase, class, and callable attributes
must stay invisible.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from lib.runtime_config import (
    _coerce,
    _deserialize,
    _is_overridable,
    _serialize,
)


# ---------------------------------------------------------------------------
# _coerce — bool branch (must precede int because bool is an int subclass)
# ---------------------------------------------------------------------------


class _StubConfig:
    """Holder for current-value introspection. ``_coerce`` reads
    ``getattr(CurrConfig, key)`` to learn the target type, but we can
    swap that with monkeypatch."""


@pytest.fixture
def stub_config(monkeypatch):
    """Replace ``lib.runtime_config.CurrConfig`` with a blank object whose
    attributes we set per-test to control the coerce branch."""
    import lib.runtime_config as rc

    stub = _StubConfig()
    monkeypatch.setattr(rc, "CurrConfig", stub)
    return stub


@pytest.mark.parametrize("raw", ["true", "True", "TRUE", "1", "yes", "y", "on"])
def test_coerce_bool_true_synonyms(stub_config, raw):
    stub_config.FLAG = True
    assert _coerce("FLAG", raw) is True


@pytest.mark.parametrize("raw", ["false", "False", "FALSE", "0", "no", "n", "off"])
def test_coerce_bool_false_synonyms(stub_config, raw):
    stub_config.FLAG = True
    assert _coerce("FLAG", raw) is False


def test_coerce_bool_rejects_unparseable(stub_config):
    stub_config.FLAG = False
    with pytest.raises(ValueError, match="bool"):
        _coerce("FLAG", "maybe")


def test_coerce_bool_takes_priority_over_int(stub_config):
    """Regression guard: bool is an int subclass, so checking int first
    would coerce ``"1"`` into ``1`` for a BOOL_KEY instead of ``True``."""
    stub_config.FLAG = False
    result = _coerce("FLAG", "1")
    assert result is True
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _coerce — int / float / timedelta / str
# ---------------------------------------------------------------------------


def test_coerce_int(stub_config):
    stub_config.NUM = 0
    assert _coerce("NUM", "42") == 42


def test_coerce_int_raises_on_garbage(stub_config):
    stub_config.NUM = 0
    with pytest.raises(ValueError):
        _coerce("NUM", "abc")


def test_coerce_float(stub_config):
    stub_config.RATIO = 1.0
    assert _coerce("RATIO", "0.25") == 0.25


def test_coerce_timedelta_uses_seconds(stub_config):
    stub_config.TTL = timedelta(seconds=60)
    assert _coerce("TTL", "3600") == timedelta(seconds=3600)


def test_coerce_str_passthrough(stub_config):
    stub_config.NAME = "default"
    assert _coerce("NAME", "anything") == "anything"


# ---------------------------------------------------------------------------
# _serialize / _deserialize round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [True, False, 0, 1, 42, -7, 1.5, 0.0, "", "hello world", "with \"quotes\""],
)
def test_scalar_round_trip(value):
    assert _deserialize(_serialize(value)) == value


def test_bool_survives_round_trip_as_bool_not_int():
    """``True`` must come back as ``True``, not ``1`` — downstream
    setattr-based application would otherwise change the type of the
    config attribute."""
    result = _deserialize(_serialize(True))
    assert result is True
    assert isinstance(result, bool)


def test_timedelta_round_trip_preserves_type_and_value():
    td = timedelta(hours=2, minutes=30)
    decoded = _deserialize(_serialize(td))
    assert isinstance(decoded, timedelta)
    assert decoded == td


def test_timedelta_serialized_with_sentinel_marker():
    """The on-disk shape must include the ``__timedelta__`` marker so a
    deserializer knows to reconstruct the type — pure ``json.dumps`` on a
    timedelta would either crash or lose the type."""
    blob = _serialize(timedelta(seconds=10))
    assert "__timedelta__" in blob


# ---------------------------------------------------------------------------
# _is_overridable
# ---------------------------------------------------------------------------


def test_is_overridable_accepts_uppercase_scalars():
    assert _is_overridable("FLAG", True)
    assert _is_overridable("NUM", 42)
    assert _is_overridable("RATIO", 1.5)
    assert _is_overridable("NAME", "x")
    assert _is_overridable("TTL", timedelta(seconds=1))


def test_is_overridable_rejects_private_underscore_prefix():
    assert not _is_overridable("_INTERNAL", 1)


def test_is_overridable_rejects_lowercase():
    """Only ALL_CAPS module-level constants count as user-facing
    config."""
    assert not _is_overridable("name", "x")


def test_is_overridable_rejects_classes():
    assert not _is_overridable("CLS", type("Inner", (), {}))


def test_is_overridable_rejects_callables():
    assert not _is_overridable("FN", lambda: None)


def test_is_overridable_rejects_unsupported_types():
    """``set`` / ``list`` / ``dict`` etc. aren't safely coerce-able from a
    string and must not be exposed."""
    assert not _is_overridable("ADMINS", {1, 2, 3})
    assert not _is_overridable("ROLES", [1, 2, 3])
    assert not _is_overridable("MAP", {"a": 1})

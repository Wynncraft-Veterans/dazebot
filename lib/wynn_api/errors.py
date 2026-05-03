"""Shared error types and helpers for Wynncraft API responses.

The v3 API returns an error envelope like ``{"error": "...", "code": 4xx}``
(sometimes ``{"Error": "..."}``) instead of the documented payload when a
request is rejected (bad token, missing player, rate-limit, etc.). Without
detection, callers feed that dict straight into pydantic and get an
unrelated wall of "Field required" validation errors.
"""

from __future__ import annotations

from typing import Any


class WynnApiError(RuntimeError):
    """Raised when the Wynncraft API returns an error envelope.

    Attributes:
        code:    HTTP-ish code from the body (or 0 if absent).
        message: Human-readable error string from the body.
        url:     Request URL, for log context.
    """

    def __init__(self, code: int, message: str, url: str | None = None):
        self.code = code
        self.message = message
        self.url = url
        suffix = f" ({url})" if url else ""
        super().__init__(f"Wynn API error {code}: {message}{suffix}")


def raise_for_error_envelope(data: Any, *, url: str | None = None) -> None:
    """If ``data`` looks like a Wynn API error envelope, raise ``WynnApiError``.

    A no-op for normal payloads (lists, or dicts without an ``error``/``Error``
    key). Safe to call before constructing pydantic models.
    """
    if not isinstance(data, dict):
        return
    err = data.get("error") or data.get("Error")
    if err is None:
        return
    code = data.get("code") or data.get("Code") or 0
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        code_int = 0
    raise WynnApiError(code_int, str(err), url=url)

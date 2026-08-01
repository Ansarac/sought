"""Vertical sources: quota exhaustion versus refusal.

`rate_limited` and `blocked` earn opposite advice — wait, versus stop asking
this way — so a source that spells "over quota" in its own dialect must not be
filed under "the network refused us". Two of these do exactly that, and both
are covered here against saved bodies.
"""

from __future__ import annotations

import pytest

from sought.config import Config
from sought.engines import verticals
from sought.engines.base import EngineFailure


def _failure(response) -> EngineFailure:
    """Run the guard over a handler that raises this response's HTTPError."""

    def handler():
        response.raise_for_status()
        raise AssertionError  # pragma: no cover — raise_for_status always fires here

    with pytest.raises(EngineFailure) as exc:
        verticals._guard(handler)  # noqa: SLF001
    return exc.value


def test_http_429_is_rate_limited(response):
    assert _failure(response(429, "too many requests")).status == "rate_limited"


def test_githubs_403_over_quota_is_rate_limited_not_blocked(response, saved):
    """GitHub answers an unauthenticated search over quota with 403, not 429."""
    resp = response(
        403,
        saved("github_rate_limited.json"),
        {"x-ratelimit-remaining": "0", "x-ratelimit-limit": "10"},
    )
    failure = _failure(resp)
    assert failure.status == "rate_limited"
    assert "403" in failure.detail


def test_githubs_403_over_quota_is_recognised_from_the_body_alone(response, saved):
    """The header is the reliable signal, but the message is a fair backstop."""
    assert _failure(response(403, saved("github_rate_limited.json"))).status == "rate_limited"


def test_an_ordinary_403_is_still_blocked(response, saved):
    """The fix must not turn every refusal into "just wait a bit"."""
    resp = response(403, saved("github_forbidden.json"), {"x-ratelimit-remaining": "58"})
    assert _failure(resp).status == "blocked"


def test_stackexchanges_throttle_violation_is_rate_limited(response, saved):
    """StackExchange throttling arrives as a 400 with the reason only in the body."""
    failure = _failure(response(400, saved("stackexchange_throttled.json")))
    assert failure.status == "rate_limited"
    assert "throttle_violation" in failure.detail


def test_an_ordinary_400_is_still_blocked(response):
    assert _failure(response(400, '{"error_name": "bad_parameter"}')).status == "blocked"


def test_a_response_less_http_error_does_not_crash_the_classifier():
    import requests

    def handler():
        raise requests.HTTPError("no response attached")

    with pytest.raises(EngineFailure) as exc:
        verticals._guard(handler)  # noqa: SLF001
    assert exc.value.status == "blocked"


def test_a_timeout_stays_a_timeout():
    import requests

    def handler():
        raise requests.Timeout

    with pytest.raises(EngineFailure) as exc:
        verticals._guard(handler)  # noqa: SLF001
    assert exc.value.status == "timeout"


def test_an_unparseable_body_is_an_error_not_an_empty():
    def handler():
        msg = "Expecting value"
        raise ValueError(msg)

    with pytest.raises(EngineFailure) as exc:
        verticals._guard(handler)  # noqa: SLF001
    assert exc.value.status == "error"


def test_an_unknown_source_is_an_error():
    with pytest.raises(EngineFailure) as exc:
        verticals.search("nonesuch", "q", config=Config())
    assert exc.value.status == "error"


def test_a_source_that_matches_nothing_is_empty(monkeypatch):
    monkeypatch.setitem(verticals._HANDLERS, "wikipedia", lambda _s, _q, _c: [])  # noqa: SLF001
    monkeypatch.setattr(verticals, "session", lambda _cfg: None)

    with pytest.raises(EngineFailure) as exc:
        verticals.search("wikipedia", "q", config=Config())
    assert exc.value.status == "empty"

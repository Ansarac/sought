"""Marginalia's failure classification, against saved pages.

Marginalia is the only general engine that works without Chromium, which makes
it the last thing standing when the browser path is gone — and therefore the
one whose "no results" answer had better be true.
"""

from __future__ import annotations

import pytest

from sought.config import Config
from sought.engines import marginalia
from sought.engines.base import EngineFailure


@pytest.fixture
def http(monkeypatch, http_session):
    """Serve one canned response to marginalia.search."""

    def serve(canned):
        sess = http_session(canned)
        monkeypatch.setattr(marginalia, "session", lambda _cfg: sess)
        return sess

    return serve


def test_a_normal_page_yields_results(http, response, saved):
    http(response(200, saved("marginalia_results.html")))
    results = marginalia.search("unix philosophy", config=Config())
    assert [r.title for r in results] == ["Unix philosophy", "Second result"]


def test_the_parser_still_matches_a_real_page(http, response, saved):
    """A drift guard against the live markup, not against a page we wrote.

    A parser that has quietly stopped matching reports `empty` forever, which
    is the same false "the web has no answer" this module exists to prevent —
    just arrived at from the other direction.
    """
    http(response(200, saved("marginalia_results_live.html")))

    results = marginalia.search("unix philosophy", config=Config(limit=25))

    assert len(results) == 25
    assert all(r.title and r.url.startswith("http") for r in results)
    assert results[0].snippet


def test_the_limit_is_applied(http, response, saved):
    http(response(200, saved("marginalia_results.html")))
    assert len(marginalia.search("unix philosophy", config=Config(limit=1))) == 1


def test_a_block_page_served_with_http_200_is_not_reported_as_empty(http, response, saved):
    """The measured failure: HTTP 200, zero cards, and sought was never asked.

    Reporting this as `empty` reaches the CLI as exit 1 — "the sources worked
    and the web had nothing" — which is a confident, false statement.
    """
    http(response(200, saved("marginalia_access_denied.html")))

    with pytest.raises(EngineFailure) as exc:
        marginalia.search("unix philosophy", config=Config())

    assert exc.value.status == "rate_limited"
    assert "access denied" in exc.value.detail.lower()


def test_marginalias_own_throttle_page_is_rate_limited_not_empty(http, response, saved):
    """Captured live from old-search.marginalia.nu while fixing this.

    HTTP 200, zero cards, and the body saying in plain English that the engine
    is refusing bots. Reported as `empty` this becomes exit 1 — and marginalia
    is the engine that survives when Chromium is gone, so its wrong answer is
    the last word the user gets.
    """
    http(response(200, saved("marginalia_bot_barrage.html")))

    with pytest.raises(EngineFailure) as exc:
        marginalia.search("unix philosophy", config=Config())

    assert exc.value.status == "rate_limited"


def test_a_genuinely_empty_index_is_still_reported_as_empty(http, response, saved):
    """The other half, also captured live: a real "No search results found" page.

    The distinction only matters if both sides survive it — a classifier that
    called everything `rate_limited` would be exactly as useless as the one
    that called everything `empty`.
    """
    http(response(200, saved("marginalia_no_matches.html")))

    with pytest.raises(EngineFailure) as exc:
        marginalia.search("zqxjkvwmpf blorptangle qqzzxx", config=Config())

    assert exc.value.status == "empty"


def test_the_bot_wall_marker_list_is_not_duplicated():
    """One list, shared with the browser engines. Two copies would drift."""
    from sought import browser

    assert marginalia.classify_dead_page is browser.classify_dead_page


def test_http_429_is_rate_limited(http, response):
    http(response(429, "slow down"))
    with pytest.raises(EngineFailure) as exc:
        marginalia.search("q", config=Config())
    assert exc.value.status == "rate_limited"


def test_an_http_error_status_is_blocked(http, response):
    http(response(503, "unavailable"))
    with pytest.raises(EngineFailure) as exc:
        marginalia.search("q", config=Config())
    assert exc.value.status == "blocked"


def test_a_timeout_is_a_timeout(http):
    import requests

    http(requests.Timeout("too slow"))
    with pytest.raises(EngineFailure) as exc:
        marginalia.search("q", config=Config(timeout_seconds=5))
    assert exc.value.status == "timeout"
    assert "5s" in exc.value.detail

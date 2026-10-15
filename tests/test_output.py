"""Rendering. Nothing here is cosmetic.

The pretty renderer is where the audit trail becomes something a human reads,
so a wrong engine name or a swallowed warning here undoes the work every other
module does to keep the facts straight.
"""

from __future__ import annotations

import json

from sought.engines.base import Attempt, Result
from sought.output import envelope, print_json, print_pretty


def _payload(**overrides):
    base = envelope(
        query="q",
        engine="startpage",
        attempts=[
            Attempt("brave", "rate_limited", "HTTP 429"),
            Attempt("startpage", "ok", count=2),
        ],
        results=[
            Result("First", "https://one.example", "snippet one"),
            Result("Second", "https://two.example", ""),
        ],
        low_relevance=False,
    )
    return {**base, **overrides}


def test_the_envelope_carries_the_attempts_even_when_it_carries_results():
    payload = _payload()
    assert [a["engine"] for a in payload["attempts"]] == ["brave", "startpage"]
    assert payload["attempts"][1]["count"] == 2


def test_a_failed_attempt_reports_no_count_and_a_reason():
    failed = envelope(
        query="q",
        engine=None,
        attempts=[Attempt("brave", "blocked", "HTTP 403")],
        results=[],
        low_relevance=False,
    )
    assert failed["attempts"][0] == {"engine": "brave", "status": "blocked", "detail": "HTTP 403"}


def test_json_output_is_one_parseable_document(capsys):
    print_json(_payload())
    assert json.loads(capsys.readouterr().out)["engine"] == "startpage"


def test_pretty_output_credits_the_engine_that_produced_the_results(capsys):
    print_pretty(_payload())
    out = capsys.readouterr().out
    assert "2 results via startpage" in out


def test_pretty_output_shows_every_attempt_including_the_failures(capsys):
    print_pretty(_payload())
    out = capsys.readouterr().out
    assert "✗ brave: rate_limited — HTTP 429" in out
    assert "✓ startpage: 2 results" in out


def test_a_failure_with_no_detail_still_gets_an_explanation(capsys):
    """A bare status is not an explanation, and the invariant is about explaining."""
    print_pretty(
        envelope(query="q", engine=None, attempts=[Attempt("brave", "rate_limited")], results=[], low_relevance=False)
    )
    assert "quota exhausted" in capsys.readouterr().out


def test_the_low_relevance_warning_is_impossible_to_miss(capsys):
    print_pretty(_payload(low_relevance=True))
    out = capsys.readouterr().out
    assert "LOW RELEVANCE" in out
    assert "Verify before trusting" in out


def test_no_results_says_so_rather_than_printing_an_empty_table(capsys):
    print_pretty(_payload(results=[], engine=None))
    assert "No results." in capsys.readouterr().out


def test_long_fields_are_clipped_rather_than_wrapped(capsys):
    print_pretty(_payload(results=[{"title": "T" * 400, "url": "https://example.com", "snippet": ""}]))
    lines = capsys.readouterr().out.splitlines()
    assert all(len(line) <= 121 for line in lines)
    assert any(line.endswith("…") for line in lines)


def test_a_fresh_envelope_says_it_is_not_cached():
    assert _payload()["cached"] is None


def test_a_fresh_search_prints_no_cache_line(capsys):
    print_pretty(_payload())
    assert "cached" not in capsys.readouterr().out


def test_a_replay_is_labelled_before_anything_else(capsys):
    print_pretty(_payload(cached={"age_seconds": 42}))
    first = capsys.readouterr().out.splitlines()[0]
    assert first == "(cached, 42s old — use --fresh to search again)"


def test_long_cache_ages_read_in_hours(capsys):
    print_pretty(_payload(cached={"age_seconds": 3 * 3600 + 5 * 60}))
    assert "3h 5m old" in capsys.readouterr().out

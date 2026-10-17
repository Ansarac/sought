"""Rendering of the result envelope: JSON for agents, a table for humans."""

from __future__ import annotations

import json
import shutil
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sought.engines.base import Attempt, Result

_STATUS_HINT = {
    "rate_limited": "quota exhausted for now — retry later or reorder engines",
    "blocked": "the network or the engine refused us",
    "timeout": "no response within the configured timeout",
    "empty": "responded normally but matched nothing",
    "error": "unexpected failure",
}


def envelope(  # noqa: PLR0913 — keyword-only, one per envelope field
    *,
    query: str,
    engine: str | None,
    attempts: list[Attempt],
    results: list[Result],
    low_relevance: bool,
    relevance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the audit envelope returned by every search.

    `cached` is always present so the shape never changes: `null` for a search
    that just happened, `{"age_seconds": N}` when `sought.state` replays one.
    `relevance` likewise: `null` unless `low_relevance` is set, then the
    numbers behind it (see `sought.engines.base.relevance_report`), so that a
    reader can judge the flag instead of only obeying it.
    """
    return {
        "query": query,
        "engine": engine,
        "cached": None,
        "attempts": [a.as_dict() for a in attempts],
        "low_relevance": low_relevance,
        "relevance": relevance,
        "results": [r.as_dict() for r in results],
    }


def print_json(payload: dict[str, Any]) -> None:
    """Write the envelope as one JSON document on stdout."""
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def print_pretty(payload: dict[str, Any]) -> None:
    """Write a human-readable rendering of the envelope on stdout."""
    width = min(shutil.get_terminal_size((100, 24)).columns, 120)

    cached = payload.get("cached")
    if cached:
        print(f"(cached, {_age(cached.get('age_seconds', 0))} old — use --fresh to search again)")
    for attempt in payload["attempts"]:
        status = attempt["status"]
        if status == "ok":
            # An `ok` detail is something the engine wants on the record even
            # though it answered — a long challenge wait, a disclosed evasion.
            note = f" — {attempt['detail']}" if attempt.get("detail") else ""
            print(f"  ✓ {attempt['engine']}: {attempt.get('count', 0)} results{note}")
        else:
            hint = _STATUS_HINT.get(status, "")
            detail = attempt.get("detail") or hint
            print(f"  ✗ {attempt['engine']}: {status} — {detail}")
    print()

    if payload["low_relevance"]:
        print(f"!! LOW RELEVANCE: {_relevance_summary(payload.get('relevance'))}")
        # One possibility among two, not a verdict: the check cannot tell a
        # decoy page from a query more specific than what the web has.
        print(
            "!! Either these results only touch part of the query, or the engine served decoys. Verify before trusting."
        )
        print()

    results = payload["results"]
    if not results:
        print("No results.")
        return

    for i, r in enumerate(results, 1):
        print(f"{i}. {_clip(r['title'], width - 4)}")
        print(f"   {_clip(r['url'], width - 4)}")
        if r.get("snippet"):
            print(f"   {_clip(r['snippet'], width - 4)}")
        print()

    print(f"— {len(results)} results via {payload['engine']}")


def _relevance_summary(report: dict[str, Any] | None) -> str:
    """Describe what the relevance check measured, in terms that are true of it.

    It used to say "no result mentions any query term", which is not what is
    checked, and was visibly false whenever a flagged result's title held a
    query word. A payload without the numbers still gets an accurate sentence.
    """
    if not report:
        return "even the best result covers less than half of the query's terms."
    coverage = round(report.get("best_coverage", 0) * 100)
    matched = ", ".join(report.get("matched") or []) or "none"
    missing = ", ".join(report.get("missing") or []) or "none"
    return (
        f"the best result covers only {coverage}% of the query's terms, weighted by length "
        f"(matched: {matched}; missing from every result: {missing})."
    )


def _age(seconds: int) -> str:
    if seconds < 60:  # noqa: PLR2004 — one minute
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:  # noqa: PLR2004 — one hour
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"

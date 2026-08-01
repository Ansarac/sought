"""Rendering of the result envelope: JSON for agents, a table for humans."""

from __future__ import annotations

import json
import shutil
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scout.engines.base import Attempt, Result

_STATUS_HINT = {
    "rate_limited": "quota exhausted for now — retry later or reorder engines",
    "blocked": "the network or the engine refused us",
    "timeout": "no response within the configured timeout",
    "empty": "responded normally but matched nothing",
    "error": "unexpected failure",
}


def envelope(
    *,
    query: str,
    engine: str | None,
    attempts: list[Attempt],
    results: list[Result],
    low_relevance: bool,
) -> dict[str, Any]:
    """Build the audit envelope returned by every search."""
    return {
        "query": query,
        "engine": engine,
        "attempts": [a.as_dict() for a in attempts],
        "low_relevance": low_relevance,
        "results": [r.as_dict() for r in results],
    }


def print_json(payload: dict[str, Any]) -> None:
    """Write the envelope as one JSON document on stdout."""
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def print_pretty(payload: dict[str, Any]) -> None:
    """Write a human-readable rendering of the envelope on stdout."""
    width = min(shutil.get_terminal_size((100, 24)).columns, 120)

    for attempt in payload["attempts"]:
        status = attempt["status"]
        if status == "ok":
            print(f"  ✓ {attempt['engine']}: {attempt.get('count', 0)} results")
        else:
            hint = _STATUS_HINT.get(status, "")
            detail = attempt.get("detail") or hint
            print(f"  ✗ {attempt['engine']}: {status} — {detail}")
    print()

    if payload["low_relevance"]:
        print("!! LOW RELEVANCE: no result mentions any query term.")
        print("!! This engine may be serving decoy results. Verify before trusting.")
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


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"

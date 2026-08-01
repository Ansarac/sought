"""The engine protocol, the result/attempt records, and failure classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from scout.browser import Browser
    from scout.config import Config

Status = Literal["ok", "rate_limited", "blocked", "empty", "timeout", "error"]

GENERAL_ENGINES = ("brave", "startpage", "marginalia")
VERTICAL_SOURCES = ("wikipedia", "github", "hn", "stackoverflow", "npm", "pypi", "news")

# Engines that need a real browser. Plain HTTP against these gets a 429 or a
# bot-check page — verified, not assumed.
BROWSER_ENGINES = frozenset({"brave", "startpage"})


def all_source_names() -> tuple[str, ...]:
    """Every value accepted by --source."""
    return GENERAL_ENGINES + VERTICAL_SOURCES


@dataclass(frozen=True)
class Result:
    """One search hit."""

    title: str
    url: str
    snippet: str = ""

    def as_dict(self) -> dict[str, str]:
        """Serialise for the JSON envelope."""
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


@dataclass(frozen=True)
class Attempt:
    """The record of one engine being asked, successfully or not.

    Every engine consulted produces one of these, including the ones that
    failed. An empty result set with no attempts is the one output scout must
    never produce.
    """

    engine: str
    status: Status
    detail: str = ""
    count: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the JSON envelope."""
        out: dict[str, Any] = {"engine": self.engine, "status": self.status}
        if self.status == "ok":
            out["count"] = self.count
        if self.detail:
            out["detail"] = self.detail
        return out


class EngineFailure(Exception):  # noqa: N818 — this is a value carrier, not an "Error"
    """An engine could not answer. Carries the classification, not just a message.

    `empty` (asked and answered, nothing matched) and `rate_limited` (never
    really asked) are different facts, and collapsing them into "no results"
    is what makes a search tool untrustworthy.
    """

    def __init__(self, status: Status, detail: str = "") -> None:
        """Record why an engine could not answer."""
        super().__init__(detail or status)
        self.status: Status = status
        self.detail = detail


class Engine(Protocol):
    """What every backend implements.

    `browser` is None for HTTP-only engines, and non-None for anything in
    BROWSER_ENGINES.
    """

    name: str

    def search(self, query: str, *, config: Config, browser: Browser | None) -> list[Result]:
        """Run the query, or raise EngineFailure with a classification."""
        ...


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    ]
)
_MIN_TOKEN_LEN = 3


def query_tokens(query: str) -> list[str]:
    """Significant lowercase tokens of a query, for relevance checking."""
    return [t for t in _TOKEN_RE.findall(query.lower()) if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS]


def detect_low_relevance(query: str, results: list[Result]) -> bool:
    """Report whether no result mentions any significant query token.

    Some engines answer a blocked request with a page of plausible but
    unrelated results — HTTP 200, well-formed, completely wrong. There is no
    error to catch, so this heuristic is the only signal. It is deliberately
    biased toward false positives: a spurious warning costs a second glance,
    a missed one costs a wrong answer.
    """
    tokens = query_tokens(query)
    if not tokens or not results:
        return False

    for r in results:
        haystack = f"{r.title} {r.url} {r.snippet}".lower()
        if any(t in haystack for t in tokens):
            return False
    return True

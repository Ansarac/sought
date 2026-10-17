"""The engine protocol, the result/attempt records, and failure classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sought.browser import Browser
    from sought.config import Config

Status = Literal["ok", "rate_limited", "blocked", "empty", "timeout", "error"]

GENERAL_ENGINES = ("brave", "startpage", "marginalia", "google")
VERTICAL_SOURCES = ("wikipedia", "github", "hn", "stackoverflow", "npm", "pypi", "news")

# General engines that stay off unless the config file switches them on;
# naming one in `engines.order` or `--source` is not enough on its own. Not in
# the default order either. See `sought.engines.google` for why google is one.
OPT_IN_ENGINES = frozenset({"google"})

# Engines that borrow the chain's shared Chromium. Plain HTTP against these
# gets a 429 or a bot-check page — verified, not assumed.
BROWSER_ENGINES = frozenset({"brave", "startpage"})

# Engines that need Chromium but launch their own, lazily, so that how the
# shared one is launched never depends on them.
OWN_BROWSER_ENGINES = frozenset({"google"})


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


class ResultList(list[Result]):
    """Results, plus a note the engine wants on its `ok` attempt.

    An answer can come with a fact the audit trail should carry even though
    nothing failed: Startpage made sought wait out a proof-of-work challenge
    (which explains a 40-second search), Google was reached with the automation
    flag hidden (which a reader of the results is entitled to know). Without
    this the chain records a bare `ok` and the fact is lost.

    A list subclass rather than a new return type, so every engine and test
    stub that returns a plain list keeps working, with no note.
    """

    def __init__(self, results: Iterable[Result] = (), *, detail: str = "") -> None:
        """Wrap results with the detail for their `ok` attempt."""
        super().__init__(results)
        self.detail = detail


@dataclass(frozen=True)
class Attempt:
    """The record of one engine being asked, successfully or not.

    Every engine consulted produces one of these, including the ones that
    failed. An empty result set with no attempts is the one output sought must
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

    `browser` is None for HTTP-only engines and for OWN_BROWSER_ENGINES, and
    non-None for anything in BROWSER_ENGINES.
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

# A token found only in the URL earns partial credit. A domain or a path
# segment containing the word is not the page discussing the subject, and
# counting it in full lets any result on `npmjs.com` vouch for "npm registry".
_URL_CREDIT = 0.4

# The best result must cover this weighted fraction of the query's tokens.
# Half is deliberately demanding; see detect_low_relevance for why erring
# toward flagging is the correct direction here.
_RELEVANCE_THRESHOLD = 0.5


def query_tokens(query: str) -> list[str]:
    """Significant lowercase tokens of a query, for relevance checking."""
    return [t for t in _TOKEN_RE.findall(query.lower()) if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS]


def _tokens(text: str) -> set[str]:
    """Tokenise arbitrary text the same way a query is tokenised."""
    return set(_TOKEN_RE.findall(text.lower()))


def relevance_score(tokens: list[str], result: Result) -> float:
    """Weighted fraction of the query's tokens that one result actually covers.

    Tokens are weighted by length as a cheap stand-in for rarity: covering
    "blorptangle" says far more than covering "app". Title and snippet count in
    full, the URL only partially.
    """
    wanted = set(tokens)
    if not wanted:
        return 1.0

    strong = _tokens(f"{result.title} {result.snippet}")
    weak = _tokens(result.url)

    total = 0.0
    covered = 0.0
    for token in wanted:
        weight = float(len(token))
        total += weight
        if token in strong:
            covered += weight
        elif token in weak:
            covered += weight * _URL_CREDIT
    return covered / total


def detect_low_relevance(query: str, results: list[Result]) -> bool:
    """Report whether even the best result fails to cover the query.

    Some engines answer a blocked request with a page of plausible but
    unrelated results — HTTP 200, well-formed, completely wrong. There is no
    error to catch, so this heuristic is the only signal, and it is
    deliberately biased toward false positives: a spurious warning costs a
    second glance, a missed one costs a wrong answer.

    Three things this must not do, each of which it used to:

    * Match substrings. `"rust" in "trustpilot"` and `"mail" in "hotmail"` are
      not mentions, and `"nat"` is not satisfied by "alternative". Both sides
      are tokenised and compared as whole words.
    * Union across the result set. Asking whether *any* result matched *any*
      token meant one hit anywhere disarmed the check for everything.
      Measured: the nonsense query "zqxjkvwmpf blorptangle 77 unfindable" came
      back unflagged because a single title happened to contain "unfindable"
      while both distinctive tokens matched nothing at all. Each result is now
      scored on its own and only the best one is judged.
    * Treat the URL as content. See `_URL_CREDIT`.

    The consequence worth stating plainly: a query of pure nonsense is flagged
    whatever the engine returns, because nothing can cover it.
    """
    tokens = query_tokens(query)
    if not tokens or not results:
        return False

    return max(relevance_score(tokens, r) for r in results) < _RELEVANCE_THRESHOLD

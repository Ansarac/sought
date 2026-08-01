"""The fallback chain: try engines in order, record every attempt.

The invariant this module exists to hold: sought never returns an empty result
set without an `attempts` list explaining what it asked and what came back.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from sought.browser import browser_session
from sought.engines import brave, marginalia, startpage, verticals
from sought.engines.base import (
    BROWSER_ENGINES,
    GENERAL_ENGINES,
    VERTICAL_SOURCES,
    Attempt,
    EngineFailure,
    Result,
    detect_low_relevance,
)
from sought.errors import UsageError
from sought.output import envelope

if TYPE_CHECKING:
    from sought.browser import Browser
    from sought.config import Config

_GENERAL = {
    "brave": brave.search,
    "startpage": startpage.search,
    "marginalia": marginalia.search,
}

# One spelling, used by both the chain and the single-source path. The engine
# the user named must appear in the audit trail whichever path asked for it.
_NO_BROWSER = "skipped: no browser session"


def run(query: str, *, config: Config, source: str | None = None) -> dict[str, Any]:
    """Search and return the audit envelope.

    With `source`, that one source is asked and there is no fallback — an
    explicit choice should not be silently swapped for something else.
    """
    if source is not None:
        return _single(query, config=config, source=source)
    return _chain(query, config=config)


def _single(query: str, *, config: Config, source: str) -> dict[str, Any]:
    attempts: list[Attempt] = []

    if source in VERTICAL_SOURCES:
        results = _attempt(attempts, source, lambda: verticals.search(source, query, config=config))
    elif source in GENERAL_ENGINES:
        with ExitStack() as stack:
            browser = _browser_or_none(stack, config, needed=source in BROWSER_ENGINES, attempts=attempts)
            if browser is None and source in BROWSER_ENGINES:
                attempts.append(Attempt(source, "error", _NO_BROWSER))
                results = []
            else:
                results = _attempt(attempts, source, lambda: _GENERAL[source](query, config=config, browser=browser))
    else:
        msg = f"unknown source {source!r}"
        raise UsageError(msg)

    return _finish(query, attempts, results)


def _chain(query: str, *, config: Config) -> dict[str, Any]:
    attempts: list[Attempt] = []
    order = [e for e in config.order if e in _GENERAL]
    if not order:
        msg = f"engines.order lists no known engine (known: {', '.join(GENERAL_ENGINES)})"
        raise UsageError(msg)

    with ExitStack() as stack:
        needs_browser = any(e in BROWSER_ENGINES for e in order)
        browser = _browser_or_none(stack, config, needed=needs_browser, attempts=attempts)

        for name in order:
            if browser is None and name in BROWSER_ENGINES:
                attempts.append(Attempt(name, "error", _NO_BROWSER))
                continue
            results = _attempt(attempts, name, lambda n=name: _GENERAL[n](query, config=config, browser=browser))
            if results:
                return _finish(query, attempts, results)

    return _finish(query, attempts, [])


def _browser_or_none(stack: ExitStack, config: Config, *, needed: bool, attempts: list[Attempt]) -> Browser | None:
    """Enter a browser session, or record why there isn't one.

    A missing Chromium disables two of the three engines, so it is recorded as
    an attempt rather than raised — marginalia can still answer.
    """
    if not needed:
        return None
    try:
        return stack.enter_context(browser_session(config))
    except EngineFailure as exc:
        attempts.append(Attempt("browser", exc.status, exc.detail))
    except Exception as exc:  # noqa: BLE001 — belt and braces; see below
        # browser_session already converts everything it can into an
        # EngineFailure. This second net exists because the cost of a miss is
        # out of all proportion to the cost of the net: an exception escaping
        # here aborts the chain with *zero* attempts recorded, which is both
        # the one output sought promises never to produce and — via Python's
        # default exit code of 1 — a silent claim that the web had no answer.
        attempts.append(Attempt("browser", "error", f"{exc.__class__.__name__}: {exc}"))
    return None


def _attempt(attempts: list[Attempt], name: str, call: Any) -> list[Result]:  # noqa: ANN401 — zero-arg callable
    """Run one engine and append its outcome to the audit trail.

    An engine that returns nothing is recorded as `empty`, never as `ok` with a
    count of zero. `_finish` credits the first `ok` attempt, so a zero-result
    `ok` would put one engine's name on another engine's results — "12 results
    via brave" under startpage's output. Every engine happens to raise
    EngineFailure("empty") today, but that was a convention repeated in four
    places; this makes it a property of the chain instead.
    """
    try:
        results = call()
    except EngineFailure as exc:
        attempts.append(Attempt(name, exc.status, exc.detail))
        return []
    except Exception as exc:  # noqa: BLE001 — one broken engine must not sink the chain
        attempts.append(Attempt(name, "error", f"{exc.__class__.__name__}: {exc}"))
        return []

    if not results:
        attempts.append(Attempt(name, "empty", "returned no results"))
        return []

    attempts.append(Attempt(name, "ok", count=len(results)))
    return results


def _finish(query: str, attempts: list[Attempt], results: list[Result]) -> dict[str, Any]:
    winner = next((a.engine for a in attempts if a.status == "ok"), None)
    return envelope(
        query=query,
        engine=winner,
        attempts=attempts,
        results=results,
        low_relevance=detect_low_relevance(query, results),
    )

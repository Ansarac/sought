"""The fallback chain: try engines in order, record every attempt.

The invariant this module exists to hold: sought never returns an empty result
set without an `attempts` list explaining what it asked and what came back.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Any, Literal

from sought import state
from sought.browser import browser_session
from sought.engines import brave, marginalia, startpage, verticals
from sought.engines.base import (
    BROWSER_ENGINES,
    GENERAL_ENGINES,
    VERTICAL_SOURCES,
    Attempt,
    EngineFailure,
    Result,
    ResultList,
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

# How `run` treats the result cache:
#   "use"     replay a recent identical search if there is one; cache the outcome
#   "refresh" always search (`--fresh`); still cache the outcome
#   "off"     neither read nor write (`doctor`: a replayed probe would claim an
#             engine works when it was not asked)
CacheMode = Literal["use", "refresh", "off"]


def run(query: str, *, config: Config, source: str | None = None, cache: CacheMode = "use") -> dict[str, Any]:
    """Search and return the audit envelope.

    With `source`, that one source is asked and there is no fallback — an
    explicit choice should not be silently swapped for something else.

    No cache mode bypasses engine cooldowns. Asking a refusing engine again
    early is exactly the load the cooldown exists to prevent.
    """
    key = state.result_key(query, source=source, order=config.order, limit=config.limit)
    if cache == "use":
        hit = state.load_result(key, ttl=config.cache_ttl_seconds)
        if hit is not None:
            return hit

    payload = _chain(query, config=config) if source is None else _single(query, config=config, source=source)

    if cache != "off":
        state.store_result(key, payload, ttl=config.cache_ttl_seconds)
    return payload


def _single(query: str, *, config: Config, source: str) -> dict[str, Any]:
    attempts: list[Attempt] = []

    if source in VERTICAL_SOURCES:
        results = _attempt(attempts, source, lambda: verticals.search(source, query, config=config))
    elif source in GENERAL_ENGINES:
        cooling = state.cooling_down([source], config=config)
        if source in cooling:
            attempts.append(cooling[source])
            return _finish(query, attempts, [])
        with ExitStack() as stack:
            browser = _browser_or_none(stack, config, needed=source in BROWSER_ENGINES, attempts=attempts)
            if browser is None and source in BROWSER_ENGINES:
                attempts.append(Attempt(source, "error", _NO_BROWSER))
                results = []
            else:
                results = _ask(attempts, source, query, config=config, browser=browser)
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

    cooling = state.cooling_down(order, config=config)
    with ExitStack() as stack:
        # Chromium is the expensive part of a search; launching it only to
        # skip every engine that would use it is pure cost.
        needs_browser = any(e in BROWSER_ENGINES and e not in cooling for e in order)
        browser = _browser_or_none(stack, config, needed=needs_browser, attempts=attempts)

        for name in order:
            if name in cooling:
                attempts.append(cooling[name])
                continue
            if browser is None and name in BROWSER_ENGINES:
                attempts.append(Attempt(name, "error", _NO_BROWSER))
                continue
            results = _ask(attempts, name, query, config=config, browser=browser)
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


def _ask(attempts: list[Attempt], name: str, query: str, *, config: Config, browser: Browser | None) -> list[Result]:
    """Ask one general engine for real, and let its outcome update the cooldown state."""
    results = _attempt(attempts, name, lambda: _GENERAL[name](query, config=config, browser=browser))
    state.note_outcome(attempts[-1], config=config)
    return results


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

    detail = results.detail if isinstance(results, ResultList) else ""
    attempts.append(Attempt(name, "ok", detail, count=len(results)))
    return list(results)


def _finish(query: str, attempts: list[Attempt], results: list[Result]) -> dict[str, Any]:
    winner = next((a.engine for a in attempts if a.status == "ok"), None)
    return envelope(
        query=query,
        engine=winner,
        attempts=attempts,
        results=results,
        low_relevance=detect_low_relevance(query, results),
    )

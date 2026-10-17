"""Playwright lifecycle: one Chromium instance shared by the whole fallback chain.

Launching Chromium costs about a second, so the chain opens it once and every
browser engine borrows a page from the same context. The one exception is the
opt-in `google` engine, which launches a separate Chromium with the automation
flag hidden (`hide_automation`) so that nothing about how `brave` and
`startpage` are launched changes when it is switched on.

Also the home of `BOT_WALL_MARKERS`, the list of phrases that mean "blocked"
on a page that arrived with an HTTP 200. That list is shared with the HTTP-only
engines (see `classify_dead_page`) rather than copied, because two copies of it
would drift and the second copy would be the one nobody notices going stale.
"""

from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager, suppress
from typing import TYPE_CHECKING, Any

from sought.engines.base import EngineFailure
from sought.net import DEFAULT_UA, environment_proxy, proxy_bypass_list

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sought.config import Config

# Startpage's JavaScript proof-of-work interstitial ("Verifying your request...
# Calculating... Difficulty: 6"). Unlike every other marker it resolves itself:
# the page's own script computes the answer and moves on to the results, the
# same as in any visitor's browser. Measured headless, with nothing spoofed:
# 39 s. `goto` waits for it when asked to; still showing at the end of the
# wait, it is a refusal like the rest.
POW_CHALLENGE_MARKER = "verifying your request"

# Phrases that mean "we know you are a bot", as served with an HTTP 200.
BOT_WALL_MARKERS = (
    "verifying you're not a bot",
    "verify you are human",
    "unusual traffic",
    "captcha",
    "access denied",
    # Marginalia's own throttle page, caught live while fixing this. Worth
    # spelling out because it is the engine that survives when Chromium is
    # gone, so a wrong `empty` from it is the last word the user gets.
    "queries from bots",
    "wait for a moment",
    # Startpage's proof-of-work interstitial, served with a 200 and an empty
    # title. Caught live: until this was listed, it came out as `empty` and
    # exit 1, "the web has no answer", for a query nobody ever ran.
    POW_CHALLENGE_MARKER,
)


class Browser:
    """A live Chromium context. Create it through `browser_session`."""

    def __init__(self, context: Any, timeout_ms: int) -> None:  # noqa: ANN401 — Playwright type, imported lazily
        """Wrap a live Playwright browser context."""
        self._context = context
        self.timeout_ms = timeout_ms

    @contextmanager
    def page(self) -> Iterator[Any]:
        """Open a page and always close it, even when the engine raises."""
        page = self._context.new_page()
        try:
            yield page
        finally:
            page.close()


# What `hide_automation` adds to the launch. Chromium sets
# `navigator.webdriver = true` under automation, and Google refuses such a
# browser per request (docs/findings/2026-10-egress-google-windows.md, §3-4).
# This switch makes it report false. It is fingerprint evasion, used by the
# opt-in `google` engine only, never by the chain's shared browser.
HIDE_AUTOMATION_FLAG = "--disable-blink-features=AutomationControlled"


@contextmanager
def browser_session(config: Config, *, hide_automation: bool = False) -> Iterator[Browser]:
    """Launch Chromium for the duration of the block.

    `hide_automation` is for the opt-in `google` engine and nothing else: it
    adds `HIDE_AUTOMATION_FLAG` to the launch and opens the context without a
    fixed viewport, the combination measured to get past Google's
    per-request check. Everything else — proxy, certificates, user agent,
    timeouts, teardown — is the same code path, so the explicit-proxy fix
    cannot be forgotten on one side.

    Every step of acquisition — importing Playwright, starting its driver,
    launching the browser, opening a context — is reported as
    EngineFailure("error"), never as a bare exception. A bare one escaping here
    aborts the chain before a single Attempt is recorded and, because Python's
    default exit code is 1, comes out indistinguishable from "searched fine,
    found nothing". Marginalia needs no browser and must still get its turn.

    Teardown is best-effort and each step is independent. Playwright's
    `BrowserContext.close()` does not swallow TargetClosedError while
    `Browser.close()` does, so a Chromium that died mid-search used to skip the
    remaining teardown and throw *after* a good result set had already been
    assembled.
    """
    with ExitStack() as stack:
        try:
            context = _acquire(stack, config, hide_automation=hide_automation)
        except EngineFailure:
            raise
        except Exception as exc:  # Playwright raises many types; none of them may escape
            msg = f"cannot start a browser: {_first_line(exc)} (try: sought doctor --fix)"
            raise EngineFailure("error", msg) from exc

        yield Browser(context, config.timeout_seconds * 1000)


def _acquire(stack: ExitStack, config: Config, *, hide_automation: bool) -> Any:  # noqa: ANN401 — BrowserContext
    """Start Playwright and open a context, registering each teardown as it goes.

    Teardown is registered step by step so that a failure halfway through
    unwinds exactly what was built, and `_close_quietly` keeps each step from
    stopping the next.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover — dependency is declared
        msg = "the playwright package is not installed"
        raise EngineFailure("error", msg) from exc

    pw = sync_playwright().start()
    stack.callback(_close_quietly, pw.stop)

    try:
        browser = pw.chromium.launch(**_launch_kwargs(config, hide_automation=hide_automation))
    except Exception as exc:  # Playwright raises many types for "no browser"
        msg = f"cannot launch chromium: {_first_line(exc)} (try: sought doctor --fix)"
        raise EngineFailure("error", msg) from exc
    stack.callback(_close_quietly, browser.close)

    # Python's `viewport=None` means "the default 1280x720", not "none":
    # `no_viewport` is the binding's spelling of the JS `viewport: null` that
    # was measured. A fixed-viewport context was the second, weaker signal
    # Google reacted to (findings §4).
    screen: dict[str, Any] = {"no_viewport": True} if hide_automation else {"viewport": {"width": 1280, "height": 900}}
    context = browser.new_context(
        ignore_https_errors=config.browser_ignore_cert_errors,
        user_agent=DEFAULT_UA,
        **screen,
    )
    stack.callback(_close_quietly, context.close)

    context.set_default_timeout(config.timeout_seconds * 1000)
    return context


def _launch_kwargs(config: Config, *, hide_automation: bool = False) -> dict[str, Any]:
    launch_args = ["--no-sandbox"]
    if config.browser_ignore_cert_errors:
        launch_args.append("--ignore-certificate-errors")
    if hide_automation:
        launch_args.append(HIDE_AUTOMATION_FLAG)

    kwargs: dict[str, Any] = {"headless": True, "args": launch_args}
    proxy_server = config.proxy.browser_server() or environment_proxy()
    if proxy_server:
        # Always explicit, even when the proxy only comes from the environment:
        # Chromium does not reliably read the environment (see
        # `environment_proxy`), and `requests` does. Passing it here makes both
        # transports take the same route, with the config file authoritative.
        proxy: dict[str, str] = {"server": proxy_server}
        bypass = proxy_bypass_list(config)
        if bypass:
            # `--proxy-server` supersedes the environment wholesale, NO_PROXY
            # included, so the bypass list has to travel with it.
            proxy["bypass"] = bypass
        kwargs["proxy"] = proxy
    return kwargs


def _close_quietly(close: Callable[[], object]) -> None:
    """Run one teardown step, swallowing whatever it raises.

    Nothing useful can be done with a failure to close something that is
    already gone, and letting it out would discard results the caller has
    already earned.
    """
    with suppress(Exception):  # teardown diagnostics are not worth a lost result set
        close()


SELECTOR_TIMEOUT_MS = 15_000
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_BAD_REQUEST = 400


def goto(  # noqa: PLR0913 — each keyword is one engine's need, all optional
    page: Any,  # noqa: ANN401 — Playwright page
    url: str,
    *,
    wait_for: str,
    selector_timeout_ms: int = SELECTOR_TIMEOUT_MS,
    challenge_wait_ms: int = 0,
    on_arrival: Callable[[Any], None] | None = None,
) -> float | None:
    """Navigate and wait for the results container, classifying every failure.

    `on_arrival` sees the page before its HTTP status is judged, for an engine
    whose walls are best recognised by where they land (Google's `/sorry/`
    arrives as a 429, and "HTTP 429" would hide which wall it was). It raises
    EngineFailure to classify, or returns to carry on.

    With `challenge_wait_ms`, a proof-of-work interstitial (`POW_CHALLENGE_MARKER`)
    extends the wait to that many milliseconds from arrival, instead of being
    reported as a bot wall the moment `selector_timeout_ms` runs out. Returns
    the seconds spent getting past one, or None when there was none.

    One navigation, then waiting. Nothing here reloads or re-requests:
    `wait_for_selector` keeps polling across the navigation the challenge
    script performs when it is done (Playwright retries the query whenever the
    execution context is replaced), so the results page is picked up wherever
    it comes from. The explicit timeouts also override the context default
    (`engines.timeout_seconds`), which would otherwise cut the wait short.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        response = page.goto(url, wait_until="domcontentloaded")
    except PlaywrightTimeout as exc:
        raise EngineFailure("timeout", "navigation timed out") from exc
    except PlaywrightError as exc:
        raise EngineFailure("blocked", _first_line(exc)) from exc

    if on_arrival is not None:
        on_arrival(page)

    status = response.status if response is not None else 0
    if status == _HTTP_TOO_MANY_REQUESTS:
        raise EngineFailure("rate_limited", "HTTP 429")
    if status >= _HTTP_BAD_REQUEST:
        raise EngineFailure("blocked", f"HTTP {status}")

    arrived = time.monotonic()
    # Recognised up front when it is already in the served HTML, so the whole
    # allowance goes to it. A challenge that only renders later is caught
    # below, once the ordinary wait has run out.
    challenged = challenge_wait_ms > 0 and _shows_challenge(page)
    try:
        page.wait_for_selector(wait_for, timeout=challenge_wait_ms if challenged else selector_timeout_ms)
    except PlaywrightTimeout as exc:
        remaining_ms = challenge_wait_ms - int((time.monotonic() - arrived) * 1000)
        if challenged or remaining_ms <= 0 or not _shows_challenge(page):
            # No results container. Distinguish a bot wall from a genuinely
            # empty page before reporting, because they need opposite responses.
            raise _classify_stuck_page(page, challenge_wait_ms=challenge_wait_ms) from exc
        challenged = True
        try:
            page.wait_for_selector(wait_for, timeout=remaining_ms)
        except PlaywrightTimeout as again:
            raise _classify_stuck_page(page, challenge_wait_ms=challenge_wait_ms) from again

    return time.monotonic() - arrived if challenged else None


def extract(page: Any, script: str, arg: Any, *, timeout_ms: int) -> Any:  # noqa: ANN401 — Playwright page and JSON
    """Run an extraction script against the page under a deadline.

    `page.evaluate` is governed by no timeout at all — not the one
    `set_default_timeout` installs, not any argument it accepts — so a page
    whose main thread is wedged (an anti-bot script spinning, usually) hangs
    the process with no way out. `wait_for_function` takes the same script and
    the same argument and *is* bounded by the driver, and every `_EXTRACT` here
    returns an array, which is truthy even when empty, so it resolves on the
    first poll rather than waiting for anything.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        handle = page.wait_for_function(script, arg=arg, timeout=timeout_ms)
    except PlaywrightTimeout as exc:
        raise EngineFailure("timeout", "the page never ran the extraction script") from exc

    try:
        return handle.json_value()
    finally:
        _close_quietly(handle.dispose)


def classify_dead_page(body: str, *, empty_detail: str) -> EngineFailure:
    """Decide whether a page that yielded nothing was blocked or genuinely empty.

    Shared by the browser engines and the HTTP-only ones, because the failure
    mode is the same on both: a proxy interstitial or a bot wall arrives with a
    perfectly ordinary HTTP 200, parses to zero results, and would otherwise be
    reported as `empty` — "the web has no answer" — when sought never got to
    ask. `empty` and `rate_limited` demand opposite responses from the caller,
    so this is exactly the distinction that must not collapse.

    Only ever called once a page has already produced no results, which is what
    makes searching the body for "captcha" safe: a real results page for the
    query "captcha bypass" never reaches here.
    """
    lowered = body.lower()
    for marker in BOT_WALL_MARKERS:
        if marker in lowered:
            return EngineFailure("rate_limited", f"bot wall: {marker!r}")

    return EngineFailure("empty", empty_detail)


def _classify_stuck_page(page: Any, *, challenge_wait_ms: int = 0) -> EngineFailure:  # noqa: ANN401 — Playwright page
    """Decide why the results container never appeared.

    A proof-of-work challenge sought was told to wait for, and did, gets its
    own detail: "bot wall" alone would hide that sought gave it the full
    allowance, which is the difference between "raise the limit" and "this
    engine is refusing you".
    """
    body = _body_text(page)
    if challenge_wait_ms > 0 and POW_CHALLENGE_MARKER in body.lower():
        detail = f"proof-of-work challenge not completed within {challenge_wait_ms // 1000}s"
        return EngineFailure("rate_limited", detail)
    return classify_dead_page(body, empty_detail="results container never rendered")


def _shows_challenge(page: Any) -> bool:  # noqa: ANN401 — Playwright page
    return POW_CHALLENGE_MARKER in _body_text(page).lower()


def _body_text(page: Any) -> str:  # noqa: ANN401 — Playwright page
    """Read the page's visible text, or "" when it cannot be read. Diagnostics must not raise."""
    try:
        return page.inner_text("body") or ""
    except Exception:  # noqa: BLE001 — diagnostics must not raise
        return ""


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__

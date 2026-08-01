"""Playwright lifecycle: one Chromium instance shared by the whole fallback chain.

Launching Chromium costs about a second, so the chain opens it once and every
browser engine borrows a page from the same context.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from scout.engines.base import EngineFailure
from scout.net import DEFAULT_UA

if TYPE_CHECKING:
    from collections.abc import Iterator

    from scout.config import Config

# Phrases that mean "we know you are a bot", as served with an HTTP 200.
BOT_WALL_MARKERS = (
    "verifying you're not a bot",
    "verify you are human",
    "unusual traffic",
    "captcha",
    "access denied",
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


@contextmanager
def browser_session(config: Config) -> Iterator[Browser]:
    """Launch Chromium for the duration of the block.

    Raises EngineFailure("error") when Playwright or its browser binary is
    missing, so the chain records the reason instead of crashing.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover — dependency is declared
        msg = "the playwright package is not installed"
        raise EngineFailure("error", msg) from exc

    launch_args = ["--no-sandbox"]
    if config.browser_ignore_cert_errors:
        launch_args.append("--ignore-certificate-errors")

    launch_kwargs: dict[str, Any] = {"headless": True, "args": launch_args}
    proxy_server = config.proxy.browser_server()
    if proxy_server:
        launch_kwargs["proxy"] = {"server": proxy_server}

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(**launch_kwargs)
        except Exception as exc:  # Playwright raises many types for "no browser"
            msg = f"cannot launch chromium: {_first_line(exc)} (try: scout doctor --fix)"
            raise EngineFailure("error", msg) from exc

        context = browser.new_context(
            ignore_https_errors=config.browser_ignore_cert_errors,
            user_agent=DEFAULT_UA,
            viewport={"width": 1280, "height": 900},
        )
        context.set_default_timeout(config.timeout_seconds * 1000)
        try:
            yield Browser(context, config.timeout_seconds * 1000)
        finally:
            context.close()
            browser.close()


SELECTOR_TIMEOUT_MS = 15_000
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_BAD_REQUEST = 400


def goto(page: Any, url: str, *, wait_for: str, selector_timeout_ms: int = SELECTOR_TIMEOUT_MS) -> None:  # noqa: ANN401
    """Navigate and wait for the results container, classifying every failure."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        response = page.goto(url, wait_until="domcontentloaded")
    except PlaywrightTimeout as exc:
        raise EngineFailure("timeout", "navigation timed out") from exc
    except PlaywrightError as exc:
        raise EngineFailure("blocked", _first_line(exc)) from exc

    status = response.status if response is not None else 0
    if status == _HTTP_TOO_MANY_REQUESTS:
        raise EngineFailure("rate_limited", "HTTP 429")
    if status >= _HTTP_BAD_REQUEST:
        raise EngineFailure("blocked", f"HTTP {status}")

    try:
        page.wait_for_selector(wait_for, timeout=selector_timeout_ms)
    except PlaywrightTimeout as exc:
        # No results container. Distinguish a bot wall from a genuinely empty
        # page before reporting, because they need opposite responses.
        raise _classify_stuck_page(page) from exc


def _classify_stuck_page(page: Any) -> EngineFailure:  # noqa: ANN401 — Playwright page
    """Decide why the results container never appeared."""
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:  # noqa: BLE001 — diagnostics must not raise
        body = ""

    for marker in BOT_WALL_MARKERS:
        if marker in body:
            return EngineFailure("rate_limited", f"bot wall: {marker!r}")

    return EngineFailure("empty", "results container never rendered")


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__

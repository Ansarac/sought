"""Waiting out Startpage's proof-of-work challenge.

The interstitial ("Verifying your request... Calculating... Difficulty: 6") is
a script the browser completes by itself. Measured headless, nothing spoofed:
the results page appeared after 39 s. sought used to give up at 15 s and call
it a bot wall — a refusal that was never made. These tests pin down the three
outcomes: the challenge clears (ok, and the wait is on the record), it never
clears (rate_limited, saying sought waited), and there is none (unchanged).

Time is simulated: the fake page moves a fake clock by exactly as long as each
wait would have blocked.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from sought import browser as browser_mod
from sought import search as search_mod
from sought import state
from sought.config import Config
from sought.engines import startpage
from sought.engines.base import EngineFailure, ResultList

CHALLENGE = "Verifying your request...\n\nCalculating...\nDifficulty: 6, Speed: 316.537kH/s"
RESULTS = "Search Results\nPlaywright — fast and reliable end-to-end testing"
ROW = {"title": "Playwright", "url": "https://playwright.dev", "snippet": "end-to-end testing"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(browser_mod, "time", SimpleNamespace(monotonic=c.monotonic))
    return c


class FakeHandle:
    def __init__(self, value) -> None:
        self.value = value

    def json_value(self):
        return self.value

    def dispose(self):
        pass


class ChallengePage:
    """A page that shows `body_at(t)` and grows its results container at `clears_at`.

    `t` is seconds since arrival. `clears_at=None` means the container never
    appears. Every `goto` is recorded, so a test can prove there was exactly
    one navigation.
    """

    def __init__(self, clock, *, body_at, clears_at=None, title="Startpage Search Results", rows=(ROW,)) -> None:
        self.clock = clock
        self.body_at = body_at
        self.clears_at = clears_at
        self._title = title
        self.rows = list(rows)
        self.gotos: list[str] = []
        self.waits: list[int] = []
        self.arrived = None

    def goto(self, url, *, wait_until):
        self.gotos.append(url)
        self.arrived = self.clock.now
        return SimpleNamespace(status=200)

    def _t(self):
        return self.clock.now - self.arrived

    def inner_text(self, _selector):
        return self.body_at(self._t())

    def wait_for_selector(self, _selector, *, timeout):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        self.waits.append(timeout)
        deadline = self._t() + timeout / 1000
        if self.clears_at is not None and self.clears_at <= deadline:
            self.clock.now = self.arrived + max(self._t(), self.clears_at)
            return object()
        self.clock.now = self.arrived + deadline
        raise PlaywrightTimeout(f"Timeout {timeout}ms exceeded.")

    def title(self):
        return self._title

    def wait_for_function(self, _script, *, arg=None, timeout=None):
        return FakeHandle(self.rows)


def challenge_until(seconds):
    """Body: the interstitial until `seconds`, the results page after."""
    return lambda t: CHALLENGE if t < seconds else RESULTS


def always(text):
    return lambda _t: text


class FakeBrowser:
    def __init__(self, page) -> None:
        self._page = page
        self.timeout_ms = 30_000

    @contextmanager
    def page(self):
        yield self._page


# --- goto -------------------------------------------------------------------


def test_a_challenge_that_clears_is_waited_out_and_timed(clock):
    page = ChallengePage(clock, body_at=challenge_until(39), clears_at=39)

    waited = browser_mod.goto(page, "https://sp.example/", wait_for=".w-gl", challenge_wait_ms=60_000)

    assert waited == pytest.approx(39)
    # Recognised on arrival, so the whole allowance went to it in one wait.
    assert page.waits == [60_000]
    assert len(page.gotos) == 1


def test_a_challenge_that_never_clears_says_sought_waited(clock):
    page = ChallengePage(clock, body_at=always(CHALLENGE), clears_at=None)

    with pytest.raises(EngineFailure) as exc:
        browser_mod.goto(page, "https://sp.example/", wait_for=".w-gl", challenge_wait_ms=60_000)

    assert exc.value.status == "rate_limited"
    assert exc.value.detail == "proof-of-work challenge not completed within 60s"
    assert len(page.gotos) == 1  # no reload, no retry


def test_a_challenge_that_renders_late_is_still_waited_out(clock):
    """The marker may come from script, after domcontentloaded. The allowance counts from arrival."""
    page = ChallengePage(clock, body_at=lambda t: "" if t < 1 else CHALLENGE if t < 39 else RESULTS, clears_at=39)

    waited = browser_mod.goto(page, "https://sp.example/", wait_for=".w-gl", challenge_wait_ms=60_000)

    assert waited == pytest.approx(39)
    assert page.waits == [browser_mod.SELECTOR_TIMEOUT_MS, 60_000 - browser_mod.SELECTOR_TIMEOUT_MS]


def test_no_challenge_means_the_old_wait_and_no_note(clock):
    page = ChallengePage(clock, body_at=always(RESULTS), clears_at=2)

    assert browser_mod.goto(page, "https://sp.example/", wait_for=".w-gl", challenge_wait_ms=60_000) is None
    assert page.waits == [browser_mod.SELECTOR_TIMEOUT_MS]


def test_no_challenge_and_no_container_is_an_unrecognised_page(clock):
    """Not `empty`: a missing container is sought not understanding the page (Brave, 2026-10)."""
    page = ChallengePage(clock, body_at=always("Nothing here."), clears_at=None)

    with pytest.raises(EngineFailure) as exc:
        browser_mod.goto(page, "https://sp.example/", wait_for=".w-gl", challenge_wait_ms=60_000)

    assert exc.value.status == "error"
    assert "page not recognised" in exc.value.detail
    assert page.waits == [browser_mod.SELECTOR_TIMEOUT_MS]


def test_without_an_allowance_the_challenge_is_a_bot_wall_as_before(clock):
    """Brave passes no allowance; its behaviour must not change."""
    page = ChallengePage(clock, body_at=always(CHALLENGE), clears_at=39)

    with pytest.raises(EngineFailure) as exc:
        browser_mod.goto(page, "https://sp.example/", wait_for=".w-gl")

    assert exc.value.status == "rate_limited"
    assert exc.value.detail == "bot wall: 'verifying your request'"
    assert page.waits == [browser_mod.SELECTOR_TIMEOUT_MS]


def test_a_challenge_that_turns_into_a_captcha_is_reported_as_the_captcha(clock):
    page = ChallengePage(clock, body_at=lambda t: CHALLENGE if t < 20 else "Please solve this CAPTCHA", clears_at=None)

    with pytest.raises(EngineFailure) as exc:
        browser_mod.goto(page, "https://sp.example/", wait_for=".w-gl", challenge_wait_ms=60_000)

    assert exc.value.status == "rate_limited"
    assert exc.value.detail == "bot wall: 'captcha'"


# --- the startpage engine ---------------------------------------------------


def test_startpage_puts_the_wait_on_its_ok_attempt(clock):
    page = ChallengePage(clock, body_at=challenge_until(39), clears_at=39)

    results = startpage.search("playwright", config=Config(), browser=FakeBrowser(page))

    assert isinstance(results, ResultList)
    assert results.detail == "passed proof-of-work challenge in 39s"
    assert [r.url for r in results] == ["https://playwright.dev"]


def test_startpage_without_a_challenge_has_no_note(clock):
    page = ChallengePage(clock, body_at=always(RESULTS), clears_at=1)

    results = startpage.search("playwright", config=Config(), browser=FakeBrowser(page))

    assert results.detail == ""


def test_the_allowance_comes_from_the_config(clock):
    page = ChallengePage(clock, body_at=always(CHALLENGE), clears_at=None)

    with pytest.raises(EngineFailure) as exc:
        startpage.search("playwright", config=Config(challenge_wait_seconds=90), browser=FakeBrowser(page))

    assert page.waits == [90_000]
    assert exc.value.detail == "proof-of-work challenge not completed within 90s"


def test_the_title_is_judged_on_the_page_after_the_wait(clock):
    """The challenge page's own (empty) title must not be what is checked — nor may a bad final one pass."""
    page = ChallengePage(clock, body_at=challenge_until(39), clears_at=39, title="Access check")

    with pytest.raises(EngineFailure) as exc:
        startpage.search("playwright", config=Config(), browser=FakeBrowser(page))

    assert exc.value.status == "blocked"
    assert "Access check" in exc.value.detail


@pytest.fixture
def chain_with(monkeypatch):
    """Run the real chain with startpage on a fake page."""

    def install(page):
        monkeypatch.setattr(
            search_mod,
            "_browser_or_none",
            lambda stack, config, *, needed, attempts: FakeBrowser(page),  # noqa: ARG005
        )

    return install


def test_the_audit_trail_records_the_passed_challenge(clock, chain_with):
    chain_with(ChallengePage(clock, body_at=challenge_until(39), clears_at=39))

    out = search_mod.run("playwright", config=Config(), source="startpage")

    assert out["attempts"] == [
        {"engine": "startpage", "status": "ok", "count": 1, "detail": "passed proof-of-work challenge in 39s"}
    ]
    assert out["engine"] == "startpage"


def test_the_audit_trail_records_a_challenge_that_never_cleared_and_cools_down(clock, chain_with):
    chain_with(ChallengePage(clock, body_at=always(CHALLENGE), clears_at=None))

    out = search_mod.run("playwright", config=Config(), source="startpage")

    assert out["attempts"] == [
        {"engine": "startpage", "status": "rate_limited", "detail": "proof-of-work challenge not completed within 60s"}
    ]
    assert "startpage" in state.cooling_down(["startpage"], config=Config())


def test_an_unchanged_search_has_an_unchanged_attempt(clock, chain_with):
    chain_with(ChallengePage(clock, body_at=always(RESULTS), clears_at=1))

    out = search_mod.run("playwright", config=Config(), source="startpage")

    assert out["attempts"] == [{"engine": "startpage", "status": "ok", "count": 1}]

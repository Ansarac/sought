"""Getting a browser, and giving it back.

Two failures live here, and both are worse than they look. Acquisition that
raises anything other than EngineFailure aborts the chain with an empty audit
trail — the one output sought promises never to produce — and comes out of
Python with exit code 1, which already means "searched fine, found nothing".
Teardown that raises throws away a result set that was already assembled.

Playwright is faked throughout: none of this needs a real Chromium, and a test
that needs one would not run on the network this tool exists for.
"""

from __future__ import annotations

import pytest

from sought import browser as browser_mod
from sought.config import Config, ProxyConfig
from sought.engines.base import EngineFailure


class FakeContext:
    def __init__(self, *, close_raises: Exception | None = None) -> None:
        self.closed = False
        self.timeout_ms = None
        self._close_raises = close_raises

    def set_default_timeout(self, ms):
        self.timeout_ms = ms

    def close(self):
        self.closed = True
        if self._close_raises is not None:
            raise self._close_raises


class FakeBrowser:
    def __init__(self, context) -> None:
        self.context = context
        self.closed = False
        self.new_context_kwargs = None

    def new_context(self, **kwargs):
        self.new_context_kwargs = kwargs
        return self.context

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser, launch_raises=None) -> None:
        self._browser = browser
        self._launch_raises = launch_raises
        self.launch_kwargs = None

    def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        if self._launch_raises is not None:
            raise self._launch_raises
        return self._browser


class FakePlaywright:
    def __init__(self, chromium) -> None:
        self.chromium = chromium
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeDriver:
    """What `sync_playwright()` returns — a thing you `.start()`."""

    def __init__(self, pw, start_raises=None) -> None:
        self._pw = pw
        self._start_raises = start_raises

    def start(self):
        if self._start_raises is not None:
            raise self._start_raises
        return self._pw


@pytest.fixture
def playwright(monkeypatch):
    """Install a fake Playwright and hand back the pieces to assert against."""
    import playwright.sync_api

    def install(*, start_raises=None, launch_raises=None, context=None):
        ctx = context if context is not None else FakeContext()
        fake_browser = FakeBrowser(ctx)
        chromium = FakeChromium(fake_browser, launch_raises=launch_raises)
        pw = FakePlaywright(chromium)
        driver = FakeDriver(pw, start_raises=start_raises)
        monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: driver)
        return pw, fake_browser, ctx

    return install


def test_a_healthy_session_yields_a_browser_and_tears_it_all_down(playwright):
    pw, fake_browser, ctx = playwright()

    with browser_mod.browser_session(Config(timeout_seconds=7)) as b:
        assert b.timeout_ms == 7000
        assert ctx.timeout_ms == 7000

    assert (ctx.closed, fake_browser.closed, pw.stopped) == (True, True, True)


@pytest.mark.parametrize("boom", [RuntimeError("driver died"), OSError("no such file")])
def test_a_driver_that_will_not_start_is_an_engine_failure_not_a_traceback(playwright, boom):
    """Nothing may escape acquisition but EngineFailure.

    `sync_playwright().start()` sat outside every guard, so a RuntimeError here
    used to abort the chain with an empty audit trail.
    """
    playwright(start_raises=boom)

    with pytest.raises(EngineFailure) as exc, browser_mod.browser_session(Config()):
        pass  # pragma: no cover — the context manager never opens

    assert exc.value.status == "error"
    assert "cannot start a browser" in exc.value.detail


def test_a_chromium_that_will_not_launch_keeps_its_specific_remedy(playwright):
    playwright(launch_raises=RuntimeError("Executable doesn't exist"))

    with pytest.raises(EngineFailure) as exc, browser_mod.browser_session(Config()):
        pass  # pragma: no cover

    assert exc.value.status == "error"
    assert "cannot launch chromium" in exc.value.detail
    assert "doctor --fix" in exc.value.detail


def test_a_context_that_will_not_open_is_an_engine_failure(playwright):
    pw, fake_browser, _ctx = playwright()
    fake_browser.new_context = lambda **_kw: (_ for _ in ()).throw(RuntimeError("no context"))

    with pytest.raises(EngineFailure) as exc, browser_mod.browser_session(Config()):
        pass  # pragma: no cover

    assert exc.value.status == "error"
    # Everything built before the failure is still unwound.
    assert (fake_browser.closed, pw.stopped) == (True, True)


def test_a_failing_set_default_timeout_is_an_engine_failure(playwright):
    class Hostile(FakeContext):
        def set_default_timeout(self, ms):
            raise RuntimeError("connection closed")

    pw, fake_browser, ctx = playwright(context=Hostile())

    with pytest.raises(EngineFailure) as exc, browser_mod.browser_session(Config()):
        pass  # pragma: no cover

    assert exc.value.status == "error"
    assert (ctx.closed, fake_browser.closed, pw.stopped) == (True, True, True)


def test_a_context_that_will_not_close_does_not_discard_the_results(playwright):
    """The measured teardown bug.

    `BrowserContext.close()` does not swallow TargetClosedError while
    `Browser.close()` does, so a Chromium that died mid-search raised out of
    the ExitStack *after* a good envelope had already been computed.
    """
    from playwright.sync_api import Error as PlaywrightError

    pw, fake_browser, ctx = playwright(context=FakeContext(close_raises=PlaywrightError("Target page closed")))

    with browser_mod.browser_session(Config()) as b:
        assert b is not None

    # No exception, and the later steps were not skipped by the earlier failure.
    assert (ctx.closed, fake_browser.closed, pw.stopped) == (True, True, True)


def test_teardown_steps_are_independent_of_each_other(playwright):
    pw, fake_browser, ctx = playwright(context=FakeContext(close_raises=RuntimeError("boom")))
    fake_browser.close = lambda: (_ for _ in ()).throw(RuntimeError("boom too"))

    with browser_mod.browser_session(Config()):
        pass

    assert ctx.closed is True
    assert pw.stopped is True  # reached despite two failures ahead of it


def test_an_exception_from_the_body_is_not_swallowed(playwright):
    """Teardown tolerance must not become "the search cannot fail"."""
    playwright()

    with pytest.raises(ValueError, match="engine bug"), browser_mod.browser_session(Config()):
        raise ValueError("engine bug")


def test_the_proxy_and_its_bypass_list_are_handed_to_chromium(playwright, monkeypatch):
    """The bypass list has to travel with the proxy.

    `--proxy-server` supersedes the proxy environment wholesale, NO_PROXY
    included.
    """
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    pw, _b, _c = playwright()
    cfg = Config(proxy=ProxyConfig(https="http://proxy.example:8080", no_proxy=("localhost", "127.0.0.1")))

    with browser_mod.browser_session(cfg):
        pass

    assert pw.chromium.launch_kwargs["proxy"] == {
        "server": "http://proxy.example:8080",
        "bypass": "localhost,127.0.0.1",
    }


_PROXY_VARS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")


@pytest.fixture
def clean_proxy_env(monkeypatch):
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_no_proxy_anywhere_means_no_proxy_argument(playwright, clean_proxy_env):
    pw, _b, _c = playwright()

    with browser_mod.browser_session(Config()):
        pass

    assert "proxy" not in pw.chromium.launch_kwargs


def test_an_environment_proxy_is_handed_to_chromium_explicitly(playwright, clean_proxy_env):
    """Chromium under a GNOME session ignores HTTPS_PROXY and goes direct.

    Measured behind a Kerberos firewall: requests used the proxy, Chromium did
    not, and about half its navigations came back HTTP 401.
    """
    clean_proxy_env.setenv("HTTPS_PROXY", "http://env-proxy.example:3128")
    clean_proxy_env.setenv("NO_PROXY", "localhost")
    pw, _b, _c = playwright()

    with browser_mod.browser_session(Config()):
        pass

    assert pw.chromium.launch_kwargs["proxy"] == {"server": "http://env-proxy.example:3128", "bypass": "localhost"}


def test_the_config_proxy_beats_the_environment_one(playwright, clean_proxy_env):
    clean_proxy_env.setenv("HTTPS_PROXY", "http://env-proxy.example:3128")
    pw, _b, _c = playwright()

    with browser_mod.browser_session(Config(proxy=ProxyConfig(https="http://cfg-proxy.example:8080"))):
        pass

    assert pw.chromium.launch_kwargs["proxy"]["server"] == "http://cfg-proxy.example:8080"


def test_ignore_cert_errors_reaches_both_the_launch_flags_and_the_context(playwright):
    pw, fake_browser, _c = playwright()

    with browser_mod.browser_session(Config(browser_ignore_cert_errors=True)):
        pass

    assert "--ignore-certificate-errors" in pw.chromium.launch_kwargs["args"]
    assert fake_browser.new_context_kwargs["ignore_https_errors"] is True


class FakeHandle:
    def __init__(self, value) -> None:
        self.value = value
        self.disposed = False

    def json_value(self):
        return self.value

    def dispose(self):
        self.disposed = True


class FakePage:
    def __init__(self, *, rows=None, raises=None, body="") -> None:
        self.handle = FakeHandle(rows if rows is not None else [])
        self._raises = raises
        self._body = body
        self.wait_kwargs = None

    def wait_for_function(self, script, *, arg=None, **kwargs):
        # Positional-arg passing is a TypeError against the real Playwright:
        # `arg` is keyword-only there. Mirror that so this fake cannot pass a
        # call the real API would reject.
        self.script = script
        self.arg = arg
        self.wait_kwargs = kwargs
        if self._raises is not None:
            raise self._raises
        return self.handle

    def inner_text(self, _selector):
        return self._body


def test_extract_runs_the_script_under_a_deadline():
    """The extraction script must be bounded.

    `page.evaluate` obeys no timeout at all, so a wedged main thread hangs the
    process. `wait_for_function` takes the same script and is bounded by the
    driver.
    """
    page = FakePage(rows=[{"title": "T", "url": "https://example.com", "snippet": ""}])

    rows = browser_mod.extract(page, "(sel) => []", ".item", timeout_ms=4000)

    assert rows == [{"title": "T", "url": "https://example.com", "snippet": ""}]
    assert page.wait_kwargs == {"timeout": 4000}
    assert page.arg == ".item"
    assert page.handle.disposed is True


def test_a_wedged_page_becomes_a_timeout_not_a_hang():
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    page = FakePage(raises=PlaywrightTimeout("Timeout 4000ms exceeded"))

    with pytest.raises(EngineFailure) as exc:
        browser_mod.extract(page, "(sel) => []", ".item", timeout_ms=4000)

    assert exc.value.status == "timeout"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ("Please verify you are human before continuing", "rate_limited"),
        ("Access Denied — network security policy", "rate_limited"),
        ("Our systems have detected unusual traffic", "rate_limited"),
        ("Verifying your request...\n\nCalculating...\nDifficulty: 6, Speed: 316.537kH/s", "rate_limited"),
        ("Nothing found for that query.", "empty"),
    ],
)
def test_a_dead_page_is_classified_by_its_body_not_its_status(body, status):
    failure = browser_mod.classify_dead_page(body, empty_detail="nothing rendered")
    assert failure.status == status


def test_a_page_that_cannot_even_be_read_is_an_error_not_empty():
    """Diagnostics must not raise, and a page sought could not read says nothing about the web."""

    class Unreadable(FakePage):
        def inner_text(self, _selector):
            raise RuntimeError("page is gone")

    failure = browser_mod._classify_stuck_page(Unreadable())  # noqa: SLF001
    assert failure.status == "error"
    assert "page not recognised" in failure.detail


def test_hiding_the_automation_flag_is_googles_alone(playwright, clean_proxy_env):
    """The shared browser's launch must not change because the google engine exists."""
    clean_proxy_env.setenv("HTTPS_PROXY", "http://env-proxy.example:3128")
    pw, fake_browser, _c = playwright()

    with browser_mod.browser_session(Config()):
        pass

    assert browser_mod.HIDE_AUTOMATION_FLAG not in pw.chromium.launch_kwargs["args"]
    assert pw.chromium.launch_kwargs["args"] == ["--no-sandbox"]
    assert fake_browser.new_context_kwargs["viewport"] == {"width": 1280, "height": 900}
    assert "no_viewport" not in fake_browser.new_context_kwargs


def test_googles_launch_hides_the_flag_and_still_carries_the_explicit_proxy(playwright, clean_proxy_env):
    """The GNOME proxy fix is not optional for the second browser either."""
    clean_proxy_env.setenv("HTTPS_PROXY", "http://env-proxy.example:3128")
    clean_proxy_env.setenv("NO_PROXY", "localhost")
    pw, fake_browser, _c = playwright()

    with browser_mod.browser_session(Config(), hide_automation=True):
        pass

    assert "--disable-blink-features=AutomationControlled" in pw.chromium.launch_kwargs["args"]
    assert pw.chromium.launch_kwargs["proxy"] == {"server": "http://env-proxy.example:3128", "bypass": "localhost"}
    assert pw.chromium.launch_kwargs["headless"] is True
    # Python's `viewport=None` would be the 1280x720 default; `no_viewport` is the measured `viewport: null`.
    assert fake_browser.new_context_kwargs["no_viewport"] is True
    assert "viewport" not in fake_browser.new_context_kwargs
    # Same user agent policy and certificate handling as the shared browser.
    assert fake_browser.new_context_kwargs["user_agent"] == browser_mod.DEFAULT_UA
    assert fake_browser.new_context_kwargs["ignore_https_errors"] is False


def test_googles_launch_failures_are_engine_failures_too(playwright):
    playwright(launch_raises=RuntimeError("Executable doesn't exist"))

    with pytest.raises(EngineFailure) as exc, browser_mod.browser_session(Config(), hide_automation=True):
        pass  # pragma: no cover

    assert exc.value.status == "error"
    assert "cannot launch chromium" in exc.value.detail

"""The opt-in google engine: off by default, bounded, isolated, disclosed.

Each of the four is a promise the owner's decision to offer google rests on,
so each is pinned here: disabled means an `error` attempt and no Chromium;
the hourly budget is checked before Chromium too; the launch that hides the
automation flag is google's alone; and its `ok` says what was done.

Parsing runs against two results pages captured live (sanitised; see the
comment at the top of each fixture) and against small synthetic pages for
the cases a live capture cannot be ordered to show.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from sought import cli, state
from sought import search as search_mod
from sought.config import Config, GoogleConfig
from sought.engines import google
from sought.engines.base import EngineFailure, Result

T0 = 1_800_000_000.0
ON = Config(google=GoogleConfig(enabled=True))
GOTO = "https://www.google.com/goto?url="


class FakeResponse:
    def __init__(self, location: str) -> None:
        self.headers = {"Location": location} if location else {}

    def close(self) -> None:
        pass


class FakeRedirects:
    """Stands in for `net.session` so resolving `/goto` tokens never touches the network.

    `answers` maps a token URL to a Location (or an exception to raise); any
    token not listed redirects to `https://dest.example/<n>`.
    """

    def __init__(self) -> None:
        self.answers: dict[str, object] = {}
        self.asked: list[str] = []
        self.sessions = 0

    def session(self, _config):
        self.sessions += 1
        return self

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        self.asked.append(url)
        answer = self.answers.get(url, f"https://dest.example/{len(self.asked)}")
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(str(answer))


@pytest.fixture(autouse=True)
def redirects(monkeypatch):
    from sought import net

    fake = FakeRedirects()
    monkeypatch.setattr(net, "session", fake.session)
    return fake


@pytest.fixture
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(state, "_now", lambda: now["t"])

    def advance(seconds):
        now["t"] += seconds

    return advance


@pytest.fixture
def no_chromium(monkeypatch):
    """Fail the test if anything launches a browser, shared or google's own."""

    def refuse(*_a, **_k):
        pytest.fail("no browser may be launched here")

    monkeypatch.setattr(search_mod, "browser_session", refuse)
    monkeypatch.setattr(google, "browser_session", refuse)


# --- parsing ----------------------------------------------------------------


def test_the_captured_results_page_parses_to_its_nine_organic_results(saved):
    results = google.parse(saved("google_results.html"))

    assert len(results) == 9
    assert results[0].title == "Coroutines and tasks"
    assert results[0].snippet.startswith("Task groups combine a task creation API")
    assert all(r.url.startswith(f"{GOTO}FIXTURE") for r in results)
    assert sum(1 for r in results if r.snippet) == 9


def test_the_ai_overview_outside_the_results_is_not_a_result(saved):
    """Its heading ("AI Mode reply for ...") sits above #search; only #search holds results."""
    titles = [r.title for r in google.parse(saved("google_results.html"))]
    assert not any("AI Mode" in t or "AI Overview" in t for t in titles)


def test_a_read_more_link_is_not_part_of_the_snippet(saved):
    assert not any("Read more" in r.snippet for r in google.parse(saved("google_results.html")))


def test_inline_markup_does_not_split_words(saved):
    """`tokio` and `::select!` are separate text nodes; joined with a space they misquote the page."""
    snippets = " ".join(r.snippet for r in google.parse(saved("google_results_2.html")))
    assert "tokio::select!" in snippets
    assert "tokio ::select" not in snippets


def test_the_second_capture_parses_too(saved):
    results = google.parse(saved("google_results_2.html"))
    assert len(results) == 9
    assert results[0].title == "Select | Tokio - An asynchronous Rust runtime"


SYNTHETIC = """
<html><body>
<div id="tads"><div class="MjjYud"><a href="https://ads.example/buy"><h3>Sponsored thing</h3></a></div></div>
<div id="search"><div id="rso">
  <div class="MjjYud"><a href="/url?q=https://example.com/a%3Fb%3D1&amp;sa=U"><h3>Old-style redirect</h3></a>
    <div class="VwiC3b"><span>First <em>snippet</em></span><a href="/x"><span>Read more</span></a></div></div>
  <div class="MjjYud"><a href="https://direct.example/page"><h3>Direct link</h3></a>
    <div data-sncf="1">Second snippet</div></div>
  <div class="MjjYud"><a href="javascript:void(0)"><h3>Not a link</h3></a></div>
  <div class="related-question-pair"><a href="/url?q=https://paa.example/"><h3>People also ask</h3></a></div>
  <div data-initq="x"><a href="/goto?url=PAA"><h3>Another question</h3></a></div>
  <div class="MjjYud"><a href="/search?q=related"><h3>Related search</h3></a></div>
  <div class="MjjYud"><a href="https://direct.example/page"><h3>Direct link, again</h3></a></div>
  <div class="MjjYud"><a href="/goto?url=TOKEN&amp;ved=xyz"><h3>Current-style redirect</h3></a>
    <div><span>no recognised snippet container</span></div></div>
  <div class="MjjYud"><a href="https://no-heading.example/"><span>no heading</span></a></div>
</div></div>
<div id="bottomads"><a href="https://ads.example/more"><h3>More ads</h3></a></div>
</body></html>
"""


def test_ads_questions_navigation_and_non_http_links_are_excluded():
    results = google.parse(SYNTHETIC)

    assert [r.title for r in results] == ["Old-style redirect", "Direct link", "Current-style redirect"]
    assert [r.url for r in results] == [
        "https://example.com/a?b=1",
        "https://direct.example/page",
        f"{GOTO}TOKEN",
    ]


def test_snippets_come_from_either_container_and_fall_back_to_nothing():
    results = google.parse(SYNTHETIC)
    assert [r.snippet for r in results] == ["First snippet", "Second snippet", ""]


def test_a_page_without_a_results_section_parses_to_nothing():
    assert google.parse("<html><body><p>hello</p></body></html>") == []


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/url?q=https://example.com/x%3Fy%3D1&sa=U", "https://example.com/x?y=1"),
        ("https://www.google.com/url?url=https://example.org/&rct=j", "https://example.org/"),
        ("/url?q=javascript:alert(1)", None),
        ("/goto?url=CAESabc_-def&ved=123", f"{GOTO}CAESabc_-def"),
        ("/goto", None),
        ("/search?q=related", None),
        ("#", None),
        ("javascript:void(0)", None),
        ("mailto:someone@example.com", None),
        ("https://example.net/page", "https://example.net/page"),
        ("https://support.google.com/websearch", "https://support.google.com/websearch"),
    ],
)
def test_links_are_unwrapped_or_dropped(href, expected):
    assert google.unwrap(href) == expected


NO_MATCH_PAGE = """<html><body><div id="search"></div><div id="topstuff"><p>Your search -
<b>zqxjkvwmpf blorptangle</b> - did not match any documents.</p><p>Suggestions:</p></div></body></html>"""


def test_googles_own_no_match_page_is_empty():
    failure = google._dead_page(NO_MATCH_PAGE)  # noqa: SLF001
    assert failure.status == "empty"


def test_a_page_that_is_not_recognised_is_never_called_empty():
    """`empty` tells the caller to reword the query. Only Google's own sentence earns it."""
    failure = google._dead_page("<html><body><p>Please enable JavaScript to continue.</p></body></html>")  # noqa: SLF001
    assert failure.status == "error"
    assert "not recognised" in failure.detail


def test_a_wall_in_the_body_is_still_a_wall():
    page = "<html><body>Our systems have detected unusual traffic from your computer network.</body></html>"
    assert google._dead_page(page).status == "rate_limited"  # noqa: SLF001


# --- the engine, against a fake page ------------------------------------------


class FakeHandle:
    def __init__(self, value) -> None:
        self.value = value

    def json_value(self):
        return self.value

    def dispose(self):
        pass


class GooglePage:
    """Lands on `final_url` with `status`, holds `html`, and has results iff `has_results`."""

    def __init__(self, *, html="", final_url=None, status=200, has_results=True, body="") -> None:
        self.html = html
        self.final_url = final_url
        self.status = status
        self.has_results = has_results
        self.body = body
        self.url = "about:blank"
        self.gotos: list[str] = []

    def goto(self, url, *, wait_until):
        self.gotos.append(url)
        self.url = self.final_url or url
        return SimpleNamespace(status=self.status)

    def wait_for_selector(self, _selector, *, timeout):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        if not self.has_results:
            raise PlaywrightTimeout(f"Timeout {timeout}ms exceeded.")

    def inner_text(self, _selector):
        return self.body

    def wait_for_function(self, _script, *, arg=None, timeout=None):
        return FakeHandle(self.html)


@pytest.fixture
def own_browser(monkeypatch):
    """Replace google's own Chromium with a fake page; record how it was asked for."""
    launches: list[dict] = []

    def install(page):
        @contextmanager
        def session(config, **kwargs):
            launches.append(kwargs)

            @contextmanager
            def one_page():
                yield page

            yield SimpleNamespace(page=one_page, timeout_ms=30_000)

        monkeypatch.setattr(google, "browser_session", session)
        # The chain's shared browser is not google's business.
        monkeypatch.setattr(search_mod, "browser_session", lambda *_a, **_k: pytest.fail("shared browser launched"))
        return launches

    return install


def test_disabled_is_an_error_attempt_and_no_browser(no_chromium):
    out = search_mod.run("q", config=Config(), source="google")

    assert out["attempts"] == [{"engine": "google", "status": "error", "detail": google.DISABLED_DETAIL}]
    assert "enabled = true" in google.DISABLED_DETAIL
    assert "[google]" in google.DISABLED_DETAIL


def test_disabled_in_the_chain_is_recorded_and_the_chain_goes_on(no_chromium, monkeypatch):
    from sought.engines.base import Result

    def marginalia(query, *, config, browser=None):
        return [Result("m", "https://m.example")]

    monkeypatch.setitem(search_mod._GENERAL, "marginalia", marginalia)  # noqa: SLF001

    out = search_mod.run("q", config=Config(order=("google", "marginalia")))

    assert [a["engine"] for a in out["attempts"]] == ["google", "marginalia"]
    assert out["attempts"][0]["status"] == "error"
    assert out["engine"] == "marginalia"


def test_the_engine_refuses_on_its_own_when_disabled(no_chromium):
    """No caller may reach Google with it off, even one that skips the chain's gate."""
    with pytest.raises(EngineFailure) as exc:
        google.search("q", config=Config())
    assert exc.value.status == "error"


def test_an_exhausted_budget_skips_without_launching_or_cooling(no_chromium, clock):
    cfg = Config(google=GoogleConfig(enabled=True, max_per_hour=3))
    for _ in range(3):
        state.spend_budget("google")
        clock(60)

    out = search_mod.run("q", config=cfg, source="google")

    [attempt] = out["attempts"]
    assert attempt["status"] == "rate_limited"
    oldest_expires = time.strftime("%H:%M", time.localtime(T0 + 3600))
    assert attempt["detail"] == f"local budget 3/h used, next slot at {oldest_expires} — not asked"
    # Holding back is not a refusal: no cooldown, and nothing spent by the skip.
    assert state.cooling_down(["google"], config=cfg) == {}
    assert state.budget("google", per_hour=3).used == 3


def test_the_budget_window_slides(clock):
    for _ in range(2):
        state.spend_budget("google")
    clock(1800)
    state.spend_budget("google")

    assert state.over_budget("google", per_hour=3) is not None
    clock(1800)  # the first two are now an hour old
    assert state.budget("google", per_hour=3).used == 1
    assert state.over_budget("google", per_hour=3) is None


@pytest.mark.parametrize("body", ["not json", "[1, 2]", '{"google": "lots"}', '{"google": [true, "x", null]}'])
def test_a_corrupt_budget_file_reads_as_unspent(isolated_cache, body):
    isolated_cache.mkdir(parents=True, exist_ok=True)
    (isolated_cache / state.BUDGET_FILE).write_text(body, encoding="utf-8")
    assert state.budget("google", per_hour=20).used == 0
    state.spend_budget("google")
    assert state.budget("google", per_hour=20).used == 1


def test_clearing_state_leaves_the_budget_alone(clock, isolated_cache):
    """A fixed network makes another burst of automated Google queries no safer."""
    state.spend_budget("google")
    state.clear_state()
    assert state.budget("google", per_hour=20).used == 1


def test_a_cooling_google_is_skipped_before_the_budget_is_looked_at(no_chromium, clock):
    from sought.engines.base import Attempt

    state.note_outcome(Attempt("google", "rate_limited", "google /sorry wall"), config=ON)

    out = search_mod.run("q", config=ON, source="google")

    assert "cooling down" in out["attempts"][0]["detail"]
    assert "google /sorry wall" in out["attempts"][0]["detail"]
    assert state.budget("google", per_hour=20).used == 0


def test_results_are_ok_with_the_evasion_disclosed(own_browser, saved, clock):
    launches = own_browser(GooglePage(html=saved("google_results.html")))

    out = search_mod.run("python asyncio taskgroup", config=ON, source="google")

    [attempt] = out["attempts"]
    assert attempt["status"] == "ok"
    assert attempt["count"] == 9
    assert attempt["detail"] == "automation flag hidden (google.enabled)"
    assert all(r["url"].startswith("https://dest.example/") for r in out["results"])
    assert launches == [{"hide_automation": True}]
    assert state.budget("google", per_hour=20).used == 1


def test_direct_links_get_no_redirect_note(own_browser, clock, redirects):
    own_browser(GooglePage(html=SYNTHETIC.replace('href="/goto?url=TOKEN&amp;ved=xyz"', 'href="https://e.example/"')))

    out = search_mod.run("q", config=ON, source="google")

    assert out["attempts"][0]["detail"] == "automation flag hidden (google.enabled)"
    assert redirects.sessions == 0  # nothing to resolve, nothing asked


# --- resolving /goto tokens -------------------------------------------------


def _goto(n: int) -> Result:
    return Result(title=f"t{n}", url=f"{GOTO}T{n}", snippet="")


def test_tokens_are_resolved_to_their_destinations(redirects):
    redirects.answers[f"{GOTO}T1"] = "https://docs.python.org/3/library/asyncio-task.html"

    results, unresolved = google.resolve_redirects([_goto(1), _goto(2)], config=ON)

    assert [r.url for r in results] == ["https://docs.python.org/3/library/asyncio-task.html", "https://dest.example/2"]
    assert unresolved == 0


def test_a_token_that_fails_stays_a_working_redirect_and_is_counted(redirects):
    import requests

    redirects.answers[f"{GOTO}T1"] = requests.ConnectionError("reset")
    redirects.answers[f"{GOTO}T2"] = ""  # a 200, no Location

    results, unresolved = google.resolve_redirects([_goto(1), _goto(2), _goto(3)], config=ON)

    assert [r.url for r in results][:2] == [f"{GOTO}T1", f"{GOTO}T2"]
    assert unresolved == 2


@pytest.mark.parametrize(
    "location",
    ["https://www.google.com/search?q=x", "https://google.com/", "/relative", "javascript:alert(1)"],
)
def test_a_redirect_back_into_google_or_nowhere_is_not_a_destination(redirects, location):
    redirects.answers[f"{GOTO}T1"] = location

    results, unresolved = google.resolve_redirects([_goto(1)], config=ON)

    assert results[0].url == f"{GOTO}T1"
    assert unresolved == 1


def test_a_sorry_redirect_stops_resolving(redirects):
    """Google has started refusing; asking it again per result is the load the budget prevents."""
    redirects.answers[f"{GOTO}T1"] = "https://www.google.com/sorry/index?continue=x"

    results, unresolved = google.resolve_redirects([_goto(1), _goto(2), _goto(3)], config=ON)

    assert redirects.asked == [f"{GOTO}T1"]
    assert unresolved == 3
    assert all(r.url.startswith(GOTO) for r in results)


def test_unresolved_tokens_are_disclosed_in_the_attempt(own_browser, saved, clock, redirects):
    import requests

    own_browser(GooglePage(html=saved("google_results.html")))
    redirects.answers = {}
    real_get = redirects.get

    def flaky(url, **kwargs):
        if len(redirects.asked) == 0:
            redirects.asked.append(url)
            raise requests.Timeout("slow")
        return real_get(url, **kwargs)

    redirects.get = flaky

    out = search_mod.run("python asyncio taskgroup", config=ON, source="google")

    assert out["attempts"][0]["detail"] == (
        "automation flag hidden (google.enabled); 1 of 9 result URLs left as google.com/goto redirects"
    )


def test_the_url_and_hl_and_num_are_asked_for(own_browser, saved, clock):
    page = GooglePage(html=saved("google_results.html"))
    own_browser(page)

    search_mod.run("a b&c", config=Config(google=GoogleConfig(enabled=True), limit=4), source="google")

    assert page.gotos == ["https://www.google.com/search?q=a+b%26c&hl=en&num=4"]


def test_the_sorry_wall_is_rate_limited_and_starts_a_cooldown(own_browser, clock):
    own_browser(GooglePage(final_url="https://www.google.com/sorry/index?continue=x", status=429, has_results=False))

    out = search_mod.run("q", config=ON, source="google")

    assert out["attempts"] == [{"engine": "google", "status": "rate_limited", "detail": "google /sorry wall"}]
    assert "google" in state.cooling_down(["google"], config=ON)
    # It was asked, so it counts against the budget.
    assert state.budget("google", per_hour=20).used == 1


def test_the_consent_interstitial_is_blocked(own_browser, clock):
    own_browser(GooglePage(final_url="https://consent.google.com/ml?continue=x", has_results=False))

    out = search_mod.run("q", config=ON, source="google")

    assert out["attempts"][0]["status"] == "blocked"
    assert "consent" in out["attempts"][0]["detail"]
    assert state.cooling_down(["google"], config=ON) == {}


def test_googles_no_match_page_is_empty(own_browser, clock):
    own_browser(GooglePage(html=NO_MATCH_PAGE, has_results=False, body="did not match any documents"))

    out = search_mod.run("zqxjkvwmpf blorptangle", config=ON, source="google")

    assert out["attempts"][0]["status"] == "empty"


def test_a_200_page_without_results_or_the_no_match_notice_is_not_empty(own_browser, clock):
    own_browser(GooglePage(html="<html><body><p>Something else</p></body></html>", has_results=False))

    out = search_mod.run("q", config=ON, source="google")

    assert out["attempts"][0]["status"] == "error"


def test_results_anchors_that_parse_to_nothing_are_not_empty_either(own_browser, clock):
    """The anchor appeared but nothing survived parsing: a markup change, not an answer."""
    own_browser(GooglePage(html="<html><body><div id='search'></div></body></html>", has_results=True))

    out = search_mod.run("q", config=ON, source="google")

    assert out["attempts"][0]["status"] == "error"


def test_an_http_error_is_classified_like_any_engine(own_browser, clock):
    own_browser(GooglePage(status=403))

    out = search_mod.run("q", config=ON, source="google")

    assert out["attempts"] == [{"engine": "google", "status": "blocked", "detail": "HTTP 403"}]


def test_a_chromium_that_will_not_start_is_an_attempt_and_spends_nothing(monkeypatch, clock):
    @contextmanager
    def broken(config, **kwargs):
        raise EngineFailure("error", "cannot launch chromium: gone (try: sought doctor --fix)")
        yield  # pragma: no cover

    monkeypatch.setattr(google, "browser_session", broken)

    out = search_mod.run("q", config=ON, source="google")

    assert out["attempts"][0]["status"] == "error"
    assert "doctor --fix" in out["attempts"][0]["detail"]
    assert state.budget("google", per_hour=20).used == 0


def test_google_in_the_chain_does_not_launch_the_shared_browser(own_browser, saved, clock, monkeypatch):
    """Only brave and startpage borrow the shared Chromium; google brings its own."""
    own_browser(GooglePage(html=saved("google_results.html")))
    seen = []
    monkeypatch.setattr(
        search_mod,
        "_browser_or_none",
        lambda stack, config, *, needed, attempts: seen.append(needed),  # noqa: ARG005
    )

    out = search_mod.run("q", config=Config(order=("google",), google=GoogleConfig(enabled=True)))

    assert seen == [False]
    assert out["engine"] == "google"


# --- the command line -------------------------------------------------------


def test_source_google_is_accepted_and_disabled_exits_two(monkeypatch, capsys, no_chromium):
    monkeypatch.setattr(cli, "_load_config", lambda **k: Config())  # noqa: ARG005

    assert cli.main(["q", "--source", "google"]) == 2
    captured = capsys.readouterr()
    assert "google=error" in captured.err
    assert "enabled = true" in captured.out


def test_the_help_lists_google_as_opt_in_not_as_part_of_the_chain():
    assert "opt-in (off by default):         google" in cli.EPILOG
    assert "general (fallback chain, tried in order): brave, startpage, marginalia\n" in cli.EPILOG

"""The opt-in brave_api engine: parsing, status classification, and the key.

The fixtures named `brave_api_*.synthetic.json` are NOT live captures. There
was no API key to capture with, so they are written by hand from Brave's
documented response schema (`type`, `query`, `mixed`, `web.results[]` with
`title`, `url`, `description`; errors as `{"type": "ErrorResponse", "error":
{...}}`). Replace them with sanitised live captures once a key is available:
until then, a drift in the real schema would not be caught here.

The one exception is `brave_api_invalid_key.live.json`: a real response to a
deliberately invalid key, which needs no subscription to provoke.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import requests

from sought import search as search_mod
from sought.config import DEFAULT_ORDER, BraveApiConfig, Config
from sought.engines import brave_api
from sought.engines.base import EngineFailure, Result

KEY = "BSA-test-key-0123456789abcdef"
KEYED = Config(brave_api=BraveApiConfig(api_key=KEY))


@pytest.fixture
def http(monkeypatch, http_session):
    """Serve one canned response (or raise one exception) to brave_api.search."""

    def serve(canned):
        sess = http_session(canned)
        monkeypatch.setattr(brave_api, "session", lambda _cfg: sess)
        return sess

    return serve


def _many(n: int) -> str:
    return json.dumps(
        {
            "type": "search",
            "query": {"original": "x"},
            "web": {
                "type": "search",
                "results": [{"title": f"r{i}", "url": f"https://e.example/{i}", "description": "d"} for i in range(n)],
            },
        }
    )


# --- results ------------------------------------------------------------------


def test_a_results_response_is_parsed(http, response, saved):
    sess = http(response(200, saved("brave_api_results.synthetic.json")))

    results = brave_api.search("rust async runtime", config=KEYED)

    assert [r.url for r in results] == [
        "https://tokio.rs/",
        "https://rust-lang.github.io/async-book/",
        "https://async.rs/",
    ]
    assert results[0].title == "Tokio - An asynchronous Rust runtime"
    assert results[1].title == "Asynchronous Programming in Rust & friends"
    assert "executors & runtimes" in results[1].snippet
    assert results[2].snippet == ""
    assert results.detail == ""
    url, kwargs = sess.calls[0]
    assert url == brave_api.ENDPOINT
    assert kwargs["params"] == {"q": "rust async runtime", "count": 10}
    assert kwargs["headers"]["X-Subscription-Token"] == KEY
    assert kwargs["headers"]["Accept"] == "application/json"


def test_the_env_key_is_used_when_the_config_has_none(http, response, saved, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "from-env")
    sess = http(response(200, saved("brave_api_results.synthetic.json")))

    brave_api.search("rust", config=Config())

    assert sess.calls[0][1]["headers"]["X-Subscription-Token"] == "from-env"


def test_the_config_key_wins_over_the_env(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "from-env")
    assert KEYED.brave_api.resolve() == (KEY, "config")
    assert Config().brave_api.resolve() == ("from-env", "env")


def test_a_limit_over_twenty_is_capped_and_says_so(http, response):
    sess = http(response(200, _many(20)))

    results = brave_api.search("x", config=replace(KEYED, limit=25))

    assert sess.calls[0][1]["params"]["count"] == 20
    assert len(results) == 20
    assert results.detail == "capped at 20 results by the API (limit 25)"


def test_the_limit_is_applied_below_twenty(http, response):
    sess = http(response(200, _many(20)))

    results = brave_api.search("x", config=replace(KEYED, limit=3))

    assert sess.calls[0][1]["params"]["count"] == 3
    assert len(results) == 3
    assert results.detail == ""


# --- empty versus not understood ---------------------------------------------


def test_a_well_formed_response_without_web_results_is_empty(http, response, saved):
    http(response(200, saved("brave_api_empty.synthetic.json")))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("zqxjkvwmpf", config=KEYED)

    assert exc.value.status == "empty"


def test_an_empty_results_list_is_empty(http, response):
    http(response(200, json.dumps({"type": "search", "web": {"type": "search", "results": []}})))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert exc.value.status == "empty"


def test_a_200_that_is_not_json_is_an_error_not_empty(http, response):
    """A captive portal or proxy page arrives as HTTP 200 too. Nothing was searched."""
    http(response(200, "<html><body>Please log in</body></html>", headers={"Content-Type": "text/html"}))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert exc.value.status == "error"
    assert "not JSON" in exc.value.detail
    assert "text/html" in exc.value.detail


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"hello": "world"},
        {"type": "search", "web": []},
        {"type": "search", "web": {"results": {"not": "a list"}}},
        {"type": "search", "web": {"results": [{"nothing": "useful"}]}},
    ],
)
def test_a_json_body_of_the_wrong_shape_is_an_error_not_empty(http, response, body):
    http(response(200, json.dumps(body)))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert exc.value.status == "error"
    assert "unexpected response" in exc.value.detail


# --- status classification ---------------------------------------------------


def test_a_rejected_key_is_an_error_never_blocked_or_rate_limited(http, response, saved):
    http(response(401, saved("brave_api_401.synthetic.json")))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert exc.value.status == "error"
    assert exc.value.detail == "API key rejected — HTTP 401 (SUBSCRIPTION_TOKEN_INVALID)"


def test_an_invalid_key_answered_with_422_is_a_rejected_key(http, response, saved):
    """Measured: Brave answers an invalid key with 422, not 401.

    The one live capture here (`brave_api_invalid_key.live.json`), taken with a
    deliberately invalid key. Classified by status alone it read "request
    rejected by the API", which points at sought instead of at the key.
    """
    http(response(422, saved("brave_api_invalid_key.live.json")))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert exc.value.status == "error"
    assert exc.value.detail == "API key rejected — HTTP 422 (SUBSCRIPTION_TOKEN_INVALID)"


def test_a_401_from_a_firewall_is_not_called_a_rejected_key(http, response):
    """An authenticating firewall answers before Brave is reached; the key may be fine."""
    http(response(401, "<html>Authentication required</html>"))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert exc.value.status == "blocked"
    assert "not from the Brave API" in exc.value.detail


def test_429_is_rate_limited_and_carries_the_quota(http, response, saved):
    headers = {
        "X-RateLimit-Limit": "1, 2000",
        "X-RateLimit-Remaining": "0, 0",
        "X-RateLimit-Reset": "1, 1209600",
    }
    http(response(429, saved("brave_api_429.synthetic.json"), headers=headers))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert exc.value.status == "rate_limited"
    # No X-RateLimit-Policy: the windows are numbered, not guessed.
    assert exc.value.detail == (
        "HTTP 429 (RATE_LIMITED) — quota — window 1: limit 1, remaining 0, resets in 1s, exhausted; "
        "window 2: limit 2000, remaining 0, resets in 14d, exhausted"
    )


def test_429_names_windows_from_the_policy_and_does_not_call_a_zero_limit_spent(http, response, saved):
    # The header values a working key returned, measured 2026-11-16: the
    # 30-day window reads limit 0 / remaining 0 while every request succeeded.
    headers = {
        "X-RateLimit-Limit": "50, 0",
        "X-RateLimit-Remaining": "0, 0",
        "X-RateLimit-Reset": "1, 1263310",
        "X-RateLimit-Policy": "50;w=1, 0;w=2592000",
    }
    http(response(429, saved("brave_api_429.synthetic.json"), headers=headers))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert exc.value.status == "rate_limited"
    assert exc.value.detail == (
        "HTTP 429 (RATE_LIMITED) — quota — 1s window: limit 50, remaining 0, resets in 1s, exhausted; "
        "30d window: no cap reported (limit 0), resets in 14d"
    )


def _refusal(http, response, saved, headers):
    http(response(429, saved("brave_api_429.synthetic.json"), headers=headers))
    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)
    assert exc.value.status == "rate_limited"
    return exc.value


MONTH_SPENT = {
    "X-RateLimit-Limit": "1, 2000",
    "X-RateLimit-Remaining": "1, 0",
    "X-RateLimit-Reset": "1, 1209600",
    "X-RateLimit-Policy": "1;w=1, 2000;w=2592000",
}


def test_a_spent_monthly_window_holds_the_engine_until_it_resets(http, response, saved):
    failure = _refusal(http, response, saved, MONTH_SPENT)

    assert failure.hold is not None
    assert failure.hold.seconds == 1209600
    assert failure.hold.reason == "quota spent (30d window), held until it resets per X-RateLimit-Reset"


def test_a_spent_one_second_window_keeps_the_ordinary_cooldown(http, response, saved):
    headers = {**MONTH_SPENT, "X-RateLimit-Remaining": "0, 1500"}
    assert _refusal(http, response, saved, headers).hold is None


def test_a_zero_limit_month_is_never_a_hold(http, response, saved):
    """Measured on a working key: limit 0, remaining 0 on the 30-day window is "no cap", not "spent"."""
    headers = {
        "X-RateLimit-Limit": "50, 0",
        "X-RateLimit-Remaining": "0, 0",
        "X-RateLimit-Reset": "1, 1263310",
        "X-RateLimit-Policy": "50;w=1, 0;w=2592000",
    }
    assert _refusal(http, response, saved, headers).hold is None


@pytest.mark.parametrize(
    "reset",
    [
        "99999999999",  # centuries
        "2600000",  # longer than its own 30-day window
        "soon",  # not a number of seconds
    ],
)
def test_an_implausible_reset_gets_the_ordinary_cooldown(http, response, saved, reset):
    headers = {**MONTH_SPENT, "X-RateLimit-Reset": f"1, {reset}"}
    assert _refusal(http, response, saved, headers).hold is None


def test_without_a_policy_the_reset_still_decides(http, response, saved):
    headers = {k: v for k, v in MONTH_SPENT.items() if k != "X-RateLimit-Policy"}
    failure = _refusal(http, response, saved, headers)
    assert failure.hold is not None
    assert failure.hold.reason.startswith("quota spent (window 2)")


def test_the_hold_reaches_the_attempt_and_the_cooldown_file(http, response, saved, isolated_cache, monkeypatch):
    from sought import state

    monkeypatch.setattr(state, "_now", lambda: 1_800_000_000.0)
    http(response(429, saved("brave_api_429.synthetic.json"), headers=MONTH_SPENT))
    cfg = replace(KEYED, order=("brave_api",))
    search_mod.run("x", config=cfg, source="brave_api")

    (cooldown,) = state.active_cooldowns(config=cfg)
    assert cooldown.until == 1_800_000_000.0 + 1209600
    assert "quota spent (30d window)" in cooldown.describe()
    skip = state.cooling_down(["brave_api"], config=cfg)["brave_api"]
    assert skip.detail.startswith("quota spent (30d window), held until it resets per X-RateLimit-Reset; cooling down")


def test_429_without_headers_is_still_rate_limited(http, response):
    http(response(429, ""))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert (exc.value.status, exc.value.detail) == ("rate_limited", "HTTP 429")


def test_a_request_brave_rejects_is_an_error(http, response):
    body = json.dumps({"type": "ErrorResponse", "error": {"code": "VALIDATION", "status": 422}})
    http(response(422, body))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert exc.value.status == "error"
    assert "HTTP 422 (VALIDATION)" in exc.value.detail


def test_a_server_error_is_blocked_like_the_other_http_engines(http, response):
    http(response(503, "upstream unavailable"))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert (exc.value.status, exc.value.detail) == ("blocked", "HTTP 503")


def test_a_transport_timeout_is_timeout(http):
    http(requests.Timeout("read timed out"))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert exc.value.status == "timeout"


def test_a_connection_failure_is_blocked(http):
    http(requests.ConnectionError("Failed to establish a new connection\nmore"))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=KEYED)

    assert (exc.value.status, exc.value.detail) == ("blocked", "Failed to establish a new connection")


# --- no key -------------------------------------------------------------------


def test_no_key_is_an_error_naming_both_places_and_asks_nothing(monkeypatch):
    monkeypatch.setattr(brave_api, "session", lambda _cfg: pytest.fail("must not make a request"))

    with pytest.raises(EngineFailure) as exc:
        brave_api.search("x", config=Config())

    assert exc.value.status == "error"
    assert "brave_api" in exc.value.detail
    assert "api_key" in exc.value.detail
    assert "BRAVE_API_KEY" in exc.value.detail


def test_asked_for_without_a_key_it_is_recorded_not_skipped():
    out = search_mod.run("x", config=Config(), source="brave_api")

    assert out["results"] == []
    assert out["attempts"] == [{"engine": "brave_api", "status": "error", "detail": brave_api.NO_KEY_DETAIL}]


def test_in_the_chain_without_a_key_it_is_recorded_and_the_chain_moves_on(monkeypatch):

    monkeypatch.setitem(search_mod._GENERAL, "marginalia", lambda *_a, **_k: [Result("m", "https://m.example")])  # noqa: SLF001

    out = search_mod.run("x", config=Config(order=("brave_api", "marginalia")))

    assert [a["status"] for a in out["attempts"]] == ["error", "ok"]
    assert out["engine"] == "marginalia"


# --- no browser ---------------------------------------------------------------


def test_asking_for_it_alone_launches_no_chromium(http, response, saved, monkeypatch):
    def no_browser(*_a, **_k):
        pytest.fail("brave_api must not launch a browser")

    monkeypatch.setattr(search_mod, "browser_session", no_browser)
    http(response(200, saved("brave_api_results.synthetic.json")))

    single = search_mod.run("rust", config=KEYED, source="brave_api", cache="off")
    chain = search_mod.run("rust", config=replace(KEYED, order=("brave_api",)), cache="off")

    assert single["engine"] == chain["engine"] == "brave_api"


def test_answering_ahead_of_the_browser_engines_launches_no_chromium(http, response, saved, monkeypatch):
    """The chain launches Chromium when it reaches brave/startpage, not before it starts.

    Measured before the change: brave_api first in the order answered, and the
    search still paid ~2.4 s for a Chromium nobody used (4.6 s vs 2.2 s).
    """

    def no_browser(*_a, **_k):
        pytest.fail("a browser was launched although brave_api answered first")

    monkeypatch.setattr(search_mod, "browser_session", no_browser)
    http(response(200, saved("brave_api_results.synthetic.json")))

    out = search_mod.run("rust", config=replace(KEYED, order=("brave_api", "brave", "startpage")), cache="off")

    assert out["engine"] == "brave_api"
    assert [a["engine"] for a in out["attempts"]] == ["brave_api"]


def test_failing_ahead_of_the_browser_engines_launches_chromium_once_for_both(http, response, monkeypatch):
    from contextlib import contextmanager

    launches = []

    @contextmanager
    def fake_session(_config):
        launches.append(1)
        yield object()

    monkeypatch.setattr(search_mod, "browser_session", fake_session)
    monkeypatch.setitem(search_mod._GENERAL, "brave", lambda *_a, **_k: [])  # noqa: SLF001
    monkeypatch.setitem(search_mod._GENERAL, "startpage", lambda *_a, **_k: [Result("s", "https://s.example")])  # noqa: SLF001
    http(response(429, ""))

    out = search_mod.run("rust", config=replace(KEYED, order=("brave_api", "brave", "startpage")), cache="off")

    assert launches == [1]
    assert (out["attempts"][0]["engine"], out["attempts"][0]["status"]) == ("brave_api", "rate_limited")
    assert out["engine"] == "startpage"


# --- the key never leaves -----------------------------------------------------


def test_the_key_appears_nowhere_but_the_request_header(http, response, saved, monkeypatch, capsys):
    """Output, attempts, cooldown state, result cache, doctor: none may carry the key."""
    from sought import doctor, output, state

    monkeypatch.setattr(doctor, "_chromium", lambda *, fix: doctor.Check("chromium", ok=True, detail="ok"))  # noqa: ARG005
    monkeypatch.setattr(doctor, "_launch_probe", lambda: (True, "ok"))

    cfg = replace(KEYED, order=("brave_api",))
    seen: list[str] = []

    # A cached success, a rate limit (cooldown file), a key echoed back in an error body.
    http(response(200, saved("brave_api_results.synthetic.json")))
    seen.append(json.dumps(search_mod.run("rust", config=cfg, source="brave_api")))
    echo = json.dumps({"type": "ErrorResponse", "error": {"code": f"bad token {KEY}"}})
    http(response(429, echo, headers={"X-RateLimit-Remaining": "0, 0"}))
    seen.append(json.dumps(search_mod.run("other", config=cfg, source="brave_api", cache="refresh")))
    # A header value requests refuses, whose message quotes the value.
    http(requests.exceptions.InvalidHeader(f"Invalid return character in header value: {KEY!r}"))
    payload = search_mod.run("third", config=replace(cfg, cooldown_seconds=0), source="brave_api", cache="off")
    seen.append(json.dumps(payload))
    output.print_pretty(payload)
    seen.append(repr(cfg))

    # Doctor, offline and online, from config and from the environment.
    doctor.run(cfg, offline=True)
    monkeypatch.setenv("BRAVE_API_KEY", KEY)
    doctor.run(Config(), offline=True)
    http(response(200, saved("brave_api_results.synthetic.json")))
    monkeypatch.setattr(doctor, "_engine_probes", lambda _cfg, *, chromium_ok, cooling: [])  # noqa: ARG005
    doctor.run(replace(KEYED, cooldown_seconds=0), offline=False)
    seen.append(capsys.readouterr().out)

    files = [p.read_text(encoding="utf-8") for p in state.cache_dir().rglob("*") if p.is_file()]
    assert files, "expected the result cache and cooldown file to have been written"
    for text in seen + files:
        assert KEY not in text
    assert "***" in seen[1]
    assert "***" in seen[2]


# --- config and doctor --------------------------------------------------------


def test_the_config_table_is_read_and_validated(tmp_path):
    from sought import config as config_mod
    from sought.errors import ConfigError

    path = tmp_path / "config.toml"
    path.write_text(f'[brave_api]\napi_key = "{KEY}"\n', encoding="utf-8")
    assert config_mod.load(path).brave_api.resolve() == (KEY, "config")

    path.write_text("[brave_api]\napi_key = 12345\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"brave_api\.api_key must be a string"):
        config_mod.load(path)


def test_the_default_template_documents_the_table():
    import tomllib

    from sought import config as config_mod

    assert tomllib.loads(config_mod.DEFAULT_CONFIG_TEMPLATE)["brave_api"] == {"api_key": "", "auto_first": True}
    assert "api-dashboard.search.brave.com" in config_mod.DEFAULT_CONFIG_TEMPLATE
    assert "brave_api" not in config_mod.DEFAULT_ORDER


def test_the_help_lists_brave_api_as_opt_in_not_as_part_of_the_chain():
    from sought import cli

    assert "opt-in (off by default):         google, brave_api" in cli.EPILOG
    assert "[brave_api]" in cli.EPILOG
    assert "tried in order): brave, startpage, marginalia\n" in cli.EPILOG


def test_doctor_without_a_key_says_so_and_is_green():
    from sought import doctor

    check = doctor._brave_api_line(Config(), offline=True, cooling=[])  # noqa: SLF001

    assert check is not None
    assert check.ok is True
    assert check.detail == doctor.BRAVE_API_NO_KEY


def test_doctor_with_no_key_but_in_the_order_warns_without_probing(monkeypatch):
    from sought import doctor

    monkeypatch.setattr(search_mod, "run", lambda *a, **k: pytest.fail("must not probe"))  # noqa: ARG005
    cfg = Config(order=("brave_api",))

    assert doctor._brave_api_line(cfg, offline=True, cooling=[]).ok is False  # noqa: SLF001
    checks = doctor._engine_probes(cfg, chromium_ok=True, cooling=[])  # noqa: SLF001
    assert checks[0].name == "engine brave_api"
    assert checks[0].ok is False
    assert checks[-1].name == "engine chain"


@pytest.mark.parametrize(("env", "cfg", "where"), [(False, KEYED, "config file"), (True, Config(), "BRAVE_API_KEY")])
def test_doctor_offline_names_the_key_source_and_does_not_probe(monkeypatch, env, cfg, where):
    from sought import doctor

    if env:
        monkeypatch.setenv("BRAVE_API_KEY", KEY)
    monkeypatch.setattr(search_mod, "run", lambda *a, **k: pytest.fail("must not probe offline"))  # noqa: ARG005

    check = doctor._brave_api_line(cfg, offline=True, cooling=[])  # noqa: SLF001

    assert check is not None
    assert check.ok is True
    assert check.detail == f"key from {where}, not probed (--offline)"


def test_doctor_online_probes_a_keyed_engine_outside_the_order(http, response, saved):
    from sought import doctor

    http(response(200, saved("brave_api_results.synthetic.json")))

    manual = replace(KEYED, brave_api=replace(KEYED.brave_api, auto_first=False))
    check = doctor._brave_api_line(manual, offline=False, cooling=[])  # noqa: SLF001

    assert check is not None
    assert check.ok is True
    assert check.detail.startswith("3 results [key from config file; auto_first = false and not in engines.order")


def test_doctor_leaves_an_auto_first_engine_to_the_chain_probes():
    from sought import doctor

    assert doctor._brave_api_line(KEYED, offline=False, cooling=[]) is None  # noqa: SLF001


# --- first in the chain when keyed --------------------------------------------


def test_a_key_puts_it_first_in_the_default_order():
    assert KEYED.chain_order() == ("brave_api", *DEFAULT_ORDER)
    assert KEYED.brave_api_auto_first() is True


def test_an_env_key_puts_it_first_too(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", KEY)

    assert Config().chain_order()[0] == "brave_api"


def test_without_a_key_the_order_is_untouched_and_nothing_is_added():
    assert Config().chain_order() == DEFAULT_ORDER
    assert Config().brave_api_auto_first() is False


def test_an_order_that_names_it_keeps_the_users_placement():
    cfg = replace(KEYED, order=("brave", "brave_api", "marginalia"))

    assert cfg.chain_order() == ("brave", "brave_api", "marginalia")
    assert cfg.brave_api_auto_first() is False


def test_auto_first_false_keeps_it_out_of_the_chain():
    cfg = replace(KEYED, brave_api=replace(KEYED.brave_api, auto_first=False))

    assert cfg.chain_order() == DEFAULT_ORDER


def test_the_chain_asks_it_first_with_a_key_and_the_default_order(http, response, saved, monkeypatch):
    def no_browser(*_a, **_k):
        pytest.fail("brave_api answered first; no browser should start")

    monkeypatch.setattr(search_mod, "browser_session", no_browser)
    http(response(200, saved("brave_api_results.synthetic.json")))

    out = search_mod.run("rust", config=KEYED, cache="off")

    assert out["engine"] == "brave_api"
    assert [a["engine"] for a in out["attempts"]] == ["brave_api"]


def test_doctor_shows_the_effective_order_and_why():
    from sought import doctor

    lines = doctor._config_checks(replace(KEYED, source_path=None))  # noqa: SLF001
    order = next(c for c in lines if c.name == "engine order")

    assert order.detail.startswith("brave_api → brave → startpage → marginalia")
    assert "[brave_api first: key set, not named in engines.order]" in order.detail


def test_auto_first_must_be_a_bool(tmp_path):
    from sought import config as config_mod
    from sought.errors import ConfigError

    path = tmp_path / "config.toml"
    path.write_text('[brave_api]\nauto_first = "yes"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match=r"brave_api\.auto_first"):
        config_mod.load(path)

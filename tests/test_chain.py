"""The fallback chain and its audit invariant.

The property under test throughout: sought never reports zero results without
an `attempts` list explaining why.
"""

from __future__ import annotations

import pytest

from sought import search as search_mod
from sought.config import Config
from sought.engines.base import EngineFailure, Result
from sought.errors import UsageError


@pytest.fixture
def engines(monkeypatch):
    """Replace the real engines with programmable stubs."""
    calls: list[str] = []
    behaviour: dict[str, object] = {}

    def make(name):
        def fn(query, *, config, browser=None):
            calls.append(name)
            outcome = behaviour.get(name)
            if isinstance(outcome, EngineFailure):
                raise outcome
            if isinstance(outcome, Exception):
                raise outcome
            return outcome or []

        return fn

    monkeypatch.setattr(search_mod, "_GENERAL", {n: make(n) for n in ("brave", "startpage", "marginalia")})
    # No engine stub touches a browser, so skip launching Chromium.
    monkeypatch.setattr(search_mod, "_browser_or_none", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(search_mod, "BROWSER_ENGINES", frozenset())
    return calls, behaviour


def _cfg(**kw):
    return Config(**kw)


def test_the_first_engine_wins_and_the_rest_are_not_called(engines):
    calls, behaviour = engines
    behaviour["brave"] = [Result("Playwright", "https://playwright.dev", "")]

    out = search_mod.run("playwright", config=_cfg())

    assert calls == ["brave"]
    assert out["engine"] == "brave"
    assert out["attempts"] == [{"engine": "brave", "status": "ok", "count": 1}]


def test_a_rate_limited_engine_falls_through_and_stays_in_the_record(engines):
    calls, behaviour = engines
    behaviour["brave"] = EngineFailure("rate_limited", "HTTP 429")
    behaviour["startpage"] = [Result("Playwright", "https://playwright.dev", "")]

    out = search_mod.run("playwright", config=_cfg())

    assert calls == ["brave", "startpage"]
    assert out["engine"] == "startpage"
    assert out["attempts"][0] == {"engine": "brave", "status": "rate_limited", "detail": "HTTP 429"}
    assert out["results"]


def test_empty_and_rate_limited_are_kept_distinct(engines):
    """They demand opposite responses, so they must never be merged."""
    _calls, behaviour = engines
    behaviour["brave"] = EngineFailure("empty", "matched nothing")
    behaviour["startpage"] = EngineFailure("rate_limited", "HTTP 429")
    behaviour["marginalia"] = EngineFailure("timeout", "no response")

    out = search_mod.run("playwright", config=_cfg())

    assert [a["status"] for a in out["attempts"]] == ["empty", "rate_limited", "timeout"]


def test_total_failure_still_carries_a_full_audit_trail(engines):
    """The invariant: never an empty result set with an empty explanation."""
    _calls, behaviour = engines
    for name in ("brave", "startpage", "marginalia"):
        behaviour[name] = EngineFailure("blocked", f"{name} refused")

    out = search_mod.run("playwright", config=_cfg())

    assert out["results"] == []
    assert out["engine"] is None
    assert len(out["attempts"]) == 3
    assert all(a["detail"] for a in out["attempts"])


def test_an_engine_crashing_does_not_sink_the_chain(engines):
    """A bug in one parser must not cost the results the next engine has."""
    _calls, behaviour = engines
    behaviour["brave"] = RuntimeError("selector blew up")
    behaviour["startpage"] = [Result("ok", "https://example.com", "")]

    out = search_mod.run("playwright", config=_cfg())

    assert out["engine"] == "startpage"
    assert out["attempts"][0]["status"] == "error"
    assert "RuntimeError" in out["attempts"][0]["detail"]


def test_config_order_is_honoured(engines):
    calls, behaviour = engines
    behaviour["marginalia"] = [Result("ok", "https://example.com", "")]

    search_mod.run("q", config=_cfg(order=("marginalia", "brave")))

    assert calls == ["marginalia"]


def test_an_order_of_only_unknown_engines_is_a_usage_error(engines):
    with pytest.raises(UsageError):
        search_mod.run("q", config=_cfg(order=("nonesuch",)))


def test_source_pins_one_engine_and_disables_fallback(engines):
    """An explicit choice must not be silently swapped for something else."""
    calls, behaviour = engines
    behaviour["brave"] = EngineFailure("rate_limited", "HTTP 429")
    behaviour["startpage"] = [Result("ok", "https://example.com", "")]

    out = search_mod.run("q", config=_cfg(), source="brave")

    assert calls == ["brave"]
    assert out["results"] == []
    assert out["attempts"] == [{"engine": "brave", "status": "rate_limited", "detail": "HTTP 429"}]


def test_low_relevance_is_computed_over_the_returned_results(engines):
    _calls, behaviour = engines
    behaviour["brave"] = [Result("Lidl Online Shop", "https://lidl.de", "Angebote")]

    out = search_mod.run("zigbee2mqtt firmware", config=_cfg())

    assert out["low_relevance"] is True


def test_a_flagged_envelope_carries_the_numbers_behind_the_flag(engines):
    _calls, behaviour = engines
    behaviour["brave"] = [Result("Zigbee news", "https://lidl.de", "Angebote")]

    out = search_mod.run("zigbee firmware flashing", config=_cfg())

    assert out["low_relevance"] is True
    assert out["relevance"] == {"best_coverage": 0.27, "matched": ["zigbee"], "missing": ["firmware", "flashing"]}


def test_an_unflagged_envelope_has_null_relevance(engines):
    _calls, behaviour = engines
    behaviour["brave"] = [Result("Zigbee firmware flashing guide", "https://example.com", "")]

    out = search_mod.run("zigbee firmware flashing", config=_cfg())

    assert out["low_relevance"] is False
    assert out["relevance"] is None


def test_an_unknown_source_is_a_usage_error(engines):
    with pytest.raises(UsageError):
        search_mod.run("q", config=_cfg(), source="nonesuch")


def test_a_missing_browser_is_recorded_and_the_http_engine_still_runs(monkeypatch):
    """No Chromium disables two engines; marginalia must still answer."""
    monkeypatch.setattr(
        search_mod,
        "_GENERAL",
        {
            "brave": lambda *a, **k: pytest.fail("brave must not run without a browser"),  # noqa: ARG005
            "startpage": lambda *a, **k: pytest.fail("startpage must not run without a browser"),  # noqa: ARG005
            "marginalia": lambda q, *, config, browser=None: [Result("ok", "https://example.com", "")],  # noqa: ARG005
        },
    )
    monkeypatch.setattr(search_mod, "_browser_or_none", lambda stack, config, *, needed, attempts: None)  # noqa: ARG005

    out = search_mod.run("q", config=_cfg())

    assert out["engine"] == "marginalia"
    assert [a["engine"] for a in out["attempts"]] == ["brave", "startpage", "marginalia"]
    assert out["attempts"][0] == {"engine": "brave", "status": "error", "detail": "skipped: no browser session"}


def test_an_engine_returning_nothing_is_recorded_as_empty_not_ok(engines):
    """Zero results is not a success, and `_finish` credits the first `ok`.

    An engine that returns `[]` without raising used to get `ok` with a count
    of zero, and the envelope then put its name on whichever engine actually
    produced the results — "3 results via brave" over startpage's output.
    """
    _calls, behaviour = engines
    behaviour["brave"] = []
    behaviour["startpage"] = [Result("real", "https://example.com", "")]

    out = search_mod.run("q", config=_cfg())

    assert out["engine"] == "startpage"
    assert out["attempts"][0]["status"] == "empty"
    assert "count" not in out["attempts"][0]


def test_a_silently_empty_engine_never_becomes_the_credited_winner(engines):
    _calls, behaviour = engines
    for name in ("brave", "startpage", "marginalia"):
        behaviour[name] = []

    out = search_mod.run("q", config=_cfg())

    assert out["engine"] is None
    assert [a["status"] for a in out["attempts"]] == ["empty", "empty", "empty"]


def test_a_browser_that_explodes_does_not_abort_the_chain(monkeypatch):
    """The worst of the lot.

    `browser_session` raising anything but EngineFailure escaped the chain
    before a single Attempt was recorded, so the audit trail was empty and
    Python's default exit code of 1 said "searched fine, found nothing".
    Marginalia never got its turn.
    """
    from contextlib import contextmanager

    @contextmanager
    def exploding(_config):
        msg = "greenlet is not the current one"
        raise RuntimeError(msg)
        yield  # pragma: no cover

    monkeypatch.setattr(search_mod, "browser_session", exploding)
    monkeypatch.setattr(
        search_mod,
        "_GENERAL",
        {
            "brave": lambda *a, **k: pytest.fail("brave must not run without a browser"),  # noqa: ARG005
            "startpage": lambda *a, **k: pytest.fail("startpage must not run"),  # noqa: ARG005
            "marginalia": lambda q, *, config, browser=None: [Result("ok", "https://example.com", "")],  # noqa: ARG005
        },
    )

    out = search_mod.run("q", config=_cfg())

    assert out["engine"] == "marginalia"
    assert out["attempts"][0] == {
        "engine": "browser",
        "status": "error",
        "detail": "RuntimeError: greenlet is not the current one",
    }
    assert out["results"]


def test_an_explicitly_named_browser_engine_still_appears_in_the_audit_trail(monkeypatch):
    """`--source brave` with no Chromium recorded nothing at all for brave.

    The chain path recorded the skip; the single-source path did not, so the
    one engine the user actually named was the one missing from the record.
    """
    monkeypatch.setattr(search_mod, "_GENERAL", {"brave": lambda *a, **k: pytest.fail("must not run")})  # noqa: ARG005
    monkeypatch.setattr(search_mod, "_browser_or_none", lambda stack, config, *, needed, attempts: None)  # noqa: ARG005

    out = search_mod.run("q", config=_cfg(), source="brave")

    assert out["results"] == []
    assert out["attempts"] == [{"engine": "brave", "status": "error", "detail": "skipped: no browser session"}]


def test_both_paths_word_a_missing_browser_the_same_way(monkeypatch):
    monkeypatch.setattr(search_mod, "_GENERAL", {n: lambda *a, **k: [] for n in ("brave", "marginalia")})  # noqa: ARG005
    monkeypatch.setattr(search_mod, "_browser_or_none", lambda stack, config, *, needed, attempts: None)  # noqa: ARG005

    chained = search_mod.run("q", config=_cfg(order=("brave", "marginalia")))
    single = search_mod.run("q", config=_cfg(), source="brave")

    assert chained["attempts"][0] == single["attempts"][0]


def test_the_config_reaches_the_engine(engines):
    """Engines apply `limit` themselves, so the chain must pass it through."""
    seen = {}

    def capture(query, *, config, browser=None):
        seen["limit"] = config.limit
        return [Result("ok", "https://example.com", "")]

    search_mod._GENERAL["brave"] = capture  # noqa: SLF001
    search_mod.run("q", config=_cfg(limit=3))

    assert seen["limit"] == 3

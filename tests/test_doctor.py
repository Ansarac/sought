"""Doctor's reporting contract.

This exists because of a tool that printed `chromium missing` and then
`Overall: OK` two lines later. A summary that can be green while the tool is
broken is worse than no summary, so these tests pin the rule down.
"""

from __future__ import annotations

import pytest

from sought import doctor
from sought.config import Config, ProxyConfig

# Captured before `stub_checks` can replace them, for the tests that want the real ones.
_REAL_TLS_NOTE = doctor._tls_note  # noqa: SLF001
_REAL_ENGINE_PROBES = doctor._engine_probes  # noqa: SLF001


@pytest.fixture
def stub_checks(monkeypatch):
    """Replace every probe so only the summary logic is under test."""
    monkeypatch.setattr(doctor, "_python", lambda: doctor.Check("python", ok=True, detail="3.13"))
    monkeypatch.setattr(doctor, "_playwright_package", lambda: doctor.Check("pw", ok=True, detail="ok"))
    monkeypatch.setattr(doctor, "_config_checks", lambda _cfg: [])
    monkeypatch.setattr(doctor, "_tls_note", lambda _cfg, *, probes: None)  # noqa: ARG005
    monkeypatch.setattr(doctor, "_proxy_checks", lambda _cfg: [])
    monkeypatch.setattr(doctor, "_engine_probes", lambda _cfg, *, chromium_ok, cooling: [])  # noqa: ARG005


def test_all_green_reports_ok(stub_checks, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_chromium", lambda *, fix: doctor.Check("chromium", ok=True, detail="/path"))  # noqa: ARG005
    assert doctor.run(Config(), offline=True) == 0
    assert "Overall: OK" in capsys.readouterr().out


def test_a_missing_chromium_fails_the_whole_run(stub_checks, monkeypatch, capsys):
    """The exact bug this design reacts to."""
    monkeypatch.setattr(doctor, "_chromium", lambda *, fix: doctor.Check("chromium", ok=False, detail="not found"))  # noqa: ARG005
    code = doctor.run(Config(), offline=True)
    out = capsys.readouterr().out
    assert code == 2
    assert "Overall: FAILED" in out
    assert "Overall: OK" not in out


def test_an_optional_failure_warns_but_still_succeeds(stub_checks, monkeypatch, capsys):
    """One engine being down is what the chain is for, so it is not fatal."""
    monkeypatch.setattr(doctor, "_chromium", lambda *, fix: doctor.Check("chromium", ok=True, detail="/path"))  # noqa: ARG005
    monkeypatch.setattr(
        doctor,
        "_engine_probes",
        lambda _cfg, *, chromium_ok, cooling: [  # noqa: ARG005
            doctor.Check("engine brave", ok=False, detail="rate_limited", required=False),
            doctor.Check("engine marginalia", ok=True, detail="8 results", required=False),
        ],
    )
    assert doctor.run(Config(), offline=False) == 0
    out = capsys.readouterr().out
    assert "Overall: OK (1 warning)" in out
    assert "! engine brave" in out


def test_every_engine_failing_is_fatal(monkeypatch):
    """Warnings do not add up to OK once nothing is left to route to."""
    payload = {"attempts": [{"engine": "x", "status": "blocked", "detail": "no"}]}
    import sought.search

    monkeypatch.setattr(sought.search, "run", lambda *a, **k: payload)  # noqa: ARG005

    checks = doctor._engine_probes(Config(), chromium_ok=True, cooling=[])  # noqa: SLF001

    assert any(not c.ok and c.required for c in checks)
    assert checks[-1].name == "engine chain"


def test_engines_are_skipped_when_there_is_no_chromium(monkeypatch):
    import sought.search

    monkeypatch.setattr(sought.search, "run", lambda *a, **k: pytest.fail("must not probe"))  # noqa: ARG005
    checks = doctor._engine_probes(Config(order=("brave", "startpage")), chromium_ok=False, cooling=[])  # noqa: SLF001
    assert all("skipped" in c.detail for c in checks)


def test_a_clean_default_install_says_nothing_about_browser_tls():
    """Verification is on by default, so there is nothing to warn about.

    Warning every healthy install about a TLS-inspecting proxy it does not have
    is how a report full of harmless `!` marks teaches people to skip it.
    """
    healthy = [doctor.Check("engine brave", ok=True, detail="8 results", required=False)]
    assert doctor._tls_note(Config(browser_ignore_cert_errors=False), probes=healthy) is None  # noqa: SLF001


def test_a_clean_default_install_exits_zero_with_no_warnings(stub_checks, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_tls_note", _REAL_TLS_NOTE)
    monkeypatch.setattr(doctor, "_chromium", lambda *, fix: doctor.Check("chromium", ok=True, detail="/path"))  # noqa: ARG005
    assert doctor.run(Config(browser_ignore_cert_errors=False), offline=True) == 0
    out = capsys.readouterr().out
    assert "Overall: OK" in out
    assert "warning" not in out
    assert "browser TLS" not in out


def test_a_cert_failure_under_the_default_gets_the_remedy():
    """The one case worth surfacing: verification on, and it is actually biting."""
    probes = [
        doctor.Check("engine brave", ok=False, detail="blocked — net::ERR_CERT_AUTHORITY_INVALID", required=False),
    ]
    check = doctor._tls_note(Config(browser_ignore_cert_errors=False), probes=probes)  # noqa: SLF001
    assert check is not None
    assert check.ok is False
    assert check.required is False
    assert "brave" in check.detail
    assert "tls.browser_ignore_cert_errors" in check.detail


def test_an_unrelated_engine_failure_is_not_blamed_on_tls():
    probes = [doctor.Check("engine brave", ok=False, detail="rate_limited — HTTP 429", required=False)]
    assert doctor._tls_note(Config(browser_ignore_cert_errors=False), probes=probes) is None  # noqa: SLF001


def test_disabled_verification_is_restated_every_run():
    """A real security posture, not a passing note — say so even when everything works."""
    healthy = [doctor.Check("engine brave", ok=True, detail="8 results", required=False)]
    check = doctor._tls_note(Config(browser_ignore_cert_errors=True), probes=healthy)  # noqa: SLF001
    assert check is not None
    assert check.ok is False
    assert check.required is False
    assert "DISABLED" in check.detail


def test_a_cert_warning_never_fails_the_run(stub_checks, monkeypatch, capsys):
    """Non-required means non-required: browser TLS must not turn exit 0 into exit 2."""
    monkeypatch.setattr(doctor, "_tls_note", _REAL_TLS_NOTE)
    monkeypatch.setattr(doctor, "_chromium", lambda *, fix: doctor.Check("chromium", ok=True, detail="/path"))  # noqa: ARG005
    monkeypatch.setattr(
        doctor,
        "_engine_probes",
        lambda _cfg, *, chromium_ok, cooling: [  # noqa: ARG005
            doctor.Check("engine brave", ok=False, detail="blocked — net::ERR_CERT_AUTHORITY_INVALID", required=False),
            doctor.Check("engine marginalia", ok=True, detail="8 results", required=False),
        ],
    )
    assert doctor.run(Config(browser_ignore_cert_errors=False), offline=False) == 0
    assert "browser TLS" in capsys.readouterr().out


SECRET = "s3cr3t"  # noqa: S105 — a fixture value, and the point is that it must not be printed


def test_credentials_in_an_environment_proxy_are_not_printed(monkeypatch):
    """Doctor output is meant to be pasted into bug reports. It must be safe to paste."""
    monkeypatch.setenv("HTTPS_PROXY", f"http://alice:{SECRET}@proxy.internal.example:8080")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)

    detail = doctor._proxy_checks(Config())[0].detail  # noqa: SLF001

    assert SECRET not in detail
    assert "alice" not in detail
    # Still diagnostically useful: host and port are what the reader needs.
    assert "proxy.internal.example:8080" in detail


def test_credentials_in_a_config_proxy_are_not_printed():
    cfg = Config(proxy=ProxyConfig(https=f"http://alice:{SECRET}@proxy.internal.example:8080"))

    detail = doctor._proxy_checks(cfg)[0].detail  # noqa: SLF001

    assert SECRET not in detail
    assert "alice" not in detail
    assert "proxy.internal.example:8080" in detail


def test_a_password_containing_at_and_colon_is_still_fully_removed():
    """Splitting on the first `@` would leak the tail of a password like this one."""
    cfg = Config(proxy=ProxyConfig(https="http://user:p@ss:word@proxy.example:3128"))

    detail = doctor._proxy_checks(cfg)[0].detail  # noqa: SLF001

    assert "p@ss" not in detail
    assert "word" not in detail
    assert "proxy.example:3128" in detail


def test_a_proxy_without_credentials_passes_through_unchanged():
    cfg = Config(proxy=ProxyConfig(https="http://proxy.example:3128"))
    assert "http://proxy.example:3128" in doctor._proxy_checks(cfg)[0].detail  # noqa: SLF001


def test_a_malformed_proxy_value_does_not_crash_the_report():
    for value in ("", "not a url", "@", "http://@", "://x@y"):
        assert isinstance(doctor._redact(value), str)  # noqa: SLF001


def test_the_proxy_line_says_which_transports_it_reaches(monkeypatch):
    """A green proxy line above a red `Connection refused` should say where to look."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    detail = doctor._proxy_checks(Config())[0].detail  # noqa: SLF001
    assert "HTTP client and browser" in detail


def test_no_proxy_anywhere_says_so_in_words(monkeypatch):
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    detail = doctor._proxy_checks(Config())[0].detail  # noqa: SLF001
    assert "none set — all engines connect directly" in detail


def test_the_mark_distinguishes_required_from_optional_failures():
    assert doctor.Check("a", ok=True, detail="").mark == "✓"
    assert doctor.Check("a", ok=False, detail="").mark == "✗"
    assert doctor.Check("a", ok=False, detail="", required=False).mark == "!"


def test_probes_never_read_or_write_the_result_cache(monkeypatch):
    """A replayed probe would report an engine as working without having asked it."""
    import sought.search
    from sought import state
    from sought.engines.base import EngineFailure, Result

    hit = [Result("sought", "https://example.com", "sought self test")]
    monkeypatch.setattr(sought.search, "_GENERAL", {"marginalia": lambda *a, **k: hit})  # noqa: ARG005
    cfg = Config(order=("marginalia",))
    # A user searched for exactly the probe query a moment ago and it worked.
    sought.search.run("sought self test", config=cfg, source="marginalia")

    def refused(*a, **k):
        raise EngineFailure("blocked", "HTTP 403")

    monkeypatch.setattr(sought.search, "_GENERAL", {"marginalia": refused})
    checks = doctor._engine_probes(cfg, chromium_ok=True, cooling=[])  # noqa: SLF001

    assert checks[0].ok is False
    assert "HTTP 403" in checks[0].detail

    # And a successful probe leaves nothing behind for a real search to replay.
    key = state.result_key("sought self test", source="marginalia", order=cfg.order, limit=cfg.limit)
    (state.cache_dir() / state.RESULTS_DIR / f"{key}.json").unlink()
    monkeypatch.setattr(sought.search, "_GENERAL", {"marginalia": lambda *a, **k: hit})  # noqa: ARG005
    assert doctor._engine_probes(cfg, chromium_ok=True, cooling=[])[0].ok is True  # noqa: SLF001
    assert state.load_result(key, ttl=cfg.cache_ttl_seconds) is None


def test_a_cooling_down_engine_is_reported_as_a_warning_and_not_probed(monkeypatch):
    import sought.search
    from sought import state
    from sought.engines.base import Attempt, Result

    hit = [Result("sought", "https://example.com", "sought self test")]
    monkeypatch.setattr(
        sought.search,
        "_GENERAL",
        {
            "brave": lambda *a, **k: pytest.fail("brave is cooling down"),  # noqa: ARG005
            "marginalia": lambda *a, **k: hit,  # noqa: ARG005
        },
    )
    monkeypatch.setattr(sought.search, "browser_session", lambda _cfg: pytest.fail("must not launch chromium"))
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config())

    cfg = Config(order=("brave", "marginalia"))
    checks = doctor._engine_probes(cfg, chromium_ok=True, cooling=state.active_cooldowns(config=cfg))  # noqa: SLF001

    brave = checks[0]
    assert brave.name == "engine brave"
    assert brave.ok is False
    assert brave.required is False
    assert brave.mark == "!"
    assert "not probed: cooling down" in brave.detail
    assert checks[1].ok is True


def test_a_cooldown_that_started_after_doctor_looked_is_still_respected(monkeypatch):
    """`cooling` is a snapshot; the probe goes through `run`, which checks again rather than asking."""
    import sought.search
    from sought import state
    from sought.engines.base import Attempt

    monkeypatch.setattr(sought.search, "_GENERAL", {"marginalia": lambda *a, **k: pytest.fail("cooling down")})  # noqa: ARG005
    state.note_outcome(Attempt("marginalia", "rate_limited", "HTTP 429"), config=Config())

    checks = doctor._engine_probes(Config(order=("marginalia",)), chromium_ok=True, cooling=[])  # noqa: SLF001

    assert "not asked this time" in checks[0].detail


def test_a_rate_limited_probe_starts_a_cooldown(monkeypatch):
    import sought.search
    from sought import state
    from sought.engines.base import EngineFailure

    def refused(*a, **k):
        raise EngineFailure("rate_limited", "HTTP 429")

    monkeypatch.setattr(sought.search, "_GENERAL", {"marginalia": refused})
    doctor._engine_probes(Config(order=("marginalia",)), chromium_ok=True, cooling=[])  # noqa: SLF001

    assert "marginalia" in state.cooling_down(["marginalia"], config=Config())


# --- the cooldowns line ------------------------------------------------------


@pytest.fixture
def green(stub_checks, monkeypatch):
    """Stub a healthy install but keep the real cooldown reporting, so any warning is the cooldowns' doing."""
    monkeypatch.setattr(doctor, "_chromium", lambda *, fix: doctor.Check("chromium", ok=True, detail="/path"))  # noqa: ARG005


def _refuse(status, detail):
    from sought.engines.base import EngineFailure

    def refused(*a, **k):
        raise EngineFailure(status, detail)

    return refused


def test_an_active_cooldown_is_listed_offline_as_a_warning(green, monkeypatch, capsys):
    """No probe is needed to know what searches are skipping: it is in the state file."""
    import time

    from sought import state
    from sought.engines.base import Attempt

    t0 = 1_800_000_000.0
    monkeypatch.setattr(state, "_now", lambda: t0)
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config(cooldown_seconds=600))

    assert doctor.run(Config(), offline=True) == 0
    out = capsys.readouterr().out
    at, until = (time.strftime("%H:%M", time.localtime(t)) for t in (t0, t0 + 600))
    assert (
        f"! cooldowns: brave until {until} (last refusal at {at}: HTTP 429); clear with: sought config --clear-state"
    ) in out
    assert "Overall: OK (1 warning)" in out


def test_several_cooldowns_share_one_line_in_chain_order(green, capsys):
    from sought import state
    from sought.engines.base import Attempt

    state.note_outcome(Attempt("marginalia", "rate_limited", "HTTP 429"), config=Config())
    state.note_outcome(Attempt("brave", "rate_limited", "bot wall"), config=Config())

    doctor.run(Config(), offline=True)

    [line] = [ln for ln in capsys.readouterr().out.splitlines() if "cooldowns:" in ln]
    assert line.index("brave until") < line.index("marginalia until")


def test_no_cooldowns_means_no_line(green, capsys):
    """Nothing to say on a healthy install, as with browser TLS."""
    assert doctor.run(Config(), offline=True) == 0
    out = capsys.readouterr().out
    assert "cooldowns" not in out
    assert "warning" not in out


def test_an_expired_cooldown_is_not_listed(green, monkeypatch, capsys):
    from sought import state
    from sought.engines.base import Attempt

    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config(cooldown_seconds=60))
    later = state._now() + 61  # noqa: SLF001
    monkeypatch.setattr(state, "_now", lambda: later)

    doctor.run(Config(), offline=True)

    assert "cooldowns" not in capsys.readouterr().out


def test_disabled_cooldowns_report_nothing_even_with_a_leftover_file(green, capsys):
    """A leftover entry is not applied when cooldowns are off, so listing it would describe a skip that won't happen."""
    from sought import state
    from sought.engines.base import Attempt

    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config())

    assert doctor.run(Config(cooldown_seconds=0), offline=True) == 0
    assert "cooldowns" not in capsys.readouterr().out


def test_online_the_line_shows_what_doctor_found_and_probes_point_at_it(green, monkeypatch, capsys):
    """Read before probing: a refusal one of doctor's own probes earns shows on the probe, not the line."""
    import sought.search
    from sought import state
    from sought.engines.base import Attempt

    monkeypatch.setattr(doctor, "_engine_probes", _REAL_ENGINE_PROBES)
    monkeypatch.setattr(
        sought.search,
        "_GENERAL",
        {
            "brave": lambda *a, **k: pytest.fail("brave is cooling down"),  # noqa: ARG005
            "marginalia": _refuse("rate_limited", "HTTP 429"),
        },
    )
    monkeypatch.setattr(sought.search, "browser_session", lambda _cfg: pytest.fail("must not launch chromium"))
    state.note_outcome(Attempt("brave", "rate_limited", "bot wall"), config=Config())

    code = doctor.run(Config(order=("brave", "marginalia")), offline=False)

    out = capsys.readouterr().out
    [line] = [ln for ln in out.splitlines() if "cooldowns:" in ln]
    assert "brave until" in line
    assert "marginalia" not in line
    assert "! engine brave: not probed: cooling down (see cooldowns)" in out
    assert "! engine marginalia: rate_limited — HTTP 429" in out
    # brave was not asked and marginalia refused: nothing left, and the detail says why.
    assert code == 2
    assert "every configured engine failed or is cooling down (brave not asked)" in out
    assert "sought config --clear-state" in out.split("engine chain:")[1]


def test_every_engine_cooling_down_fails_the_chain_and_says_it_was_not_asked(monkeypatch):
    """A user who just fixed the network must not read "every engine failed" when nothing was asked."""
    import sought.search
    from sought import state
    from sought.engines.base import Attempt

    monkeypatch.setattr(sought.search, "_GENERAL", {"marginalia": lambda *a, **k: pytest.fail("cooling down")})  # noqa: ARG005
    state.note_outcome(Attempt("marginalia", "rate_limited", "HTTP 429"), config=Config())
    cfg = Config(order=("marginalia",))

    checks = doctor._engine_probes(cfg, chromium_ok=True, cooling=state.active_cooldowns(config=cfg))  # noqa: SLF001

    chain = checks[-1]
    assert chain.name == "engine chain"
    # Still required: a search right now gets nothing, exactly as `sought <query>` exits 2.
    assert chain.ok is False
    assert chain.required is True
    assert "every configured engine is cooling down" in chain.detail
    assert "none was asked" in chain.detail
    assert "sought config --clear-state" in chain.detail
    assert "every configured engine failed" not in chain.detail


def test_a_chain_with_no_cooldowns_keeps_the_plain_detail(monkeypatch):
    import sought.search

    monkeypatch.setattr(sought.search, "_GENERAL", {"marginalia": _refuse("blocked", "HTTP 403")})
    checks = doctor._engine_probes(Config(order=("marginalia",)), chromium_ok=True, cooling=[])  # noqa: SLF001
    assert checks[-1].detail == "every configured engine failed"

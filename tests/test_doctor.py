"""Doctor's reporting contract.

This exists because of a tool that printed `chromium missing` and then
`Overall: OK` two lines later. A summary that can be green while the tool is
broken is worse than no summary, so these tests pin the rule down.
"""

from __future__ import annotations

import pytest

from sought import doctor
from sought.config import Config, ProxyConfig

# Captured before `stub_checks` can replace it, for the tests that want the real one.
_REAL_TLS_NOTE = doctor._tls_note  # noqa: SLF001


@pytest.fixture
def stub_checks(monkeypatch):
    """Replace every probe so only the summary logic is under test."""
    monkeypatch.setattr(doctor, "_python", lambda: doctor.Check("python", ok=True, detail="3.13"))
    monkeypatch.setattr(doctor, "_playwright_package", lambda: doctor.Check("pw", ok=True, detail="ok"))
    monkeypatch.setattr(doctor, "_config_checks", lambda _cfg: [])
    monkeypatch.setattr(doctor, "_tls_note", lambda _cfg, *, probes: None)  # noqa: ARG005
    monkeypatch.setattr(doctor, "_proxy_checks", lambda _cfg: [])
    monkeypatch.setattr(doctor, "_engine_probes", lambda _cfg, *, chromium_ok: [])  # noqa: ARG005


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
        lambda _cfg, *, chromium_ok: [  # noqa: ARG005
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

    checks = doctor._engine_probes(Config(), chromium_ok=True)  # noqa: SLF001

    assert any(not c.ok and c.required for c in checks)
    assert checks[-1].name == "engine chain"


def test_engines_are_skipped_when_there_is_no_chromium(monkeypatch):
    import sought.search

    monkeypatch.setattr(sought.search, "run", lambda *a, **k: pytest.fail("must not probe"))  # noqa: ARG005
    checks = doctor._engine_probes(Config(order=("brave", "startpage")), chromium_ok=False)  # noqa: SLF001
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
        lambda _cfg, *, chromium_ok: [  # noqa: ARG005
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

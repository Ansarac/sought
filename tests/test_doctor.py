"""Doctor's reporting contract.

This exists because of a tool that printed `chromium missing` and then
`Overall: OK` two lines later. A summary that can be green while the tool is
broken is worse than no summary, so these tests pin the rule down.
"""

from __future__ import annotations

import pytest

from scout import doctor
from scout.config import Config


@pytest.fixture
def stub_checks(monkeypatch):
    """Replace every probe so only the summary logic is under test."""
    monkeypatch.setattr(doctor, "_python", lambda: doctor.Check("python", ok=True, detail="3.13"))
    monkeypatch.setattr(doctor, "_playwright_package", lambda: doctor.Check("pw", ok=True, detail="ok"))
    monkeypatch.setattr(doctor, "_config_checks", lambda _cfg: [])
    monkeypatch.setattr(doctor, "_tls_note", lambda _cfg: doctor.Check("tls", ok=True, detail="ok"))
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
    import scout.search

    monkeypatch.setattr(scout.search, "run", lambda *a, **k: payload)  # noqa: ARG005

    checks = doctor._engine_probes(Config(), chromium_ok=True)  # noqa: SLF001

    assert any(not c.ok and c.required for c in checks)
    assert checks[-1].name == "engine chain"


def test_engines_are_skipped_when_there_is_no_chromium(monkeypatch):
    import scout.search

    monkeypatch.setattr(scout.search, "run", lambda *a, **k: pytest.fail("must not probe"))  # noqa: ARG005
    checks = doctor._engine_probes(Config(order=("brave", "startpage")), chromium_ok=False)  # noqa: SLF001
    assert all("skipped" in c.detail for c in checks)


def test_disabling_browser_tls_is_surfaced_as_a_warning():
    """Turning it off is legitimate, but it silently kills two engines."""
    check = doctor._tls_note(Config(browser_ignore_cert_errors=False))  # noqa: SLF001
    assert check.ok is False
    assert check.required is False
    assert "brave" in check.detail


def test_the_mark_distinguishes_required_from_optional_failures():
    assert doctor.Check("a", ok=True, detail="").mark == "✓"
    assert doctor.Check("a", ok=False, detail="").mark == "✗"
    assert doctor.Check("a", ok=False, detail="", required=False).mark == "!"

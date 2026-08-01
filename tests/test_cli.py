"""CLI dispatch, exit codes, and output shape."""

from __future__ import annotations

import json

import pytest

from scout import cli
from scout.config import Config
from scout.errors import ScoutError


@pytest.fixture
def fake_search(monkeypatch):
    """Stub the chain so CLI tests never touch the network."""
    payload = {
        "query": "q",
        "engine": "brave",
        "attempts": [{"engine": "brave", "status": "ok", "count": 1}],
        "low_relevance": False,
        "results": [{"title": "T", "url": "https://example.com", "snippet": "S"}],
    }
    import scout.search

    monkeypatch.setattr(scout.search, "run", lambda *a, **k: payload)  # noqa: ARG005
    monkeypatch.setattr(cli, "_load_config", lambda **k: Config())  # noqa: ARG005
    return payload


def test_results_exit_zero_and_print_json(fake_search, capsys):
    assert cli.main(["hello", "world"]) == 0
    assert json.loads(capsys.readouterr().out)["results"]


def test_pretty_output_is_not_json(fake_search, capsys):
    assert cli.main(["hello", "--pretty"]) == 0
    out = capsys.readouterr().out
    assert "https://example.com" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_no_results_after_clean_searches_exits_one(fake_search, capsys):
    """Exit 1 is an answer: the sources worked and the web had nothing."""
    fake_search["results"] = []
    fake_search["attempts"] = [{"engine": "brave", "status": "empty"}]
    assert cli.main(["hello"]) == 1
    assert "no results" in capsys.readouterr().err


def test_every_source_failing_exits_two(fake_search, capsys):
    """Exit 2 is a malfunction, and must not be confused with exit 1."""
    fake_search["results"] = []
    fake_search["attempts"] = [
        {"engine": "brave", "status": "rate_limited"},
        {"engine": "startpage", "status": "blocked"},
    ]
    assert cli.main(["hello"]) == 2
    err = capsys.readouterr().err
    assert "every source failed" in err
    assert "brave=rate_limited" in err


def test_a_bare_invocation_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_scout_errors_become_exit_codes_not_tracebacks(monkeypatch, capsys):
    class BoomError(ScoutError):
        exit_code = 2

    monkeypatch.setattr(cli, "_dispatch", lambda _argv: (_ for _ in ()).throw(BoomError("kaboom")))
    assert cli.main(["x"]) == 2
    assert "scout: kaboom" in capsys.readouterr().err


def test_interrupt_exits_130(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_dispatch", lambda _argv: (_ for _ in ()).throw(KeyboardInterrupt))
    assert cli.main(["x"]) == 130
    assert "interrupted" in capsys.readouterr().err


def test_unexpected_exceptions_keep_their_traceback(monkeypatch):
    """A real bug must not be flattened into a tidy error message."""
    monkeypatch.setattr(cli, "_dispatch", lambda _argv: (_ for _ in ()).throw(RuntimeError("bug")))
    with pytest.raises(RuntimeError):
        cli.main(["x"])


def test_cli_flags_override_the_config_file(monkeypatch):
    import scout.config

    monkeypatch.setattr(scout.config, "load", lambda *a, **k: Config(limit=10, timeout_seconds=30))  # noqa: ARG005
    cfg = cli._load_config(limit=2, timeout=7)  # noqa: SLF001
    assert (cfg.limit, cfg.timeout_seconds) == (2, 7)


def test_omitted_flags_leave_the_config_alone(monkeypatch):
    import scout.config

    monkeypatch.setattr(scout.config, "load", lambda *a, **k: Config(limit=10, timeout_seconds=30))  # noqa: ARG005
    cfg = cli._load_config()  # noqa: SLF001
    assert (cfg.limit, cfg.timeout_seconds) == (10, 30)


def test_an_unknown_source_is_rejected_before_any_network_call(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["q", "--source", "nonesuch"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_install_skill_print_writes_the_skill_to_stdout(capsys):
    assert cli.main(["install-skill", "--print"]) == 0
    assert "WebFetch" in capsys.readouterr().out


def test_install_skill_writes_into_dest(tmp_path, capsys):
    assert cli.main(["install-skill", "--dest", str(tmp_path)]) == 0
    assert (tmp_path / "scout" / "SKILL.md").is_file()
    assert "installed" in capsys.readouterr().out


def test_config_path_prints_the_path(capsys):
    assert cli.main(["config", "--path"]) == 0
    assert "config.toml" in capsys.readouterr().out

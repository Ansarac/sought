"""CLI dispatch, exit codes, and output shape."""

from __future__ import annotations

import json

import pytest

from sought import cli
from sought.config import Config
from sought.errors import SoughtError


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
    import sought.search

    monkeypatch.setattr(sought.search, "run", lambda *a, **k: payload)  # noqa: ARG005
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


def test_sought_errors_become_exit_codes_not_tracebacks(monkeypatch, capsys):
    class BoomError(SoughtError):
        exit_code = 2

    monkeypatch.setattr(cli, "_dispatch", lambda _argv: (_ for _ in ()).throw(BoomError("kaboom")))
    assert cli.main(["x"]) == 2
    assert "sought: kaboom" in capsys.readouterr().err


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
    import sought.config

    monkeypatch.setattr(sought.config, "load", lambda *a, **k: Config(limit=10, timeout_seconds=30))  # noqa: ARG005
    cfg = cli._load_config(limit=2, timeout=7)  # noqa: SLF001
    assert (cfg.limit, cfg.timeout_seconds) == (2, 7)


def test_omitted_flags_leave_the_config_alone(monkeypatch):
    import sought.config

    monkeypatch.setattr(sought.config, "load", lambda *a, **k: Config(limit=10, timeout_seconds=30))  # noqa: ARG005
    cfg = cli._load_config()  # noqa: SLF001
    assert (cfg.limit, cfg.timeout_seconds) == (10, 30)


@pytest.mark.parametrize(("flag", "value"), [("--limit", 0), ("--limit", -5), ("--timeout", 0), ("--timeout", -3)])
def test_nonpositive_cli_overrides_are_a_usage_error(monkeypatch, capsys, flag, value):
    """These used to be written straight into the frozen Config and fail far away.

    `--limit 0` slices every engine to nothing and is reported as `empty` — a
    false statement about the web. `--timeout 0` means "no timeout" to
    Playwright and hangs. `--timeout -3` leaks a Node.js warning.
    """
    import sought.config
    import sought.search

    monkeypatch.setattr(sought.config, "load", lambda *a, **k: Config())  # noqa: ARG005
    monkeypatch.setattr(sought.search, "run", lambda *a, **k: pytest.fail("must not search"))  # noqa: ARG005

    assert cli.main(["hello", flag, str(value)]) == 2
    err = capsys.readouterr().err
    assert flag in err
    assert "positive integer" in err
    assert str(value) in err


def test_a_positive_override_still_gets_through(monkeypatch):
    import sought.config

    monkeypatch.setattr(sought.config, "load", lambda *a, **k: Config(limit=10, timeout_seconds=30))  # noqa: ARG005
    cfg = cli._load_config(limit=1, timeout=1)  # noqa: SLF001
    assert (cfg.limit, cfg.timeout_seconds) == (1, 1)


def test_an_empty_audit_trail_is_a_bug_and_says_so(fake_search, capsys):
    """A state CLAUDE.md calls a bug rather than an outcome.

    `all()` over an empty list is True, so this used to exit 1 — "the sources
    worked and the web had nothing" — with nothing backing the claim up.
    """
    fake_search["results"] = []
    fake_search["attempts"] = []

    assert cli.main(["hello"]) == 2
    err = capsys.readouterr().err
    assert "bug" in err
    assert "no results" not in err


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
    assert (tmp_path / "sought" / "SKILL.md").is_file()
    assert "installed" in capsys.readouterr().out


def test_config_path_prints_the_path(capsys):
    assert cli.main(["config", "--path"]) == 0
    assert "config.toml" in capsys.readouterr().out


@pytest.mark.parametrize("name", cli.SUBCOMMANDS)
def test_every_subcommand_is_documented_in_the_help(name, capsys):
    # A subcommand absent from --help is a subcommand nobody finds.
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert f"sought {name}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "is_subcommand"),
    [
        (["doctor"], True),
        (["doctor", "--offline"], True),
        (["config", "--init"], True),
        (["install-skill", "--print"], True),
        # A flag that takes a value must not look like search terms.
        (["install-skill", "--dest", "somewhere"], True),
        # Every subcommand name is also an ordinary word; quoting reaches search.
        (["doctor who"], False),
        ([], False),
        (["brave", "--pretty"], False),
    ],
)
def test_subcommand_dispatch_matches_on_the_leading_word(argv, is_subcommand):
    assert cli._names_subcommand(argv) is is_subcommand  # noqa: SLF001


def test_a_quoted_query_starting_with_a_subcommand_word_reaches_search(fake_search, capsys):
    # The search path emits the JSON envelope; `doctor` emits a check report.
    assert cli.main(["doctor who"]) == 0
    assert "attempts" in json.loads(capsys.readouterr().out)


def _capture_run(monkeypatch, payload):
    import sought.search

    seen = {}

    def run(*a, **k):
        seen.update(k)
        return payload

    monkeypatch.setattr(sought.search, "run", run)
    return seen


def test_a_plain_search_uses_the_cache(fake_search, monkeypatch):
    seen = _capture_run(monkeypatch, fake_search)
    assert cli.main(["hello"]) == 0
    assert seen["cache"] == "use"


def test_fresh_skips_the_cache_lookup(fake_search, monkeypatch):
    seen = _capture_run(monkeypatch, fake_search)
    assert cli.main(["hello", "--fresh"]) == 0
    assert seen["cache"] == "refresh"


def test_the_fresh_help_says_cooldowns_still_apply(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--fresh" in out
    assert "cooling down" in out


def test_a_cached_replay_is_labelled_in_pretty_output(fake_search, capsys):
    fake_search["cached"] = {"age_seconds": 200}
    assert cli.main(["hello", "--pretty"]) == 0
    assert "(cached, 3m old — use --fresh to search again)" in capsys.readouterr().out


def test_every_engine_cooling_down_exits_two_like_a_live_refusal(monkeypatch, capsys):
    """A cooldown skip is `rate_limited`: sought did not get an answer, so it is not exit 1."""
    import sought.search
    from sought import state
    from sought.engines.base import Attempt

    monkeypatch.setattr(cli, "_load_config", lambda **k: Config(order=("marginalia",)))  # noqa: ARG005
    monkeypatch.setattr(sought.search, "_GENERAL", {"marginalia": lambda *a, **k: pytest.fail("cooling down")})  # noqa: ARG005
    state.note_outcome(Attempt("marginalia", "rate_limited", "HTTP 429"), config=Config())

    assert cli.main(["hello"]) == 2
    err = capsys.readouterr().err
    assert "marginalia=rate_limited" in err

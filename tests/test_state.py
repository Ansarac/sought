"""Cooldowns and the result cache.

Both exist to lower the load sought itself puts on the engines, and neither is
allowed to cost the audit trail anything: a skipped engine still gets an
attempt, and a replayed result still says which engine produced it and that
it is a replay.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from sought import search as search_mod
from sought import state
from sought.config import Config
from sought.engines.base import Attempt, EngineFailure, Result

T0 = 1_800_000_000.0
HIT = [Result("Playwright", "https://playwright.dev", "playwright docs")]


@pytest.fixture
def clock(monkeypatch):
    """Replace the wall clock with one the test moves by hand."""
    now = {"t": T0}
    monkeypatch.setattr(state, "_now", lambda: now["t"])

    def advance(seconds):
        now["t"] += seconds

    return advance


@pytest.fixture
def engines(monkeypatch, clock):
    """Programmable engine stubs, no browser."""
    calls: list[str] = []
    behaviour: dict[str, object] = {}

    def make(name):
        def fn(query, *, config, browser=None):
            calls.append(name)
            outcome = behaviour.get(name)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome or []

        return fn

    monkeypatch.setattr(search_mod, "_GENERAL", {n: make(n) for n in ("brave", "startpage", "marginalia")})
    monkeypatch.setattr(search_mod, "_browser_or_none", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(search_mod, "BROWSER_ENGINES", frozenset())
    return calls, behaviour


# --- cooldowns --------------------------------------------------------------


def test_a_rate_limited_engine_is_not_asked_again_but_still_appears(engines):
    calls, behaviour = engines
    behaviour["brave"] = EngineFailure("rate_limited", "HTTP 429")
    behaviour["startpage"] = HIT

    search_mod.run("first", config=Config())
    calls.clear()
    out = search_mod.run("second", config=Config())

    assert calls == ["startpage"]
    first = out["attempts"][0]
    assert first["engine"] == "brave"
    assert first["status"] == "rate_limited"
    assert "not asked this time" in first["detail"]
    assert "HTTP 429" in first["detail"]
    assert out["engine"] == "startpage"


@pytest.mark.parametrize("status", ["blocked", "timeout", "empty", "error"])
def test_only_rate_limited_starts_a_cooldown(engines, status):
    """`blocked` is usually a local fault being fixed; the rest are not refusals."""
    calls, behaviour = engines
    behaviour["brave"] = EngineFailure(status, "nope")
    behaviour["startpage"] = HIT

    search_mod.run("first", config=Config())
    calls.clear()
    search_mod.run("second", config=Config())

    assert calls == ["brave", "startpage"]
    assert state.cooling_down(["brave"], config=Config()) == {}


def test_a_crashing_engine_does_not_start_a_cooldown(engines):
    calls, behaviour = engines
    behaviour["brave"] = RuntimeError("selector blew up")
    behaviour["startpage"] = HIT

    search_mod.run("first", config=Config())
    calls.clear()
    search_mod.run("second", config=Config())

    assert calls == ["brave", "startpage"]


def test_the_cooldown_expires(engines, clock):
    calls, behaviour = engines
    behaviour["brave"] = EngineFailure("rate_limited", "HTTP 429")
    cfg = Config(cooldown_seconds=600)
    search_mod.run("first", config=cfg)

    clock(599)
    calls.clear()
    search_mod.run("second", config=cfg)
    assert "brave" not in calls

    clock(2)
    calls.clear()
    behaviour["brave"] = HIT
    out = search_mod.run("third", config=cfg)
    assert calls == ["brave"]
    assert out["attempts"] == [{"engine": "brave", "status": "ok", "count": 1}]


def test_an_ok_attempt_clears_the_cooldown(clock):
    cfg = Config()
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=cfg)
    assert "brave" in state.cooling_down(["brave"], config=cfg)

    state.note_outcome(Attempt("brave", "ok", count=3), config=cfg)

    assert state.cooling_down(["brave"], config=cfg) == {}


def test_other_engines_cooldowns_survive_an_unrelated_ok(clock):
    cfg = Config()
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=cfg)
    state.note_outcome(Attempt("startpage", "ok", count=3), config=cfg)
    assert "brave" in state.cooling_down(["brave"], config=cfg)


def test_cooldown_zero_disables_recording_and_skipping(engines):
    calls, behaviour = engines
    behaviour["brave"] = EngineFailure("rate_limited", "HTTP 429")
    # A cooldown left over from before the user switched it off must not apply either.
    state.note_outcome(Attempt("startpage", "rate_limited", "bot wall"), config=Config())
    off = Config(cooldown_seconds=0)

    search_mod.run("first", config=off)
    calls.clear()
    search_mod.run("second", config=off)

    assert calls == ["brave", "startpage", "marginalia"]


def test_vertical_sources_are_not_cooled(clock):
    """A keyless API saying "slow down" is answered by the next cheap request, not a ten-minute ban."""
    state.note_outcome(Attempt("github", "rate_limited", "HTTP 429"), config=Config())
    assert state.cooling_down(["github"], config=Config()) == {}


def test_the_detail_names_both_times_and_the_original_refusal(clock):
    cfg = Config(cooldown_seconds=600)
    state.note_outcome(Attempt("brave", "rate_limited", 'bot wall: "verifying"'), config=cfg)

    detail = state.cooling_down(["brave"], config=cfg)["brave"].detail

    assert time.strftime("%H:%M", time.localtime(T0 + 600)) in detail
    assert time.strftime("%H:%M", time.localtime(T0)) in detail
    assert 'bot wall: "verifying"' in detail


def test_a_single_source_respects_the_cooldown(engines):
    calls, _behaviour = engines
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config())

    out = search_mod.run("q", config=Config(), source="brave")

    assert calls == []
    assert out["results"] == []
    assert len(out["attempts"]) == 1
    assert out["attempts"][0]["status"] == "rate_limited"
    assert "not asked this time" in out["attempts"][0]["detail"]


def test_a_single_source_records_a_refusal(engines):
    _calls, behaviour = engines
    behaviour["marginalia"] = EngineFailure("rate_limited", "HTTP 429")

    search_mod.run("q", config=Config(), source="marginalia")

    assert "marginalia" in state.cooling_down(["marginalia"], config=Config())


def test_no_browser_is_launched_when_every_browser_engine_is_cooling_down(monkeypatch, clock):
    """Chromium is the expensive part of a search; launching it to skip everything is pure cost."""
    monkeypatch.setattr(search_mod, "browser_session", lambda _cfg: pytest.fail("must not launch chromium"))
    monkeypatch.setattr(
        search_mod,
        "_GENERAL",
        {
            "brave": lambda *a, **k: pytest.fail("brave is cooling down"),  # noqa: ARG005
            "startpage": lambda *a, **k: pytest.fail("startpage is cooling down"),  # noqa: ARG005
            "marginalia": lambda q, *, config, browser=None: HIT,  # noqa: ARG005
        },
    )
    for name in ("brave", "startpage"):
        state.note_outcome(Attempt(name, "rate_limited", "HTTP 429"), config=Config())

    out = search_mod.run("playwright", config=Config())

    assert [a["engine"] for a in out["attempts"]] == ["brave", "startpage", "marginalia"]
    assert [a["status"] for a in out["attempts"]] == ["rate_limited", "rate_limited", "ok"]
    assert out["engine"] == "marginalia"


def test_a_single_cooling_browser_engine_does_not_launch_a_browser(monkeypatch, clock):
    monkeypatch.setattr(search_mod, "browser_session", lambda _cfg: pytest.fail("must not launch chromium"))
    state.note_outcome(Attempt("startpage", "rate_limited", "HTTP 429"), config=Config())

    out = search_mod.run("q", config=Config(), source="startpage")

    assert out["attempts"][0]["status"] == "rate_limited"


def test_the_browser_is_still_launched_while_one_browser_engine_is_available(monkeypatch, clock):
    seen = []

    def fake(stack, config, *, needed, attempts):
        seen.append(needed)

    monkeypatch.setattr(search_mod, "_browser_or_none", fake)
    monkeypatch.setattr(search_mod, "_GENERAL", {n: lambda *a, **k: [] for n in ("brave", "startpage")})  # noqa: ARG005
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config())

    search_mod.run("q", config=Config(order=("brave", "startpage")))

    assert seen == [True]


@pytest.mark.parametrize(
    "body",
    [
        "not json {{{",
        "[1, 2, 3]",
        '{"brave": "soon"}',
        '{"brave": {"until": "later", "at": 0, "detail": "x"}}',
        '{"brave": {"until": true, "at": 0, "detail": "x"}}',
        "\udcff",
    ],
)
def test_a_corrupt_cooldown_file_means_no_cooldowns(engines, isolated_cache, body):
    calls, _behaviour = engines
    isolated_cache.mkdir(parents=True)
    (isolated_cache / state.COOLDOWN_FILE).write_bytes(body.encode("utf-8", "surrogateescape"))

    search_mod.run("q", config=Config())

    assert calls == ["brave", "startpage", "marginalia"]


def test_an_unwritable_state_dir_never_breaks_a_search(engines, monkeypatch, tmp_path):
    """XDG_CACHE_HOME pointing at a regular file: every write fails, the search must not."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    _calls, behaviour = engines
    behaviour["brave"] = EngineFailure("rate_limited", "HTTP 429")
    behaviour["startpage"] = HIT

    out = search_mod.run("q", config=Config())

    assert out["engine"] == "startpage"
    assert search_mod.run("q", config=Config())["engine"] == "startpage"


def test_writes_are_atomic_and_leave_no_temp_files(clock, isolated_cache):
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config())
    assert [p.name for p in isolated_cache.iterdir()] == [state.COOLDOWN_FILE]
    assert json.loads((isolated_cache / state.COOLDOWN_FILE).read_text(encoding="utf-8"))["brave"]["detail"] == (
        "HTTP 429"
    )


def test_the_cache_dir_follows_xdg_at_call_time(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert state.cache_dir() == tmp_path / "sought"


def test_a_relative_xdg_cache_home_is_ignored(monkeypatch):
    """The XDG spec says a relative value is invalid; resolving it against the cwd scatters state."""
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/dir")
    assert state.cache_dir().is_absolute()


# --- result cache -----------------------------------------------------------


def test_a_repeat_search_is_replayed_and_labelled(engines, clock):
    calls, behaviour = engines
    behaviour["brave"] = EngineFailure("rate_limited", "HTTP 429")
    behaviour["startpage"] = HIT

    fresh = search_mod.run("playwright", config=Config())
    clock(180)
    calls.clear()
    replay = search_mod.run("playwright", config=Config())

    assert calls == []
    assert fresh["cached"] is None
    assert replay["cached"] == {"age_seconds": 180}
    # The audit trail of the original search is kept: who answered, who refused.
    assert replay["attempts"] == fresh["attempts"]
    assert replay["engine"] == "startpage"
    assert replay["results"] == fresh["results"]


def test_the_envelope_shape_is_stable(engines):
    """`cached` is always present, so consumers never have to probe for it."""
    _calls, behaviour = engines
    behaviour["brave"] = HIT
    out = search_mod.run("q", config=Config())
    assert list(out) == ["query", "engine", "cached", "attempts", "low_relevance", "results"]


def test_a_search_without_results_is_never_cached(engines, isolated_cache):
    """A failure is exactly what should be asked again."""
    calls, behaviour = engines
    for name in ("brave", "startpage", "marginalia"):
        behaviour[name] = EngineFailure("empty", "matched nothing")

    search_mod.run("q", config=Config())
    calls.clear()
    search_mod.run("q", config=Config())

    assert calls == ["brave", "startpage", "marginalia"]
    assert not (isolated_cache / state.RESULTS_DIR).exists()


def test_low_relevance_results_are_not_cached(engines):
    """A possible decoy page must not keep being served after the engine recovers."""
    calls, behaviour = engines
    behaviour["brave"] = [Result("Lidl Online Shop", "https://lidl.de", "Angebote")]

    assert search_mod.run("zigbee2mqtt firmware", config=Config())["low_relevance"] is True
    calls.clear()
    search_mod.run("zigbee2mqtt firmware", config=Config())

    assert calls == ["brave"]


def test_the_cache_expires(engines, clock):
    calls, behaviour = engines
    behaviour["brave"] = HIT
    cfg = Config(cache_ttl_seconds=900)
    search_mod.run("playwright", config=cfg)

    clock(899)
    calls.clear()
    assert search_mod.run("playwright", config=cfg)["cached"] == {"age_seconds": 899}
    assert calls == []

    clock(1)
    assert search_mod.run("playwright", config=cfg)["cached"] is None
    assert calls == ["brave"]


def test_a_clock_that_went_backwards_is_a_miss(engines, clock):
    calls, behaviour = engines
    behaviour["brave"] = HIT
    search_mod.run("playwright", config=Config())
    clock(-60)
    calls.clear()
    search_mod.run("playwright", config=Config())
    assert calls == ["brave"]


def test_ttl_zero_disables_the_cache(engines, isolated_cache):
    calls, behaviour = engines
    behaviour["brave"] = HIT
    off = Config(cache_ttl_seconds=0)

    search_mod.run("playwright", config=off)
    calls.clear()
    search_mod.run("playwright", config=off)

    assert calls == ["brave"]
    assert not (isolated_cache / state.RESULTS_DIR).exists()


def test_refresh_skips_the_lookup_but_still_stores(engines, clock):
    calls, behaviour = engines
    behaviour["brave"] = HIT
    search_mod.run("playwright", config=Config())
    clock(300)

    calls.clear()
    out = search_mod.run("playwright", config=Config(), cache="refresh")
    assert calls == ["brave"]
    assert out["cached"] is None

    clock(10)
    assert search_mod.run("playwright", config=Config())["cached"] == {"age_seconds": 10}


def test_refresh_does_not_bypass_a_cooldown(engines):
    """Bypassing a cooldown is precisely the hammering it exists to stop."""
    calls, _behaviour = engines
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config())

    search_mod.run("q", config=Config(), cache="refresh")

    assert "brave" not in calls


def test_off_neither_reads_nor_writes(engines, isolated_cache):
    calls, behaviour = engines
    behaviour["brave"] = HIT
    search_mod.run("playwright", config=Config())

    calls.clear()
    search_mod.run("playwright", config=Config(), cache="off")
    assert calls == ["brave"]

    search_mod.run("playwright other", config=Config(), cache="off")
    assert len(list((isolated_cache / state.RESULTS_DIR).iterdir())) == 1


def test_whitespace_and_case_do_not_split_the_cache():
    key = state.result_key("Rust  async\truntime", source=None, order=("brave",), limit=10)
    assert key == state.result_key("rust async runtime", source=None, order=("brave",), limit=10)


@pytest.mark.parametrize(
    "other",
    [
        {"query": "rust sync runtime"},
        {"source": "hn"},
        {"order": ("startpage", "brave")},
        {"limit": 5},
    ],
)
def test_anything_that_changes_the_answer_changes_the_key(other):
    base = {"query": "rust async runtime", "source": None, "order": ("brave", "startpage"), "limit": 10}
    changed = {**base, **other}
    assert state.result_key(base.pop("query"), **base) != state.result_key(changed.pop("query"), **changed)


def test_order_does_not_matter_to_a_single_source():
    """`--source` ignores the chain order, so the cache should too."""
    one = state.result_key("q", source="hn", order=("brave",), limit=10)
    assert one == state.result_key("q", source="hn", order=("marginalia",), limit=10)


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        '{"stored_at": "yesterday", "envelope": {}}',
        '{"stored_at": 1800000000, "envelope": {"results": [], "attempts": []}}',
        '{"stored_at": 1800000000, "envelope": {"results": [{"title": "x"}], "attempts": []}}',
        '{"stored_at": 1800000000}',
    ],
)
def test_a_corrupt_cache_entry_is_a_miss(engines, isolated_cache, body):
    calls, behaviour = engines
    behaviour["brave"] = HIT
    key = state.result_key("playwright", source=None, order=Config().order, limit=Config().limit)
    (isolated_cache / state.RESULTS_DIR).mkdir(parents=True)
    (isolated_cache / state.RESULTS_DIR / f"{key}.json").write_text(body, encoding="utf-8")

    out = search_mod.run("playwright", config=Config())

    assert calls == ["brave"]
    assert out["cached"] is None


def test_expired_entries_are_pruned_on_write(engines, clock, isolated_cache):
    _calls, behaviour = engines
    behaviour["brave"] = HIT
    search_mod.run("playwright old", config=Config())
    stale = next((isolated_cache / state.RESULTS_DIR).iterdir())
    long_ago = time.time() - 10_000
    os.utime(stale, (long_ago, long_ago))

    search_mod.run("playwright new", config=Config())

    remaining = list((isolated_cache / state.RESULTS_DIR).iterdir())
    assert len(remaining) == 1
    assert remaining[0] != stale


# --- reporting cooldowns ----------------------------------------------------


def test_active_cooldowns_lists_live_entries_in_chain_order(clock):
    cfg = Config(cooldown_seconds=600)
    state.note_outcome(Attempt("marginalia", "rate_limited", "HTTP 429"), config=cfg)
    state.note_outcome(Attempt("brave", "rate_limited", "bot wall"), config=cfg)

    active = state.active_cooldowns(config=cfg)

    assert [c.engine for c in active] == ["brave", "marginalia"]
    assert active[0] == state.Cooldown("brave", T0 + 600, T0, "bot wall")
    assert active[0].describe() == (
        f"brave until {time.strftime('%H:%M', time.localtime(T0 + 600))} "
        f"(last refusal at {time.strftime('%H:%M', time.localtime(T0))}: bot wall)"
    )


def test_active_cooldowns_drops_expired_entries(clock):
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config(cooldown_seconds=60))
    clock(60)
    assert state.active_cooldowns(config=Config()) == []


def test_active_cooldowns_is_empty_when_cooldowns_are_off(clock):
    """Matches what a search does: a leftover entry is not applied, so it is not reported either."""
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config())
    assert state.active_cooldowns(config=Config(cooldown_seconds=0)) == []


# --- clearing ---------------------------------------------------------------


def _cache_one(query="playwright"):
    key = state.result_key(query, source=None, order=Config().order, limit=Config().limit)
    state.store_result(key, {"results": [{"title": "x"}], "attempts": [{"engine": "brave"}]}, ttl=900)
    return key


def test_clear_state_removes_cooldowns_cached_results_and_temp_files(clock, isolated_cache):
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config())
    state.note_outcome(Attempt("marginalia", "rate_limited", "HTTP 429"), config=Config())
    key = _cache_one("one")
    _cache_one("two")
    # What a crash between mkstemp and replace leaves behind.
    (isolated_cache / ".cooldown.json.abc123.tmp").write_text("{", encoding="utf-8")
    (isolated_cache / state.RESULTS_DIR / f".{key}.json.xyz.tmp").write_text("{", encoding="utf-8")

    cleared = state.clear_state()

    assert cleared.directory == isolated_cache
    assert cleared.cooldowns == ["brave", "marginalia"]
    assert cleared.results == 2
    assert cleared.stale == 2
    assert cleared.failed == []
    assert list(isolated_cache.rglob("*")) == [isolated_cache / state.RESULTS_DIR]
    assert state.cooling_down(["brave", "marginalia"], config=Config()) == {}


def test_an_expired_cooldown_file_is_removed_but_not_called_a_cooldown(clock, isolated_cache):
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config(cooldown_seconds=60))
    clock(120)

    cleared = state.clear_state()

    assert cleared.cooldowns == []
    assert cleared.stale == 1
    assert cleared.removed_anything
    assert not (isolated_cache / state.COOLDOWN_FILE).exists()


def test_clearing_a_missing_dir_is_not_an_error(isolated_cache):
    assert not isolated_cache.exists()
    cleared = state.clear_state()
    assert not cleared.removed_anything
    assert cleared.failed == []
    assert not isolated_cache.exists()


def test_clearing_an_empty_dir_removes_nothing(isolated_cache):
    (isolated_cache / state.RESULTS_DIR).mkdir(parents=True)
    cleared = state.clear_state()
    assert not cleared.removed_anything
    assert cleared.failed == []


def test_an_unremovable_file_is_reported_and_the_rest_still_goes(clock, isolated_cache, monkeypatch):
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config())
    _cache_one()
    stuck = isolated_cache / state.COOLDOWN_FILE
    real_unlink = type(stuck).unlink

    def unlink(self, *a, **k):
        if self == stuck:
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(type(stuck), "unlink", unlink)

    cleared = state.clear_state()

    assert cleared.failed == [(stuck, "Permission denied")]
    # Not removed, so not reported as cleared.
    assert cleared.cooldowns == []
    assert cleared.results == 1
    assert stuck.exists()


def test_an_unlistable_dir_is_reported_not_raised(isolated_cache, monkeypatch):
    """XDG_CACHE_HOME/sought being a regular file, say."""
    isolated_cache.parent.mkdir(parents=True, exist_ok=True)
    isolated_cache.write_text("not a directory", encoding="utf-8")

    cleared = state.clear_state()

    assert [path for path, _reason in cleared.failed] == [isolated_cache]
    assert isolated_cache.read_text(encoding="utf-8") == "not a directory"


def test_clearing_touches_nothing_sought_did_not_write(clock, isolated_cache, tmp_path):
    state.note_outcome(Attempt("brave", "rate_limited", "HTTP 429"), config=Config())
    _cache_one()
    results = isolated_cache / state.RESULTS_DIR
    # Neighbours in sought's own dirs whose names sought never writes.
    foreign = [
        isolated_cache / "notes.txt",
        isolated_cache / "cooldown.json.bak",
        results / "mine.json",
        results / ("0" * 64 + ".json.bak"),
    ]
    for path in foreign:
        path.write_text("keep", encoding="utf-8")
    # A directory with a sought-shaped name is not a file sought wrote.
    (results / ("a" * 64 + ".json")).mkdir()
    # And a file entirely outside the cache dir.
    outside = tmp_path / "elsewhere.json"
    outside.write_text("keep", encoding="utf-8")

    cleared = state.clear_state()

    assert cleared.cooldowns == ["brave"]
    assert cleared.results == 1
    assert all(path.read_text(encoding="utf-8") == "keep" for path in [*foreign, outside])
    assert (results / ("a" * 64 + ".json")).is_dir()


def test_a_symlinked_results_dir_is_not_followed(isolated_cache, tmp_path):
    """Globbing through a symlink would delete files outside the cache dir."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / ("f" * 64 + ".json")
    victim.write_text("keep", encoding="utf-8")
    isolated_cache.mkdir(parents=True)
    (isolated_cache / state.RESULTS_DIR).symlink_to(elsewhere, target_is_directory=True)

    cleared = state.clear_state()

    assert victim.read_text(encoding="utf-8") == "keep"
    assert cleared.failed == [(isolated_cache / state.RESULTS_DIR, "is a symlink; not followed")]
    assert (isolated_cache / state.RESULTS_DIR).is_symlink()


def test_a_symlinked_state_file_is_unlinked_as_a_link_and_its_target_kept(isolated_cache, tmp_path):
    target = tmp_path / "precious.json"
    target.write_text("{}", encoding="utf-8")
    isolated_cache.mkdir(parents=True)
    (isolated_cache / state.COOLDOWN_FILE).symlink_to(target)

    cleared = state.clear_state()

    assert cleared.stale == 1
    assert not (isolated_cache / state.COOLDOWN_FILE).is_symlink()
    assert target.read_text(encoding="utf-8") == "{}"

"""Local state that lowers the load sought itself puts on the engines.

Two mechanisms, both kept under `${XDG_CACHE_HOME:-~/.cache}/sought/`:

* **Cooldowns.** An engine that answered `rate_limited` is not asked again
  until `engines.cooldown_seconds` have passed. Asking a refusing engine again
  seconds later does not get an answer — on a shared egress IP it prolongs the
  ban for everyone behind it, and a browser engine costs a selector timeout
  besides. The skip is still recorded as a `rate_limited` attempt whose detail
  says sought did not ask: respecting a refusal must never cost the audit
  trail an entry.
* **A short-lived result cache.** Agents repeat the exact same query within
  minutes. Replaying an envelope that had results is cheaper for everyone, and
  the replay is labelled (`cached.age_seconds`) so it is never mistaken for a
  fresh search.
* **A request budget** for the opt-in `google` engine: a sliding hour of
  request timestamps, checked before Chromium is launched. It bounds what one
  shared egress IP shows Google, which is the user's call to make, not a
  refusal by Google — so it is a separate mechanism from the cooldowns.

Cooldowns and the cache are disposable. `sought config --clear-state`
(`clear_state`) deletes them early, for the user who has fixed whatever caused
a refusal. It does not touch the budget: nothing about the network being
fixed makes another burst of automated Google queries any safer.

Everything here is an optimisation. A missing, corrupt or unwritable state
file must degrade to "no cooldowns, cache miss" and never break a search, so
every I/O path below swallows its own failures. Concurrent sought processes
can race on the cooldown file; a lost update costs one extra request at worst,
which is not worth a lock.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from sought.engines.base import GENERAL_ENGINES, Attempt

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sought.config import Config

COOLDOWN_FILE = "cooldown.json"
RESULTS_DIR = "results"

# Part of every result-cache key, so a change to the stored envelope's shape
# turns old entries into misses instead of replaying something stale.
# 2: the envelope gained `relevance`.
_CACHE_FORMAT = 2

# Only the general engines are scraped, so only they can be hammered. A
# vertical source that rate limits is a documented API saying "slow down" for
# this one request, and the next request is cheap to make. `google` is a
# general engine and is cooled like the rest.
_COOLED = frozenset(GENERAL_ENGINES)


def _now() -> float:
    """Wall-clock time, behind a seam so tests can move it."""
    return time.time()


def cache_dir() -> Path:
    """Where sought keeps its state, resolved at call time.

    Read on every call rather than at import, so that tests (and anything else
    that changes XDG_CACHE_HOME) cannot leak into the real ~/.cache. The XDG
    spec says a relative value is invalid and must be ignored.
    """
    raw = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(raw) if raw and Path(raw).is_absolute() else Path.home() / ".cache"
    return base / "sought"


# --- cooldowns --------------------------------------------------------------


@dataclass(frozen=True)
class Cooldown:
    """One engine sought is leaving alone, and the refusal that started it."""

    engine: str
    until: float
    at: float
    detail: str

    def describe(self) -> str:
        """`brave until 23:14 (last refusal at 23:04: HTTP 429)` — for a report, not an attempt."""
        return f"{self.engine} until {_clock(self.until)} (last refusal at {_clock(self.at)}: {self.detail})"


def active_cooldowns(*, config: Config) -> list[Cooldown]:
    """Every engine a search would skip right now, in chain order.

    Empty when cooldowns are disabled: a leftover file is not applied then, so
    reporting it would describe a skip that will not happen.
    """
    if config.cooldown_seconds <= 0:
        return []
    return _live_cooldowns()


def cooling_down(names: Iterable[str], *, config: Config) -> dict[str, Attempt]:
    """Return a ready-made `rate_limited` attempt for each engine still cooling down.

    The state file is read once per search, not once per engine.
    """
    active = {c.engine: c for c in active_cooldowns(config=config)}
    return {name: Attempt(name, "rate_limited", _cooldown_detail(active[name])) for name in names if name in active}


def note_outcome(attempt: Attempt, *, config: Config) -> None:
    """Update cooldown state from one live attempt.

    Only `rate_limited` starts a cooldown. `blocked` is usually a local fault
    (a proxy, a certificate) the user is busy fixing, and holding the engine
    back would hide whether the fix worked; `timeout`, `empty` and `error` are
    not refusals at all. An `ok` ends any cooldown early.
    """
    if config.cooldown_seconds <= 0 or attempt.engine not in _COOLED:
        return
    if attempt.status not in {"rate_limited", "ok"}:
        return

    entries = _read_cooldowns()
    now = _now()
    live = {name: entry for name, entry in entries.items() if entry["until"] > now}
    if attempt.status == "rate_limited":
        live[attempt.engine] = {
            "until": now + config.cooldown_seconds,
            "detail": attempt.detail or "rate limited",
            "at": now,
        }
    else:
        live.pop(attempt.engine, None)

    if live != entries:
        _write_json(cache_dir() / COOLDOWN_FILE, live)


def _live_cooldowns() -> list[Cooldown]:
    """Return the unexpired entries in the cooldown file, whatever the config says."""
    entries = _read_cooldowns()
    now = _now()
    return [
        Cooldown(name, entry["until"], entry["at"], entry["detail"])
        for name in GENERAL_ENGINES
        if name in _COOLED and (entry := entries.get(name)) is not None and entry["until"] > now
    ]


def _cooldown_detail(cooldown: Cooldown) -> str:
    return (
        f"cooling down until {_clock(cooldown.until)}, not asked this time "
        f"(last refusal at {_clock(cooldown.at)}: {cooldown.detail})"
    )


def _clock(epoch: float) -> str:
    """Local HH:MM, with the date only when it is not today."""
    when = time.localtime(epoch)
    if time.strftime("%Y-%m-%d", when) == time.strftime("%Y-%m-%d", time.localtime(_now())):
        return time.strftime("%H:%M", when)
    return time.strftime("%Y-%m-%d %H:%M", when)


def _read_cooldowns() -> dict[str, dict[str, Any]]:
    """Read the cooldown file, keeping only well-formed entries."""
    raw = _read_json(cache_dir() / COOLDOWN_FILE)
    if not isinstance(raw, dict):
        return {}
    return {
        name: entry
        for name, entry in raw.items()
        if isinstance(entry, dict)
        and _is_number(entry.get("until"))
        and _is_number(entry.get("at"))
        and isinstance(entry.get("detail"), str)
    }


# --- request budget ---------------------------------------------------------

BUDGET_FILE = "budget.json"
_HOUR = 3600.0


@dataclass(frozen=True)
class Budget:
    """An engine's local hourly allowance, as of now."""

    engine: str
    limit: int
    used: int
    next_slot: float | None  # when a request may be made again; None while one may be made now

    @property
    def remaining(self) -> int:
        """Requests still allowed in the current hour-long window."""
        return max(self.limit - self.used, 0)

    def describe(self) -> str:
        """`98/100 left this hour` — for doctor."""
        return f"{self.remaining}/{self.limit} left this hour"


def budget(engine: str, *, per_hour: int) -> Budget:
    """How much of an engine's hourly budget the last hour has used.

    A sliding window, not a clock hour: a burst of twenty at 13:59 does not buy
    another twenty at 14:00. The next slot opens when enough of the window's
    requests have aged out to bring it under the limit.
    """
    stamps = sorted(_budget_window(_read_budget().get(engine, []), _now()))
    used = len(stamps)
    next_slot = stamps[used - per_hour] + _HOUR if used >= per_hour else None
    return Budget(engine, per_hour, used, next_slot)


def over_budget(engine: str, *, per_hour: int) -> Attempt | None:
    """Return a ready-made `rate_limited` attempt if the budget is spent, else None.

    Shaped like `cooling_down`: a skip that still appears in the audit trail,
    worded so it cannot be read as the engine having refused. Never fed to
    `note_outcome` — sought holding itself back is not a refusal, and a
    cooldown started by it would later be reported as one.
    """
    state = budget(engine, per_hour=per_hour)
    if state.next_slot is None:
        return None
    detail = f"local budget {per_hour}/h used, next slot at {_clock(state.next_slot)} — not asked"
    return Attempt(engine, "rate_limited", detail)


def spend_budget(engine: str) -> None:
    """Record one real request against an engine's budget.

    Called just before the navigation, so a request that then fails still
    counts: the budget bounds what the engine *sees*. Concurrent sought
    processes can lose an update here as with cooldowns; at worst one request
    goes uncounted. A corrupt file is read as an empty one — the same "degrade,
    never break a search" rule as everything else in this module.
    """
    now = _now()
    entries = {name: _budget_window(stamps, now) for name, stamps in _read_budget().items()}
    entries[engine] = [*entries.get(engine, []), now]
    _write_json(cache_dir() / BUDGET_FILE, {name: stamps for name, stamps in entries.items() if stamps})


def _budget_window(stamps: list[float], now: float) -> list[float]:
    """Keep the stamps inside the last hour. Future ones mean the clock moved; they are dropped."""
    return [t for t in stamps if now - _HOUR < t <= now]


def _read_budget() -> dict[str, list[float]]:
    raw = _read_json(cache_dir() / BUDGET_FILE)
    if not isinstance(raw, dict):
        return {}
    return {
        name: [t for t in stamps if _is_number(t)]
        for name, stamps in raw.items()
        if isinstance(name, str) and isinstance(stamps, list)
    }


# --- result cache -----------------------------------------------------------


def result_key(query: str, *, source: str | None, order: Iterable[str], limit: int) -> str:
    """Identify a search by everything that changes its answer.

    The query is compared after collapsing whitespace and case, which no engine
    distinguishes. The engine order only matters for the chain: `--source`
    asks one source whatever the order says.
    """
    material = {
        "format": _CACHE_FORMAT,
        "query": " ".join(query.split()).casefold(),
        "source": source or "chain",
        "order": list(order) if source is None else [],
        "limit": limit,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def load_result(key: str, *, ttl: int) -> dict[str, Any] | None:
    """Return the cached envelope for `key`, labelled with its age, or None on any kind of miss."""
    if ttl <= 0:
        return None
    raw = _read_json(cache_dir() / RESULTS_DIR / f"{key}.json")
    if not isinstance(raw, dict) or not _is_number(raw.get("stored_at")):
        return None
    payload = raw.get("envelope")
    if not _is_replayable(payload):
        return None

    age = _now() - raw["stored_at"]
    # A negative age means the clock moved backwards; trust nothing about it.
    if age < 0 or age >= ttl:
        return None
    return {**payload, "cached": {"age_seconds": int(age)}}


def store_result(key: str, payload: dict[str, Any], *, ttl: int) -> None:
    """Cache an envelope worth replaying, and prune entries that have expired.

    Never cached: an envelope without results (a failure is exactly what
    should be retried, once the cooldowns allow), and one flagged
    `low_relevance` (possibly a decoy page; replaying it would keep serving
    the decoy after the engine has recovered).
    """
    if ttl <= 0 or not payload.get("results") or payload.get("low_relevance"):
        return
    directory = cache_dir() / RESULTS_DIR
    stored = {**payload, "cached": None}
    _write_json(directory / f"{key}.json", {"stored_at": _now(), "envelope": stored})
    _prune(directory, ttl=ttl)


def _is_replayable(payload: object) -> TypeGuard[dict[str, Any]]:
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("results"), list)
        and bool(payload["results"])
        and isinstance(payload.get("attempts"), list)
        and bool(payload["attempts"])
    )


def _prune(directory: Path, *, ttl: int) -> None:
    """Delete expired entries so the cache stays bounded without a separate cleanup step.

    Goes by file mtime, so corrupt entries age out too, and therefore by the
    real clock rather than `_now`: mtimes are written by the real one.
    """
    cutoff = time.time() - ttl
    with contextlib.suppress(OSError):
        for path in directory.glob("*.json"):
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()


# --- clearing ---------------------------------------------------------------

# The exact names sought writes, and nothing else. `_write_json`'s temp files
# are `.<target name>.<random>.tmp`; a crash between mkstemp and replace can
# leave one behind.
_COOLDOWN_NAMES = re.compile(rf"{re.escape(COOLDOWN_FILE)}|\.{re.escape(COOLDOWN_FILE)}\..+\.tmp")
_RESULT_NAMES = re.compile(r"[0-9a-f]{64}\.json|\.[0-9a-f]{64}\.json\..+\.tmp")


@dataclass
class Cleared:
    """What `clear_state` removed, and what it could not."""

    directory: Path
    cooldowns: list[str] = field(default_factory=list)
    results: int = 0
    # An expired cooldown file, leftover temp files: removed, but nothing was
    # being held back by them, so they are not reported as cooldowns.
    stale: int = 0
    failed: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def removed_anything(self) -> bool:
        """Whether any file was actually deleted."""
        return bool(self.cooldowns or self.results or self.stale)


def clear_state() -> Cleared:
    """Delete sought's own state files, and only those.

    For the user who has fixed what caused a refusal and should not have to
    wait out the cooldown, or hand-edit a file to stop waiting.

    Deliberately not `rmtree`. `cache_dir()` comes from an environment
    variable, so it is matched name by name against what sought writes, in the
    two directories it writes to. A `results` that is a symlink is not entered,
    and a symlinked entry is unlinked as a link — its target is never touched.
    A file that cannot be removed is reported, not raised: the rest still goes.
    """
    directory = cache_dir()
    out = Cleared(directory)
    live = [c.engine for c in _live_cooldowns()]

    top = _own_files(directory, _COOLDOWN_NAMES, out)
    if top is None:
        # Not a directory, or not ours to list: the subdirectory would fail
        # the same way, and one report of it is enough.
        return out
    for path in top:
        if _remove(path, out):
            if path.name == COOLDOWN_FILE and live:
                out.cooldowns = live
            else:
                out.stale += 1

    results = directory / RESULTS_DIR
    if results.is_symlink():
        out.failed.append((results, "is a symlink; not followed"))
        return out
    for path in _own_files(results, _RESULT_NAMES, out) or []:
        if _remove(path, out):
            if path.name.endswith(".tmp"):
                out.stale += 1
            else:
                out.results += 1
    return out


def _own_files(directory: Path, names: re.Pattern[str], out: Cleared) -> list[Path] | None:
    """Files and symlinks in `directory` whose whole name matches, without following any link.

    A missing directory has nothing in it. One that cannot be listed is
    reported in `out` and returns None, so the caller can tell the two apart.
    """
    try:
        with os.scandir(directory) as it:
            return [
                Path(entry.path)
                for entry in it
                if names.fullmatch(entry.name) and (entry.is_symlink() or entry.is_file(follow_symlinks=False))
            ]
    except FileNotFoundError:
        return []
    except OSError as exc:
        out.failed.append((directory, _reason(exc)))
        return None


def _remove(path: Path, out: Cleared) -> bool:
    """Unlink one file; report rather than raise. Already gone counts as nothing removed."""
    try:
        path.unlink()
    except FileNotFoundError:
        # A concurrent sought pruned it first. Nothing to report either way.
        return False
    except OSError as exc:
        out.failed.append((path, _reason(exc)))
        return False
    return True


def _reason(exc: OSError) -> str:
    return exc.strerror or exc.__class__.__name__


# --- file I/O ---------------------------------------------------------------


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _read_json(path: Path) -> object:
    """Parse a state file, or return None if it is missing, unreadable or corrupt."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: object) -> None:
    """Replace `path` atomically, or silently do nothing.

    Temp file in the same directory, then `os.replace`, so a reader racing
    this write sees the old file or the new one and never half of either.
    """
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        Path(tmp).replace(path)
        tmp = None
    except (OSError, TypeError, ValueError):
        pass
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()

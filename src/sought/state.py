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
import tempfile
import time
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
_CACHE_FORMAT = 1

# Only the general engines are scraped, so only they can be hammered. A
# vertical source that rate limits is a documented API saying "slow down" for
# this one request, and the next request is cheap to make.
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


def cooling_down(names: Iterable[str], *, config: Config) -> dict[str, Attempt]:
    """Return a ready-made `rate_limited` attempt for each engine still cooling down.

    The state file is read once per search, not once per engine.
    """
    if config.cooldown_seconds <= 0:
        return {}
    entries = _read_cooldowns()
    now = _now()
    out: dict[str, Attempt] = {}
    for name in names:
        entry = entries.get(name)
        if name in _COOLED and entry is not None and entry["until"] > now:
            out[name] = Attempt(name, "rate_limited", _cooldown_detail(entry))
    return out


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


def _cooldown_detail(entry: dict[str, Any]) -> str:
    return (
        f"cooling down until {_clock(entry['until'])}, not asked this time "
        f"(last refusal at {_clock(entry['at'])}: {entry['detail']})"
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

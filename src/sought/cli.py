"""Command line: argument parsing, subcommand dispatch, exit codes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from sought import __version__
from sought import config as config_mod
from sought.engines.base import GENERAL_ENGINES, OPT_IN_ENGINES, VERTICAL_SOURCES, all_source_names
from sought.errors import ConfigError, InvariantError, NoResultsError, SoughtError, UpstreamError, UsageError
from sought.output import print_fetch_pretty, print_json, print_pretty
from sought.skill import DEFAULT_SKILL_ROOT, install_skill, locate_skill

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sought.config import Config

# In GENERAL_ENGINES order, so the help reads the same on every run.
_OPT_IN = ", ".join(e for e in GENERAL_ENGINES if e in OPT_IN_ENGINES)

EPILOG = f"""\
commands:
  sought <query>            search (the default — no subcommand needed)
  sought fetch <url>        read one page and return its content as markdown
  sought doctor             check that sought can actually work here
  sought config             show, create, or locate the config file; clear state
  sought install-skill      install the bundled Claude skill

  Each takes its own --help. A leading bare `fetch`, `doctor`, `config` or
  install-skill is read as the subcommand, so quote a query that starts
  with one: sought "doctor who", sought "fetch api polyfill".

sources:
  general (fallback chain, tried in order): {", ".join(e for e in GENERAL_ENGINES if e not in OPT_IN_ENGINES)}
  general, opt-in (off by default):         {_OPT_IN} — see [google] and [brave_api]
                                            in `sought config --init`
  vertical (exact, keyless APIs):           {", ".join(VERTICAL_SOURCES)}

examples:
  sought "playwright stealth detection"
  sought "rust async runtime" --pretty
  sought "MCP web search" --source hn
  sought fetch https://docs.python.org/3/library/textwrap.html --pretty
  sought doctor --offline
  sought config --init
  sought config --clear-state
"""


def entrypoint() -> None:
    """Console-script entry point."""
    for stream in (sys.stdout, sys.stderr):
        ensure_utf8(stream)
    sys.exit(main(sys.argv[1:]))


def ensure_utf8(stream: object) -> None:
    """Make a text stream write UTF-8, so no result title can crash the output.

    Python encodes a redirected stdout with the locale's code page, and on
    Windows that is a legacy one (cp936, cp1252, ...). Measured on a cp936
    machine: `--pretty` and `doctor` died with UnicodeEncodeError on the first
    `✓`, and the JSON output dies the same way on any title outside the code
    page. A crash is exit 1, which is "searched fine, found nothing" — a false
    statement — and doctor lost its probe results after paying for them.

    UTF-8 rather than `errors="replace"` on the native code page: JSON is
    UTF-8 by definition, and replacement would quietly rewrite result titles.
    Interactive Windows consoles already get UTF-8 from Python, so this only
    changes pipes and files.
    """
    encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
    reconfigure = getattr(stream, "reconfigure", None)
    if encoding != "utf8" and reconfigure is not None:
        # Keep the stream's own error handler. Given only `encoding`,
        # reconfigure resets it to "strict", which would turn stderr's
        # backslashreplace into a new way to crash with exit 1.
        reconfigure(encoding="utf-8", errors=getattr(stream, "errors", None) or "strict")


def main(argv: Sequence[str]) -> int:
    """Run sought and return the process exit code."""
    try:
        return _dispatch(list(argv))
    except SoughtError as exc:
        print(f"sought: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("sought: interrupted", file=sys.stderr)
        return 130


SUBCOMMANDS = ("install-skill", "doctor", "config", "fetch")


def _dispatch(argv: list[str]) -> int:
    if _names_subcommand(argv):
        handler = {"install-skill": _install_skill, "doctor": _doctor, "config": _config, "fetch": _fetch}[argv[0]]
        return handler(argv[1:])
    return _search(argv)


def _names_subcommand(argv: list[str]) -> bool:
    """Decide whether argv[0] names a subcommand rather than opening a query.

    A first bare word that matches wins, as in git. Every subcommand name is
    also an ordinary English word, so `sought doctor who` runs the doctor
    rather than searching — quoting (`sought "doctor who"`) reaches the
    search path, and the help says so. Inferring intent from the shape of the
    remaining arguments was tried and is worse: `--dest PATH` is
    indistinguishable from two search terms.
    """
    return bool(argv) and argv[0] in SUBCOMMANDS


def _search(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sought",
        description="Search the web, with an explicit audit trail of every engine consulted.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", nargs="*", help="search terms")
    parser.add_argument(
        "--source",
        metavar="NAME",
        choices=all_source_names(),
        help="query exactly one source instead of the fallback chain",
    )
    parser.add_argument("--limit", type=int, metavar="N", help="maximum results (default from config)")
    parser.add_argument("--timeout", type=int, metavar="SEC", help="per-engine timeout (default from config)")
    parser.add_argument("--pretty", action="store_true", help="human-readable output instead of JSON")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "ignore a cached result for this exact search and ask the engines again (the new result is still "
            "cached). Engines cooling down after a rate limit stay skipped: asking them early prolongs the ban. "
            "Once the cause is fixed, `sought config --clear-state` ends the cooldowns"
        ),
    )
    parser.add_argument("--version", action="version", version=f"sought {__version__}")
    args = parser.parse_args(argv)

    if not args.query:
        parser.print_help()
        return 0

    query = " ".join(args.query)
    cfg = _load_config(limit=args.limit, timeout=args.timeout)

    from sought.search import run

    payload = run(query, config=cfg, source=args.source, cache="refresh" if args.fresh else "use")

    if args.pretty:
        print_pretty(payload)
    else:
        print_json(payload)

    if payload["results"]:
        return 0

    # No results and nothing in the audit trail is the one output sought must
    # never produce, and it cannot be allowed to fall through to the check
    # below: `all()` over an empty list is True, so an unexplained zero would
    # be reported as the clean, well-understood "the web has no answer".
    if not payload["attempts"]:
        msg = f"no engine was consulted for {query!r} and nothing says why — this is a bug in sought"
        raise InvariantError(msg)

    # Nothing to show. Which of the two zero-result meanings applies decides
    # the exit code, and the caller needs to be able to tell them apart.
    if all(a["status"] in {"ok", "empty"} for a in payload["attempts"]):
        raise NoResultsError(f"no results for {query!r}")
    reasons = ", ".join(f"{a['engine']}={a['status']}" for a in payload["attempts"] if a["status"] != "ok")
    raise UpstreamError(f"every source failed ({reasons})")


def _fetch(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sought fetch",
        description=(
            "Fetch one URL over plain HTTP and return its readable content as markdown (HTML), or verbatim "
            "(text, markdown, JSON). Failures are classified, never an empty page: exit 2 with the reason in "
            "the output. Pages that need JavaScript are reported as such, not rendered."
        ),
    )
    parser.add_argument("url", help="an absolute http:// or https:// URL")
    parser.add_argument(
        "--max-chars",
        type=int,
        metavar="N",
        help="cut content after N characters, always disclosed as `truncated` (default from config; 0 = no limit)",
    )
    parser.add_argument("--timeout", type=int, metavar="SEC", help="request timeout (default from config)")
    parser.add_argument("--pretty", action="store_true", help="human-readable output instead of JSON")
    args = parser.parse_args(argv)

    from sought import fetch

    url = fetch.check_url(args.url)
    cfg = _load_config(timeout=args.timeout)
    max_chars = cfg.fetch.max_chars if args.max_chars is None else _checked_max_chars(args.max_chars)

    payload = fetch.fetch(url, config=cfg, max_chars=max_chars)
    if args.pretty:
        print_fetch_pretty(payload)
    else:
        print_json(payload)

    if payload["status"] == "ok":
        return 0
    raise UpstreamError(f"fetch failed: {payload['status']} — {payload['detail']}")


def _checked_max_chars(value: int) -> int:
    try:
        return config_mod.non_negative_int(value, "--max-chars")
    except ConfigError as exc:
        raise UsageError(f"{exc} (got {value})") from exc


def _doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sought doctor", description="Check that sought can actually work here.")
    parser.add_argument("--offline", action="store_true", help="skip live engine probes")
    parser.add_argument("--fix", action="store_true", help="install a missing chromium")
    args = parser.parse_args(argv)

    from sought import doctor

    return doctor.run(_load_config(), offline=args.offline, fix=args.fix)


def _config(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sought config", description="Show or create the config file, or clear sought's cached state."
    )
    # Exclusive rather than ranked: `--init --clear-state` silently doing one
    # of the two would be the quiet partial success this tool exists to avoid.
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--init", action="store_true", help="write the annotated default config if absent")
    action.add_argument("--path", action="store_true", help="print the config path and exit")
    action.add_argument(
        "--clear-state",
        action="store_true",
        help=(
            "end every engine cooldown and drop cached results now, e.g. after fixing the network. "
            "Deletes only sought's own files in its cache directory"
        ),
    )
    args = parser.parse_args(argv)

    if args.clear_state:
        return _clear_state()

    if args.path:
        print(config_mod.CONFIG_PATH)
        return 0

    if args.init:
        path, created = config_mod.ensure_config_file()
        print(f"{'created' if created else 'already exists'}: {path}")
        return 0

    path = config_mod.CONFIG_PATH
    if not path.exists():
        print(f"{path} does not exist — using builtin defaults.")
        print("Run `sought config --init` to write an annotated one.")
        return 0
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def _clear_state() -> int:
    """Clear cooldowns and the result cache, and say exactly what went.

    Takes no config on purpose: a broken config file must not stand between
    the user and unblocking their engines. A file that could not be removed
    exits 2 — a leftover cooldown keeps an engine skipped, and a script that
    clears and then searches should be able to tell.
    """
    from sought import state

    cleared = state.clear_state()
    if cleared.removed_anything:
        parts = []
        if cleared.cooldowns:
            n = len(cleared.cooldowns)
            parts.append(f"{n} cooldown{'s' if n > 1 else ''} ({', '.join(cleared.cooldowns)})")
        if cleared.results:
            parts.append(f"{cleared.results} cached result{'s' if cleared.results > 1 else ''}")
        if cleared.stale:
            parts.append(f"{cleared.stale} stale file{'s' if cleared.stale > 1 else ''}")
        print(f"cleared: {', '.join(parts)} — {cleared.directory}")
    elif not cleared.failed:
        print(f"nothing to clear — {cleared.directory}")

    for path, reason in cleared.failed:
        print(f"sought: could not remove {path}: {reason}", file=sys.stderr)
    return 2 if cleared.failed else 0


def _install_skill(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sought install-skill", description="Install the bundled Claude skill.")
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_SKILL_ROOT,
        help=f"skills root (default {DEFAULT_SKILL_ROOT})",
    )
    parser.add_argument("--force", action="store_true", help="overwrite an existing SKILL.md")
    parser.add_argument("--print", dest="show", action="store_true", help="print the skill instead of installing it")
    args = parser.parse_args(argv)

    if args.show:
        source, branch = locate_skill()
        print(f"# source: {source} ({branch})", file=sys.stderr)
        print(source.read_text(encoding="utf-8"), end="")
        return 0

    dest = install_skill(dest_root=args.dest, force=args.force)
    print(f"installed: {dest}")
    return 0


def _load_config(*, limit: int | None = None, timeout: int | None = None) -> Config:
    """Load the config file and apply CLI overrides, which win."""
    from dataclasses import replace

    cfg = config_mod.load()
    overrides = {}
    if limit is not None:
        overrides["limit"] = _checked(limit, "--limit")
    if timeout is not None:
        overrides["timeout_seconds"] = _checked(timeout, "--timeout")
    return replace(cfg, **overrides) if overrides else cfg


def _checked(value: int, flag: str) -> int:
    """Hold a CLI override to the same rule the config file is held to.

    `dataclasses.replace` writes straight into the frozen Config and never
    passes config.py's validation, so these used to arrive unchecked and fail
    somewhere far away and misleadingly: `--limit 0` slices every engine to
    nothing and is reported as `empty`, a false statement about the web rather
    than about the command line; `--timeout 0` means "no timeout" to Playwright
    and hangs indefinitely; `--timeout -3` surfaces as a raw Node.js
    TimeoutNegativeWarning.
    """
    try:
        return config_mod.positive_int(value, flag)
    except ConfigError as exc:
        raise UsageError(f"{exc} (got {value})") from exc

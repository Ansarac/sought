"""Command line: argument parsing, subcommand dispatch, exit codes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from scout import __version__
from scout import config as config_mod
from scout.engines.base import GENERAL_ENGINES, VERTICAL_SOURCES, all_source_names
from scout.errors import NoResultsError, ScoutError, UpstreamError
from scout.output import print_json, print_pretty
from scout.skill import DEFAULT_SKILL_ROOT, install_skill, locate_skill

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scout.config import Config

EPILOG = f"""\
sources:
  general (fallback chain, tried in order): {", ".join(GENERAL_ENGINES)}
  vertical (exact, keyless APIs):           {", ".join(VERTICAL_SOURCES)}

examples:
  scout "playwright stealth detection"
  scout "rust async runtime" --pretty
  scout "MCP web search" --source hn
  scout doctor
"""


def entrypoint() -> None:
    """Console-script entry point."""
    sys.exit(main(sys.argv[1:]))


def main(argv: Sequence[str]) -> int:
    """Run scout and return the process exit code."""
    try:
        return _dispatch(list(argv))
    except ScoutError as exc:
        print(f"scout: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("scout: interrupted", file=sys.stderr)
        return 130


def _dispatch(argv: list[str]) -> int:
    if argv and argv[0] == "install-skill":
        return _install_skill(argv[1:])
    if argv and argv[0] == "doctor":
        return _doctor(argv[1:])
    if argv and argv[0] == "config":
        return _config(argv[1:])
    return _search(argv)


def _search(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="scout",
        description="Search the web from a network that fights back.",
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
    parser.add_argument("--version", action="version", version=f"scout {__version__}")
    args = parser.parse_args(argv)

    if not args.query:
        parser.print_help()
        return 0

    query = " ".join(args.query)
    cfg = _load_config(limit=args.limit, timeout=args.timeout)

    from scout.search import run

    payload = run(query, config=cfg, source=args.source)

    if args.pretty:
        print_pretty(payload)
    else:
        print_json(payload)

    if payload["results"]:
        return 0

    # Nothing to show. Which of the two zero-result meanings applies decides
    # the exit code, and the caller needs to be able to tell them apart.
    if all(a["status"] in {"ok", "empty"} for a in payload["attempts"]):
        raise NoResultsError(f"no results for {query!r}")
    reasons = ", ".join(f"{a['engine']}={a['status']}" for a in payload["attempts"] if a["status"] != "ok")
    raise UpstreamError(f"every source failed ({reasons})")


def _doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="scout doctor", description="Check that scout can actually work here.")
    parser.add_argument("--offline", action="store_true", help="skip live engine probes")
    parser.add_argument("--fix", action="store_true", help="install a missing chromium")
    args = parser.parse_args(argv)

    from scout import doctor

    return doctor.run(_load_config(), offline=args.offline, fix=args.fix)


def _config(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="scout config", description="Show or create the config file.")
    parser.add_argument("--init", action="store_true", help="write the annotated default config if absent")
    parser.add_argument("--path", action="store_true", help="print the config path and exit")
    args = parser.parse_args(argv)

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
        print("Run `scout config --init` to write an annotated one.")
        return 0
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def _install_skill(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="scout install-skill", description="Install the bundled Claude skill.")
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
        overrides["limit"] = limit
    if timeout is not None:
        overrides["timeout_seconds"] = timeout
    return replace(cfg, **overrides) if overrides else cfg

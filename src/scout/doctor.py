"""Self-check.

Design rule, learned from a tool that reported "Overall: OK" while its browser
was missing: any failed *required* check fails the whole run and sets a
non-zero exit code. A green summary must mean the tool works.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from scout import config as config_mod
from scout.engines.base import GENERAL_ENGINES

if TYPE_CHECKING:
    from scout.config import Config

MIN_PYTHON = (3, 13)


@dataclass
class Check:
    """One diagnostic line."""

    name: str
    ok: bool
    detail: str
    required: bool = True

    @property
    def mark(self) -> str:
        """Glyph for the report: required failures read differently from optional ones."""
        if self.ok:
            return "✓"
        return "✗" if self.required else "!"


def run(cfg: Config, *, offline: bool = False, fix: bool = False) -> int:
    """Print the diagnostic report and return the process exit code."""
    checks: list[Check] = [_python(), _playwright_package()]

    chromium = _chromium(fix=fix)
    checks.append(chromium)
    checks.extend(_config_checks(cfg))
    checks.append(_tls_note(cfg))
    checks.extend(_proxy_checks(cfg))

    if offline:
        checks.append(Check("engines", ok=True, detail="skipped (--offline)", required=False))
    else:
        checks.extend(_engine_probes(cfg, chromium_ok=chromium.ok))

    print("scout doctor\n")
    for c in checks:
        print(f"  {c.mark} {c.name}: {c.detail}")

    failed = [c for c in checks if not c.ok and c.required]
    print()
    if failed:
        print(f"Overall: FAILED ({len(failed)} required check{'s' if len(failed) > 1 else ''})")
        return 2
    warned = [c for c in checks if not c.ok]
    suffix = f" ({len(warned)} warning{'s' if len(warned) > 1 else ''})" if warned else ""
    print(f"Overall: OK{suffix}")
    return 0


def _python() -> Check:
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PYTHON
    return Check("python", ok=ok, detail=f"{v.major}.{v.minor}.{v.micro}" + ("" if ok else " (need >= 3.13)"))


def _playwright_package() -> Check:
    try:
        import playwright

        version = getattr(playwright, "__version__", "installed")
    except ImportError:
        return Check("playwright package", ok=False, detail="not installed (pip install playwright)")
    return Check("playwright package", ok=True, detail=str(version))


def _chromium(*, fix: bool) -> Check:
    path = _chromium_path()
    if path is not None:
        return Check("chromium binary", ok=True, detail=path)

    if not fix:
        return Check(
            "chromium binary",
            ok=False,
            detail="not found — run `scout doctor --fix` or `playwright install chromium`",
        )

    print("installing chromium ...", flush=True)
    result = subprocess.run(  # fixed argv, no shell
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
        capture_output=True,
        text=True,
    )
    path = _chromium_path()
    if path is not None:
        return Check("chromium binary", ok=True, detail=f"{path} (just installed)")
    tail = (result.stderr or result.stdout or "").strip().splitlines()
    return Check("chromium binary", ok=False, detail=f"install failed: {tail[-1] if tail else 'unknown error'}")


# Read the browser path in a child process. Starting the Playwright driver
# in-process only to read a path and then tearing it down leaves a pending
# asyncio task, which prints a TargetClosedError traceback to stderr at exit.
# Muting stderr in a diagnostic tool would be the wrong trade, so pay for a
# subprocess instead — doctor is not a hot path.
_PATH_PROBE = "from playwright.sync_api import sync_playwright as s\nwith s() as p: print(p.chromium.executable_path)"


def _chromium_path() -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell, no external input
            [sys.executable, "-c", _PATH_PROBE],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    return path if path and Path(path).exists() else None


def _config_checks(cfg: Config) -> list[Check]:
    path = config_mod.CONFIG_PATH
    if path.exists():
        where = Check("config file", ok=True, detail=str(path))
    else:
        where = Check(
            "config file",
            ok=True,
            detail=f"{path} (absent — using builtin defaults; `scout config --init` writes one)",
            required=False,
        )

    known = set(GENERAL_ENGINES)
    unknown = [e for e in cfg.order if e not in known]
    order = Check(
        "engine order",
        ok=not unknown,
        detail=" → ".join(cfg.order) + (f"  [unknown: {', '.join(unknown)}]" if unknown else ""),
    )
    return [where, order]


def _tls_note(cfg: Config) -> Check:
    if not cfg.browser_ignore_cert_errors:
        return Check(
            "browser TLS",
            ok=False,
            detail=(
                "tls.browser_ignore_cert_errors = false. Correct on a clean network. "
                "Behind a TLS-inspecting proxy it disables brave and startpage entirely, "
                "because Chromium does not read the OS trust store."
            ),
            required=False,
        )
    return Check(
        "browser TLS",
        ok=True,
        detail="chromium accepts the local proxy CA (tls.browser_ignore_cert_errors = true)",
    )


def _proxy_checks(cfg: Config) -> list[Check]:
    import os

    if cfg.proxy.any_set:
        detail = f"config: https={cfg.proxy.https or '-'} http={cfg.proxy.http or '-'}"
    else:
        env = {k: os.environ.get(k, "") for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")}
        live = [f"{k}={v}" for k, v in env.items() if v]
        detail = "inherited from environment: " + (", ".join(live) if live else "none set (direct connection)")
    return [Check("proxy", ok=True, detail=detail, required=False)]


def _engine_probes(cfg: Config, *, chromium_ok: bool) -> list[Check]:
    """Ask each configured engine one cheap real query."""
    from scout.search import run as search_run

    checks: list[Check] = []
    probed = 0
    for name in cfg.order:
        if name not in GENERAL_ENGINES:
            continue
        if not chromium_ok and name in {"brave", "startpage"}:
            checks.append(Check(f"engine {name}", ok=False, detail="skipped: no chromium", required=False))
            continue
        probed += 1
        payload = search_run("scout self test", config=cfg, source=name)
        attempt = payload["attempts"][-1] if payload["attempts"] else {"status": "error", "detail": "no attempt"}
        ok = attempt["status"] == "ok"
        detail = f"{attempt.get('count', 0)} results" if ok else f"{attempt['status']} — {attempt.get('detail', '')}"
        # An individual engine being down is exactly what the chain is for, so
        # a probe failure is a warning; only a chain with nothing left is fatal.
        checks.append(Check(f"engine {name}", ok=ok, detail=detail, required=False))

    # Only meaningful once something was actually asked. Reporting "every
    # engine failed" for engines that were skipped would blame the wrong thing;
    # the missing-chromium check has already failed the run in that case.
    if probed and not any(c.ok for c in checks):
        checks.append(Check("engine chain", ok=False, detail="every configured engine failed"))
    return checks

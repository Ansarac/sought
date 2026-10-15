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

from sought import config as config_mod
from sought.engines.base import BROWSER_ENGINES, GENERAL_ENGINES

if TYPE_CHECKING:
    from sought.config import Config

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
    checks.extend(_proxy_checks(cfg))

    # Probe first, report after: whether browser TLS is worth mentioning
    # depends on whether the browser engines are actually failing on a
    # certificate, and the probes are the only thing that knows.
    if offline:
        probes = [Check("engines", ok=True, detail="skipped (--offline)", required=False)]
    else:
        probes = _engine_probes(cfg, chromium_ok=chromium.ok)

    tls = _tls_note(cfg, probes=probes)
    if tls is not None:
        checks.append(tls)
    checks.extend(probes)

    print("sought doctor\n")
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
            detail="not found — run `sought doctor --fix` or `playwright install chromium`",
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
            detail=f"{path} (absent — using builtin defaults; `sought config --init` writes one)",
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


# Substrings of a Playwright/Chromium certificate rejection, lowercased.
_CERT_ERROR_MARKERS = ("err_cert", "certificate", "ssl")


def _tls_note(cfg: Config, *, probes: list[Check]) -> Check | None:
    """Report on browser certificate verification — but only when there is something to report.

    Verification is on by default, which is right, so three situations have to
    read differently:

    * On, and the browser engines are fine. Nothing to say. Warning every
      healthy install about a TLS-inspecting proxy it does not have is how a
      report full of harmless `!` marks teaches people to skip the report.
    * On, and a browser engine just failed on a certificate. The one case worth
      surfacing, with the remedy and with what the remedy costs.
    * Off. A real security posture, restated every run so it cannot quietly
      become permanent.

    Never required: none of these should fail a run on their own. A browser
    engine that cannot connect is already reported as its own probe, and the
    chain exists so that one engine down is survivable.
    """
    if cfg.browser_ignore_cert_errors:
        return Check(
            "browser TLS",
            ok=False,
            detail=(
                "certificate verification is DISABLED for the browser "
                "(tls.browser_ignore_cert_errors = true) — brave and startpage will accept any "
                "certificate, a hostile one included. Correct behind a TLS-inspecting proxy, "
                "wrong on a network you do not control."
            ),
            required=False,
        )

    failing = _cert_failures(probes)
    if failing:
        return Check(
            "browser TLS",
            ok=False,
            detail=(
                f"{', '.join(failing)} failed on a certificate. Chromium reads its own NSS "
                "database, not the OS trust store, so behind a TLS-inspecting proxy it rejects "
                "a CA the rest of this machine trusts. Import that CA and verification stays on:\n"
                "      certutil -d sql:$HOME/.pki/nssdb -A -t 'C,,' -n proxy-ca -i <ca>.crt\n"
                "      (needs libnss3-tools; `certutil -d sql:$HOME/.pki/nssdb -N --empty-password` "
                "first if the database does not exist yet)\n"
                "      Only if that is not possible, tls.browser_ignore_cert_errors = true makes "
                "the browser accept any certificate instead. marginalia and the vertical sources "
                "are unaffected either way."
            ),
            required=False,
        )

    return None


def _cert_failures(probes: list[Check]) -> list[str]:
    """Names of the browser engines whose probe failed on a certificate."""
    wanted = {f"engine {name}" for name in BROWSER_ENGINES}
    return [
        c.name.removeprefix("engine ")
        for c in probes
        if c.name in wanted and not c.ok and any(m in c.detail.lower() for m in _CERT_ERROR_MARKERS)
    ]


def _proxy_checks(cfg: Config) -> list[Check]:
    import os

    # Both transports go through whatever is reported here, and saying so is
    # the point: a green proxy line above a red `marginalia: Connection
    # refused` should tell the reader where to look, not just that a variable
    # was set.
    reach = "HTTP client and browser"
    if cfg.proxy.any_set:
        detail = (
            f"config ({reach}): https={_redact(cfg.proxy.https) or '-'} http={_redact(cfg.proxy.http) or '-'}"
            f" no_proxy={cfg.proxy.no_proxy_value() or '-'}"
        )
    else:
        env = {k: os.environ.get(k, "") for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")}
        live = [f"{k}={_redact(v)}" for k, v in env.items() if v]
        detail = (
            f"inherited from environment ({reach}): " + ", ".join(live)
            if live
            else "none set — all engines connect directly"
        )
    return [Check("proxy", ok=True, detail=detail, required=False)]


def _redact(url: str) -> str:
    """Strip the `user:password@` out of a proxy URL, keeping the rest readable.

    The documented troubleshooting flow is "run doctor, paste the output into
    the bug report", so anything printed here should be assumed to be heading
    for a public issue tracker. Scheme, host and port are what makes the line
    worth printing at all and stay; credentials do not.

    Split on the *last* `@`, because a password may contain both `@` and `:`.
    """
    if not url:
        return url
    scheme, sep, rest = url.partition("://")
    if not sep:
        scheme, rest = "", url
    if "@" not in rest:
        return url
    _userinfo, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}" if sep else f"***@{host}"


def _engine_probes(cfg: Config, *, chromium_ok: bool) -> list[Check]:
    """Ask each configured engine one cheap real query."""
    from sought.search import run as search_run

    checks: list[Check] = []
    probed = 0
    for name in cfg.order:
        if name not in GENERAL_ENGINES:
            continue
        if not chromium_ok and name in {"brave", "startpage"}:
            checks.append(Check(f"engine {name}", ok=False, detail="skipped: no chromium", required=False))
            continue
        probed += 1
        # Never from the result cache: a replayed probe would report an engine
        # as working without having asked it. Cooldowns still apply — a probe
        # is a real request, and an engine cooling down is reported as such.
        payload = search_run("sought self test", config=cfg, source=name, cache="off")
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

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
from sought import net, state
from sought.engines.base import BROWSER_ENGINES, GENERAL_ENGINES, OWN_BROWSER_ENGINES

if TYPE_CHECKING:
    from sought.config import Config
    from sought.state import Budget, Cooldown

MIN_PYTHON = (3, 13)

CLEAR_STATE = "sought config --clear-state"


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

    # Read before probing, so the line reports the state doctor found rather
    # than one its own probes may have just changed, and so the probes can
    # refer to it instead of repeating it.
    cooling = state.active_cooldowns(config=cfg)
    note = _cooldown_note(cooling)
    if note is not None:
        checks.append(note)

    # Probe first, report after: whether browser TLS is worth mentioning
    # depends on whether the browser engines are actually failing on a
    # certificate, and the probes are the only thing that knows.
    if offline:
        probes = [Check("engines", ok=True, detail="skipped (--offline)", required=False)]
    else:
        probes = _engine_probes(cfg, chromium_ok=chromium.ok, cooling=cooling)
    google = _google_line(cfg, offline=offline, chromium_ok=chromium.ok, cooling=cooling)
    if google is not None:
        probes.append(google)

    tls = _tls_note(cfg, probes=probes)
    if tls is not None:
        checks.append(tls)
    route = _route_note(cfg, probes=probes)
    if route is not None:
        checks.append(route)
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
                f"{', '.join(failing)} failed on a certificate. {_ca_import_hint()}\n"
                "      Only if that is not possible, tls.browser_ignore_cert_errors = true makes "
                "the browser accept any certificate instead. marginalia and the vertical sources "
                "are unaffected either way."
            ),
            required=False,
        )

    return None


def _ca_import_hint() -> str:
    """Where Chromium looks for a proxy CA on this platform, and how to put one there.

    The Linux remedy must not be printed elsewhere. On Windows `certutil` is a
    different program with the same name, so the NSS command line fails or
    does something else entirely — and the premise is wrong there too: Chromium
    on Windows verifies against the Windows certificate store, the same one
    `truststore` gives requests. Measured on a TLS-inspecting corporate proxy
    whose CA was in the Windows store: no certificate error from either
    browser engine, with verification on.
    """
    if sys.platform == "win32":
        return (
            "Chromium on Windows verifies against the Windows certificate store, so the proxy's CA "
            "is missing from it. Import the root of the chain into your user store and verification "
            "stays on (requests reads the same store):\n"
            "      certutil -user -addstore Root <ca>.crt\n"
            "      (or certmgr.msc → Trusted Root Certification Authorities → Import)"
        )
    if sys.platform == "darwin":
        return (
            "Chromium on macOS verifies against the system keychain, so the proxy's CA is missing "
            "from it. Import the root of the chain and verification stays on:\n"
            "      security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db <ca>.crt"
        )
    return (
        "Chromium reads its own NSS database, not the OS trust store, so behind a TLS-inspecting "
        "proxy it rejects a CA the rest of this machine trusts. Import that CA and verification stays on:\n"
        "      certutil -d sql:$HOME/.pki/nssdb -A -t 'C,,' -n proxy-ca -i <ca>.crt\n"
        "      (needs libnss3-tools; `certutil -d sql:$HOME/.pki/nssdb -N --empty-password` "
        "first if the database does not exist yet)"
    )


# Statuses that point at the route rather than at the engine's own answer.
_ROUTE_FAILURES = frozenset({"blocked", "timeout", "error"})
_HTTP_ENGINES = frozenset({"marginalia"})


def _route_note(cfg: Config, *, probes: list[Check]) -> Check | None:
    """Say when a PAC script is the likely reason the HTTP client cannot connect.

    Only when it is biting, for the reason `_tls_note` gives: a PAC script
    sends the browser and the HTTP client down different routes, and on many
    networks direct egress works anyway, so warning on every PAC machine would
    be a permanent `!` nobody reads. A plain-HTTP engine failing to connect on
    a machine whose browser follows a PAC is the case worth a line.
    """
    if cfg.proxy.any_set or _environment_proxies():
        return None
    system = net.system_proxy()
    if system is None or not system.pac_url or system.server:
        return None
    failing = [
        c.name.removeprefix("engine ")
        for c in probes
        if c.name.removeprefix("engine ") in _HTTP_ENGINES
        and not c.ok
        and c.detail.split(" — ", 1)[0] in _ROUTE_FAILURES
    ]
    if not failing:
        return None
    return Check(
        "proxy route",
        ok=False,
        detail=(
            f"{', '.join(failing)} could not connect. The browser engines follow the Windows PAC script "
            f"({_redact(system.pac_url)}); the HTTP client cannot evaluate one and connects directly. Name the "
            "proxy the PAC script returns in the config file, and both transports use it:\n"
            "      [proxy]\n"
            '      https = "http://<proxy-host>:<port>"\n'
            "      The vertical sources (--source) take the same route as marginalia."
        ),
        required=False,
    )


def _cert_failures(probes: list[Check]) -> list[str]:
    """Names of the browser engines whose probe failed on a certificate."""
    wanted = {f"engine {name}" for name in BROWSER_ENGINES | OWN_BROWSER_ENGINES}
    return [
        c.name.removeprefix("engine ")
        for c in probes
        if c.name in wanted and not c.ok and any(m in c.detail.lower() for m in _CERT_ERROR_MARKERS)
    ]


def _cooldown_note(cooling: list[Cooldown]) -> Check | None:
    """Report engines that searches are skipping right now — and nothing when there are none.

    Silence when nothing is cooling down (or cooldowns are off) follows
    `_tls_note`: a `✓ cooldowns: none` on every healthy run is a line people
    learn to skip, and then they skip it on the run where it matters.

    A warning, never required. A cooldown is sought holding back on purpose,
    not a broken install, and it ends on its own. The clear command is named
    because the usual reason to read this line is having just fixed the
    network and wanting to know why searches still skip an engine.
    """
    if not cooling:
        return None
    engines = ", ".join(c.describe() for c in cooling)
    return Check("cooldowns", ok=False, detail=f"{engines}; clear with: {CLEAR_STATE}", required=False)


_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")


def _environment_proxies() -> list[tuple[str, str]]:
    """Return the proxy variables that are set, each once.

    Windows environment names are case-insensitive, so `https_proxy` and
    `HTTPS_PROXY` are one variable there and would otherwise be reported twice.
    """
    import os

    seen: set[str] = set()
    live: list[tuple[str, str]] = []
    for name in _PROXY_VARS:
        key = name.upper() if sys.platform == "win32" else name
        value = os.environ.get(name, "")
        if value and key not in seen:
            seen.add(key)
            live.append((name, value))
    return live


def _proxy_checks(cfg: Config) -> list[Check]:
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
    elif live := _environment_proxies():
        detail = f"inherited from environment ({reach}): " + ", ".join(f"{k}={_redact(v)}" for k, v in live)
    elif (system := net.system_proxy()) is not None:
        detail = _system_proxy_detail(system)
    else:
        detail = "none set — all engines connect directly"
    return [Check("proxy", ok=True, detail=detail, required=False)]


def _system_proxy_detail(system: net.SystemProxy) -> str:
    """Describe the Windows proxy settings by what each transport does with them.

    "None set" would be false here: nothing is in the environment, yet the
    browser does use a proxy. Saying which transport takes which route is the
    whole value of the line.
    """
    if system.pac_url and system.server:
        return (
            f"Windows settings, no environment variable: the browser follows the PAC script {_redact(system.pac_url)}, "
            f"the HTTP client uses the manual proxy {_redact(system.server)} — the two can differ"
        )
    if system.pac_url:
        return (
            f"Windows PAC script {_redact(system.pac_url)}, no environment variable: the browser follows it, "
            "the HTTP client cannot evaluate PAC and connects directly"
        )
    return f"Windows manual proxy (HTTP client and browser): {_redact(system.server)}"


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


def _engine_probes(cfg: Config, *, chromium_ok: bool, cooling: list[Cooldown]) -> list[Check]:
    """Ask each configured engine one cheap real query.

    An engine in `cooling` is not asked — a probe is a real request, and
    asking a refusing engine early is what the cooldown prevents. Its line
    points at the cooldowns line rather than repeating it.

    A disabled opt-in engine named in the order is not probed either, and it
    counts as failed for the chain verdict: every search records an `error`
    for it, so a chain of nothing else is a chain that answers nothing.
    """
    cooled = {c.engine for c in cooling}
    checks: list[Check] = []
    probed = 0
    skipped_cooling: list[str] = []
    for name in cfg.order:
        if name not in GENERAL_ENGINES:
            continue
        if name == "google" and not cfg.google.enabled:
            probed += 1
            checks.append(Check("engine google", ok=False, detail=_google_disabled(in_order=True), required=False))
            continue
        check, outcome = _probe(cfg, name, chromium_ok=chromium_ok, cooled=cooled)
        checks.append(check)
        if outcome == "probed":
            probed += 1
        elif outcome == "cooling":
            skipped_cooling.append(name)

    # Only meaningful once something was actually asked. Reporting "every
    # engine failed" for engines that were skipped would blame the wrong thing;
    # the missing-chromium check has already failed the run in that case.
    #
    # Engines skipped for a cooldown do count: a search right now would get
    # nothing from them either, so the run fails exactly as `sought <query>`
    # would. But the detail has to say so — the likeliest reader has just
    # fixed their network, and "every engine failed" would tell them the fix
    # did not work when nothing was asked.
    if (probed or skipped_cooling) and not any(c.ok for c in checks):
        checks.append(Check("engine chain", ok=False, detail=_chain_failure(probed=probed, cooling=skipped_cooling)))
    return checks


def _probe(cfg: Config, name: str, *, chromium_ok: bool, cooled: set[str]) -> tuple[Check, str]:
    """Probe one general engine; also say whether it was "probed", "cooling" or "skipped"."""
    from sought.search import run as search_run

    if not chromium_ok and name in BROWSER_ENGINES | OWN_BROWSER_ENGINES:
        return Check(f"engine {name}", ok=False, detail="skipped: no chromium", required=False), "skipped"
    if name in cooled:
        detail = "not probed: cooling down (see cooldowns)"
        return Check(f"engine {name}", ok=False, detail=detail, required=False), "cooling"

    before = state.budget(name, per_hour=cfg.google.max_per_hour) if name == "google" else None
    # Never from the result cache: a replayed probe would report an engine
    # as working without having asked it. Cooldowns still apply inside
    # `run` too, should one have started since `cooling` was read.
    payload = search_run("sought self test", config=cfg, source=name, cache="off")
    attempt = payload["attempts"][-1] if payload["attempts"] else {"status": "error", "detail": "no attempt"}
    ok = attempt["status"] == "ok"
    if ok:
        note = f" — {attempt['detail']}" if attempt.get("detail") else ""
        detail = f"{attempt.get('count', 0)} results{note}"
    else:
        detail = f"{attempt['status']} — {attempt.get('detail', '')}"
    if before is not None:
        detail += _google_budget_note(cfg, before=before)
    # An individual engine being down is exactly what the chain is for, so
    # a probe failure is a warning; only a chain with nothing left is fatal.
    return Check(f"engine {name}", ok=ok, detail=detail, required=False), "probed"


GOOGLE_DISABLED = "disabled (opt-in: [google] enabled = true)"


def _google_disabled(*, in_order: bool) -> str:
    if not in_order:
        return GOOGLE_DISABLED
    return f"{GOOGLE_DISABLED} — but engines.order lists it, so every search reaching it records an error"


def _google_budget_note(cfg: Config, *, before: Budget) -> str:
    """Say what the google probe cost, since every probe spends the same budget searches do."""
    after = state.budget("google", per_hour=cfg.google.max_per_hour)
    spent = "this probe used one" if after.used > before.used else "this probe used none"
    return f"; budget {after.describe()} ({spent})"


def _google_line(cfg: Config, *, offline: bool, chromium_ok: bool, cooling: list[Cooldown]) -> Check | None:
    """Report the opt-in google engine wherever the probes have not already.

    Always said, unlike the TLS and cooldown notes: whether sought may hide
    its automation flag from Google is a posture, not a fault, and a reader of
    the report should not have to know the key exists to learn its value.
    Disabled reads as a plain ✓ — it is the default — unless the order names
    google, which makes it a warning. Enabled, it is probed (online, even
    outside the order, since `--source google` reaches it) and the remaining
    budget is shown, because a probe spends it.
    """
    in_order = "google" in cfg.order
    if not cfg.google.enabled:
        if in_order and not offline:
            return None  # `_engine_probes` reported it
        return Check("engine google", ok=not in_order, detail=_google_disabled(in_order=in_order), required=False)
    if offline:
        b = state.budget("google", per_hour=cfg.google.max_per_hour)
        return Check(
            "engine google",
            ok=True,
            detail=f"enabled (automation flag hidden), not probed (--offline); budget {b.describe()}",
            required=False,
        )
    if in_order:
        return None  # probed with the chain
    check, _outcome = _probe(cfg, "google", chromium_ok=chromium_ok, cooled={c.engine for c in cooling})
    check.detail += " [not in engines.order: reached only by --source google]"
    return check


def _chain_failure(*, probed: int, cooling: list[str]) -> str:
    if not cooling:
        return "every configured engine failed"
    names = ", ".join(cooling)
    if not probed:
        return (
            f"every configured engine is cooling down after a rate limit ({names}), so none was asked and "
            f"searches will fail until one cooldown ends. If the cause is fixed, run `{CLEAR_STATE}` "
            "and doctor again"
        )
    return (
        f"every configured engine failed or is cooling down ({names} not asked). If the cause is fixed, "
        f"run `{CLEAR_STATE}` and doctor again"
    )

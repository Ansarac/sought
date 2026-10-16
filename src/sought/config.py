"""Configuration loading from ~/.config/sought/config.toml.

Precedence, highest first: CLI flags > config file > environment > builtin
defaults. The CLI layer applies its own overrides on top of what `load()`
returns, so everything below stops at the config-file level.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sought.errors import ConfigError

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sought"
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULT_ORDER = ("brave", "startpage", "marginalia")
DEFAULT_TIMEOUT = 30
DEFAULT_LIMIT = 10
DEFAULT_COOLDOWN = 600
DEFAULT_CACHE_TTL = 900

DEFAULT_CONFIG_TEMPLATE = """\
# sought configuration — https://github.com/Ansarac/sought
# Precedence: CLI flags > this file > environment variables > builtin defaults.

[proxy]
# Leave both empty to inherit HTTP_PROXY / HTTPS_PROXY / NO_PROXY from the
# environment. Set them here when you want sought to use a proxy that the rest
# of your shell does not.
http = ""
https = ""
no_proxy = ["localhost", "127.0.0.1"]

[engines]
# Fallback chain for general queries, tried left to right. The three defaults
# use independent networks and quotas, so one being rate limited says nothing
# about the next.
order = ["brave", "startpage", "marginalia"]
timeout_seconds = 30
# Maximum results returned per query.
limit = 10
# After an engine answers `rate_limited`, leave it alone for this long. Asking
# again seconds after a refusal does not get an answer; on a shared egress IP
# it prolongs the ban. Skipped engines still appear in the audit trail, as
# `rate_limited` with "cooling down ... not asked this time". 0 disables.
# `sought config --clear-state` ends every cooldown early.
cooldown_seconds = 600

[cache]
# Replay an identical search (same query, source, order and limit) from a
# local cache for this long instead of asking the engines again. Only searches
# that returned results are cached, and a replay carries
# `"cached": {"age_seconds": N}` so it is never mistaken for a fresh search.
# `--fresh` skips the lookup for one search. 0 disables.
ttl_seconds = 900

[tls]
# Verify against the OS trust store instead of certifi's static bundle. Behind
# a TLS-inspecting corporate proxy this is required, and it raises no security
# bar and lowers none — the proxy CA is one your machine already trusts.
# This is the safe knob. Leave it on.
system_trust = true

# DANGER — this one genuinely turns TLS verification OFF, for the browser only.
# It defaults to false and you must opt in deliberately.
#
# Why you might: Chromium does not read the OS trust store, so behind a
# TLS-inspecting proxy every browser engine dies with
# ERR_CERT_AUTHORITY_INVALID. Setting this to true is the only way to use the
# `brave` and `startpage` engines on such a network — at the cost of the
# browser accepting *any* certificate, including a hostile one.
#
# Why you might not: with it false, `brave` and `startpage` simply fail (and
# `sought doctor` will say so plainly), while `marginalia` and every vertical
# source keep working — they go through the `requests` path above, which
# verifies against the OS trust store and is unaffected.
browser_ignore_cert_errors = false
"""


@dataclass(frozen=True)
class ProxyConfig:
    """Proxy settings shared by the HTTP client and the browser."""

    http: str = ""
    https: str = ""
    no_proxy: tuple[str, ...] = ()

    @property
    def any_set(self) -> bool:
        """True when an explicit proxy was configured (not inherited from env)."""
        return bool(self.http or self.https)

    def as_requests_mapping(self) -> dict[str, str] | None:
        """Return a requests-style proxies mapping, or None to inherit from env.

        `no_proxy` is included under the key requests documents for it, but
        requests only consults that key when deciding whether to adopt a proxy
        from the *environment*. Against an explicitly-set proxy it is inert, so
        `sought.net` enforces the bypass itself as well.
        """
        if not self.any_set:
            return None
        mapping = {}
        if self.http:
            mapping["http"] = self.http
        if self.https:
            mapping["https"] = self.https
        bypass = self.no_proxy_value()
        if bypass:
            mapping["no_proxy"] = bypass
        return mapping

    def no_proxy_value(self) -> str:
        """Return the bypass list in the comma-separated form requests and Chromium both expect."""
        return ",".join(self.no_proxy)

    def browser_server(self) -> str | None:
        """Return the configured proxy URL for Playwright, or None when the config names none.

        None does not mean "leave Chromium alone": the browser layer then falls
        back to `sought.net.environment_proxy`, because Chromium under a Linux
        desktop session ignores the proxy environment.
        """
        return self.https or self.http or None


@dataclass(frozen=True)
class Config:
    """Everything sought reads out of the config file."""

    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    order: tuple[str, ...] = DEFAULT_ORDER
    timeout_seconds: int = DEFAULT_TIMEOUT
    limit: int = DEFAULT_LIMIT
    cooldown_seconds: int = DEFAULT_COOLDOWN
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL
    system_trust: bool = True
    browser_ignore_cert_errors: bool = False
    source_path: Path | None = None


def ensure_config_file(path: Path = CONFIG_PATH) -> tuple[Path, bool]:
    """Create the annotated default config if absent.

    Returns the path and whether it was just created.
    """
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return path, True


def load(path: Path = CONFIG_PATH) -> Config:
    """Read the config file, falling back to builtin defaults when absent."""
    if not path.exists():
        return Config()

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path} is not valid TOML: {exc}"
        raise ConfigError(msg) from exc
    except OSError as exc:
        msg = f"cannot read {path}: {exc}"
        raise ConfigError(msg) from exc

    return _from_mapping(raw, path)


def _from_mapping(raw: dict, path: Path | None) -> Config:
    proxy_raw = _table(raw, "proxy", path)
    engines_raw = _table(raw, "engines", path)
    tls_raw = _table(raw, "tls", path)
    cache_raw = _table(raw, "cache", path)

    order = tuple(_str_list(engines_raw.get("order", DEFAULT_ORDER), "engines.order", path))
    if not order:
        msg = f"{path}: engines.order must list at least one engine"
        raise ConfigError(msg)

    return Config(
        proxy=ProxyConfig(
            http=_str(proxy_raw.get("http", ""), "proxy.http", path),
            https=_str(proxy_raw.get("https", ""), "proxy.https", path),
            no_proxy=tuple(_str_list(proxy_raw.get("no_proxy", ()), "proxy.no_proxy", path)),
        ),
        order=order,
        timeout_seconds=positive_int(
            engines_raw.get("timeout_seconds", DEFAULT_TIMEOUT),
            "engines.timeout_seconds",
            path,
        ),
        limit=positive_int(engines_raw.get("limit", DEFAULT_LIMIT), "engines.limit", path),
        cooldown_seconds=_non_negative_int(
            engines_raw.get("cooldown_seconds", DEFAULT_COOLDOWN),
            "engines.cooldown_seconds",
            path,
        ),
        cache_ttl_seconds=_non_negative_int(
            cache_raw.get("ttl_seconds", DEFAULT_CACHE_TTL),
            "cache.ttl_seconds",
            path,
        ),
        system_trust=_bool(tls_raw.get("system_trust", True), "tls.system_trust", path),
        browser_ignore_cert_errors=_bool(
            tls_raw.get("browser_ignore_cert_errors", False),
            "tls.browser_ignore_cert_errors",
            path,
        ),
        source_path=path,
    )


def _table(raw: dict, key: str, path: Path | None) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        msg = f"{path}: [{key}] must be a table"
        raise ConfigError(msg)
    return value


def _str(value: object, key: str, path: Path | None) -> str:
    if not isinstance(value, str):
        msg = f"{path}: {key} must be a string"
        raise ConfigError(msg)
    return value


def positive_int(value: object, key: str, path: Path | None = None) -> int:
    """Validate a positive integer, for the config file and for CLI overrides alike.

    Public because the CLI applies `--limit` / `--timeout` with
    `dataclasses.replace`, which walks straight past this module. A limit of 0
    is not a smaller search and a timeout of 0 is not a shorter one — the first
    makes every engine report `empty` (a false statement about the web) and the
    second means "wait forever" to Playwright.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        where = f"{path}: " if path is not None else ""
        msg = f"{where}{key} must be a positive integer"
        raise ConfigError(msg)
    return value


def _non_negative_int(value: object, key: str, path: Path | None) -> int:
    """Validate a duration where 0 is meaningful: it switches the feature off.

    Kept apart from `positive_int` on purpose. Zero is a valid cooldown or
    cache lifetime and an invalid limit or timeout, and loosening the shared
    validator would let `--limit 0` back in.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        msg = f"{path}: {key} must be a non-negative integer (0 disables)"
        raise ConfigError(msg)
    return value


def _bool(value: object, key: str, path: Path | None) -> bool:
    if not isinstance(value, bool):
        msg = f"{path}: {key} must be true or false"
        raise ConfigError(msg)
    return value


def _str_list(value: object, key: str, path: Path | None) -> list[str]:
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        msg = f"{path}: {key} must be a list of strings"
        raise ConfigError(msg)
    return value

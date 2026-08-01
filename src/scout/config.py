"""Configuration loading from ~/.config/scout/config.toml.

Precedence, highest first: CLI flags > config file > environment > builtin
defaults. The CLI layer applies its own overrides on top of what `load()`
returns, so everything below stops at the config-file level.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from scout.errors import ConfigError

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "scout"
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULT_ORDER = ("brave", "startpage", "marginalia")
DEFAULT_TIMEOUT = 30
DEFAULT_LIMIT = 10

DEFAULT_CONFIG_TEMPLATE = """\
# scout configuration — https://github.com/  (local project)
# Precedence: CLI flags > this file > environment variables > builtin defaults.

[proxy]
# Leave both empty to inherit HTTP_PROXY / HTTPS_PROXY / NO_PROXY from the
# environment. Set them here when you want scout to use a proxy that the rest
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

[tls]
# Verify against the OS trust store instead of certifi's static bundle. Behind
# a TLS-inspecting corporate proxy this is required, and it raises no security
# bar and lowers none — the proxy CA is one your machine already trusts.
system_trust = true

# Chromium does not read the OS trust store, so behind the same proxy every
# browser engine dies with ERR_CERT_AUTHORITY_INVALID. Setting this to false
# disables brave and startpage entirely; `scout doctor` will say so plainly.
browser_ignore_cert_errors = true
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
        """Return a requests-style proxies mapping, or None to inherit from env."""
        if not self.any_set:
            return None
        mapping = {}
        if self.http:
            mapping["http"] = self.http
        if self.https:
            mapping["https"] = self.https
        return mapping

    def browser_server(self) -> str | None:
        """Return the proxy URL for Playwright, which ignores the environment."""
        return self.https or self.http or None


@dataclass(frozen=True)
class Config:
    """Everything scout reads out of the config file."""

    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    order: tuple[str, ...] = DEFAULT_ORDER
    timeout_seconds: int = DEFAULT_TIMEOUT
    limit: int = DEFAULT_LIMIT
    system_trust: bool = True
    browser_ignore_cert_errors: bool = True
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
        timeout_seconds=_int(engines_raw.get("timeout_seconds", DEFAULT_TIMEOUT), "engines.timeout_seconds", path),
        limit=_int(engines_raw.get("limit", DEFAULT_LIMIT), "engines.limit", path),
        system_trust=_bool(tls_raw.get("system_trust", True), "tls.system_trust", path),
        browser_ignore_cert_errors=_bool(
            tls_raw.get("browser_ignore_cert_errors", True),
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


def _int(value: object, key: str, path: Path | None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        msg = f"{path}: {key} must be a positive integer"
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

"""Plain-HTTP client shared by the keyless sources.

Search engines are *not* fetched through here — they detect and block plain
HTTP clients. See `sought.browser` for those.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import requests

    from sought.config import Config

# A generic desktop UA. Several keyless APIs reject the requests default, and
# every one of them is fine with this.
DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


@cache
def _use_system_trust() -> bool:
    """Verify TLS against the OS trust store instead of certifi's static bundle.

    Behind a TLS-inspecting corporate proxy every request otherwise fails with
    CERTIFICATE_VERIFY_FAILED, because the proxy re-signs with a CA that lives
    in the OS store and not in certifi. This raises no security bar and lowers
    none — unlike `verify=False`, which is the wrong fix for the same symptom.
    """
    try:
        import truststore
    except ImportError:
        return False
    truststore.inject_into_ssl()
    return True


def proxy_bypass_list(config: Config) -> str:
    """Return the effective no-proxy list, in the comma-separated form both transports take.

    Config first, environment second. Both transports lose the environment's
    copy the moment sought names a proxy explicitly — requests because
    `trust_env` goes off with it, Chromium because `--proxy-server` supersedes
    the environment it would otherwise have read — so it has to be picked up
    here and handed back to them.

    Lowercase `no_proxy` wins over `NO_PROXY`, matching what Chromium does with
    the conflicting pair.
    """
    return config.proxy.no_proxy_value() or os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""


def environment_proxy() -> str | None:
    """Return the proxy the environment names, for a browser that will not look for itself.

    Chromium on Linux reads `HTTPS_PROXY` only outside a desktop session. Under
    GNOME or KDE it takes the desktop's own proxy settings instead and ignores
    the environment entirely — measured: with gsettings on `auto` and no PAC
    URL, Chromium went direct while `requests` used the proxy, and a firewall
    answered roughly half its requests with HTTP 401. The two transports must
    not disagree about the route, so the browser gets told.

    `getproxies_environment` is what requests itself consults, and it lets
    lowercase names win over uppercase, which is also Chromium's rule.
    """
    from urllib.request import getproxies_environment

    proxies = getproxies_environment()
    return proxies.get("https") or proxies.get("http") or None


@dataclass(frozen=True)
class SystemProxy:
    """The proxy Windows' own settings name, which the two transports read differently."""

    pac_url: str = ""
    server: str = ""  # only when the manual proxy is switched on (ProxyEnable)


_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def system_proxy() -> SystemProxy | None:
    """Return the per-user WinINET proxy settings, or None off Windows or when none are set.

    This is the one route the transports genuinely disagree on, and no
    environment variable shows it. Chromium on Windows follows these settings,
    PAC script included, whenever it is not handed `--proxy-server`. requests
    resolves proxies through `urllib.request.getproxies`, which falls back to
    the registry's `ProxyServer` — but only with `ProxyEnable` on — and never
    evaluates a PAC script. Measured on a PAC-only corporate machine: the
    browser engines went through the proxy, `requests` went direct, and doctor
    said "all engines connect directly".

    WPAD auto-detection lives in an undocumented binary blob and is not read.
    """
    if sys.platform != "win32":
        return None
    import winreg

    values: dict[str, object] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS) as key:
            for name in ("ProxyEnable", "ProxyServer", "AutoConfigURL"):
                with contextlib.suppress(OSError):
                    values[name] = winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None

    pac_url = str(values.get("AutoConfigURL") or "")
    server = str(values.get("ProxyServer") or "") if values.get("ProxyEnable") else ""
    if not pac_url and not server:
        return None
    return SystemProxy(pac_url=pac_url, server=server)


@cache
def _no_proxy_session_class() -> type:
    """Build the Session subclass that enforces a bypass list against an explicit proxy.

    requests consults `no_proxy` only when deciding whether to adopt a proxy
    from the *environment*; one set on the session itself is used for every
    host, bypass list or not. Since sought sets the proxy on the session, this
    override is the only thing that makes `proxy.no_proxy` mean anything.

    Defined lazily and cached because importing requests costs real startup
    time, and `sought config --path` should not pay it.
    """
    import requests

    class NoProxySession(requests.Session):
        """A requests Session that honours a bypass list for explicitly-set proxies."""

        no_proxy: str = ""

        def merge_environment_settings(
            self,
            url: str,
            proxies: object,
            stream: object,
            verify: object,
            cert: object,
        ) -> dict[str, object]:
            """Drop the proxy entirely for a host the bypass list covers."""
            settings = super().merge_environment_settings(url, proxies, stream, verify, cert)
            if self.no_proxy and requests.utils.should_bypass_proxies(url, no_proxy=self.no_proxy):
                settings["proxies"] = {}
            return settings

    return NoProxySession


def session(config: Config) -> requests.Session:
    """Build a configured requests session."""
    import requests

    if config.system_trust:
        _use_system_trust()

    proxies = config.proxy.as_requests_mapping()
    if proxies is None:
        # Nothing configured: inherit the environment's proxies *and* its
        # NO_PROXY, which is what requests does on its own.
        s: requests.Session = requests.Session()
    else:
        s = _no_proxy_session_class()()
        s.no_proxy = proxy_bypass_list(config)
        s.proxies.update(proxies)
        # An explicitly configured proxy must not be quietly widened by the
        # environment — that is the whole point of setting it in the config
        # file. The environment's NO_PROXY is folded into `no_proxy` above
        # rather than discarded along with the rest of `trust_env`.
        s.trust_env = False
    s.headers["User-Agent"] = DEFAULT_UA
    return s


def get_json(
    sess: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> Any:  # noqa: ANN401 — upstream JSON shapes vary per source
    """GET a URL and decode the JSON body, raising for HTTP errors."""
    resp = sess.get(url, params=params, timeout=timeout, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_text(
    sess: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> str:
    """GET a URL and return the decoded body, raising for HTTP errors."""
    resp = sess.get(url, params=params, timeout=timeout, headers=headers)
    resp.raise_for_status()
    return resp.text

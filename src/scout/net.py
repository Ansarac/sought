"""Plain-HTTP client shared by the keyless sources.

Search engines are *not* fetched through here — they detect and block plain
HTTP clients. See `scout.browser` for those.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import requests

    from scout.config import Config

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


def session(config: Config) -> requests.Session:
    """Build a configured requests session."""
    import requests

    if config.system_trust:
        _use_system_trust()

    s = requests.Session()
    s.headers["User-Agent"] = DEFAULT_UA
    proxies = config.proxy.as_requests_mapping()
    if proxies is not None:
        s.proxies.update(proxies)
        s.trust_env = False
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

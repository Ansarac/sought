"""The official Brave Search API: opt-in, keyed, plain HTTP.

This is not the HTTP-only path for `brave` that CLAUDE.md forbids. That rule
is about scraping Brave's HTML results page without a browser, which Brave
answers with 429 at the same moment Playwright succeeds — a bot wall, so an
HTTP-only `brave` would claim coverage it does not have. This engine sends the
user's own subscription key to Brave's documented JSON endpoint
(`api.search.brave.com/res/v1/web/search`). There is no bot wall to meet, and a
429 here is the API saying the plan's quota is spent: a real `rate_limited`,
with the quota headers in the detail.

Opt-in without a separate switch: a key (`brave_api.api_key`, or the
`BRAVE_API_KEY` environment variable when that is empty) is what turns it on.
Asked for without one, it records an `error` naming both, and is never skipped
silently. Never in the default order.

The key travels in one request header and nowhere else. Every detail this
module produces is passed through `_scrub` before it leaves, so neither an
attempt, the cooldown file, the result cache nor doctor can carry it.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Any

from sought.config import BRAVE_API_KEY_ENV
from sought.engines.base import EngineFailure, Result, ResultList
from sought.net import session

if TYPE_CHECKING:
    import requests

    from sought.config import Config

NAME = "brave_api"
ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

# The API's documented maximum for `count`. A larger request is a 422, so the
# limit is capped before asking and the `ok` attempt says it was.
MAX_COUNT = 20

NO_KEY_DETAIL = (
    f"no API key — brave_api is opt-in; set `api_key` under [brave_api] in the config file "
    f"or the {BRAVE_API_KEY_ENV} environment variable (key: https://api-dashboard.search.brave.com)"
)

_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_BAD_REQUEST = 400
_HTTP_SERVER_ERROR = 500

# Brave reports quotas per window as comma-separated lists, one entry per
# window, and `X-RateLimit-Policy` names each window's length (`50;w=1,
# 0;w=2592000`). Measured on a working key: the 30-day window read
# `limit 0, remaining 0` while every request succeeded — a 0 limit is "no
# fixed cap reported", not "spent", and must not be printed as if it were.
_LIMIT_HEADER = "X-RateLimit-Limit"
_REMAINING_HEADER = "X-RateLimit-Remaining"
_RESET_HEADER = "X-RateLimit-Reset"
_POLICY_HEADER = "X-RateLimit-Policy"
_WINDOW_RE = re.compile(r"\bw=(\d+)")
_SECONDS_PER = (("d", 86400), ("h", 3600), ("m", 60))

_TAG_RE = re.compile(r"<[^>]+>")


def search(query: str, *, config: Config, browser: object | None = None) -> list[Result]:  # noqa: ARG001
    """Ask the Brave Search API and return parsed results."""
    key, _source = config.brave_api.resolve()
    if not key:
        raise EngineFailure("error", NO_KEY_DETAIL)
    try:
        return _search(query, key=key, config=config)
    except EngineFailure as exc:
        raise EngineFailure(exc.status, _scrub(exc.detail, key)) from None
    except Exception as exc:  # noqa: BLE001 — the chain would print str(exc) unscrubbed
        # requests puts a header value it refuses (a key with a stray control
        # character, say) into its exception message.
        raise EngineFailure("error", _scrub(f"{exc.__class__.__name__}: {exc}", key)) from None


def _search(query: str, *, key: str, config: Config) -> ResultList:
    import requests

    count = min(config.limit, MAX_COUNT)
    try:
        resp = session(config).get(
            ENDPOINT,
            params={"q": query, "count": count},
            headers={"Accept": "application/json", "X-Subscription-Token": key},
            timeout=config.timeout_seconds,
        )
    except requests.Timeout as exc:
        raise EngineFailure("timeout", f"no response in {config.timeout_seconds}s") from exc
    except requests.RequestException as exc:
        raise EngineFailure("blocked", str(exc).strip().splitlines()[0]) from exc

    if resp.status_code != 200:  # noqa: PLR2004 — the one status that means an answer
        raise _classify_status(resp)

    try:
        payload = resp.json()
    except ValueError as exc:
        # HTTP 200 and not JSON: a captive portal or a proxy's interstitial
        # page, not Brave. Never `empty` — nothing was searched.
        ctype = resp.headers.get("Content-Type", "unknown content type")
        raise EngineFailure("error", f"HTTP 200 but the body is not JSON ({ctype})") from exc

    results = parse(payload)
    if not results:
        raise EngineFailure("empty", "the API returned no web results")

    notes = []
    if config.limit > MAX_COUNT:
        notes.append(f"capped at {MAX_COUNT} results by the API (limit {config.limit})")
    return ResultList(results[: config.limit], detail="; ".join(notes))


def parse(payload: Any) -> list[Result]:  # noqa: ANN401 — the decoded JSON body, whatever it holds
    """Extract results from a decoded API response.

    Split out from `search` so the parser can be tested against saved
    responses without touching the network.

    An empty list means the API answered and nothing matched: Brave omits the
    `web` section when it has no web results, so a well-formed search response
    without one is `empty`. A body that is not that shape at all — no `web`
    section and no search envelope either, or a `web` section that is not what
    the schema says — raises an `error`: sought does not understand the page,
    and must not report that as "nothing matched".
    """
    if not isinstance(payload, dict):
        raise EngineFailure("error", f"unexpected response: top level is {type(payload).__name__}, not an object")

    web = payload.get("web")
    if web is None:
        if payload.get("type") == "search" or isinstance(payload.get("query"), dict):
            return []
        raise EngineFailure("error", "unexpected response: neither a `web` section nor a search response")
    if not isinstance(web, dict):
        raise EngineFailure("error", "unexpected response: `web` is not an object")

    items = web.get("results", [])
    if not isinstance(items, list):
        raise EngineFailure("error", "unexpected response: `web.results` is not a list")

    results: list[Result] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        title = item.get("title")
        if not isinstance(url, str) or not url or not isinstance(title, str):
            continue
        snippet = item.get("description")
        results.append(
            Result(
                title=_text(title),
                url=url,
                snippet=_text(snippet) if isinstance(snippet, str) else "",
            )
        )
    if items and not results:
        raise EngineFailure("error", f"unexpected response: none of {len(items)} web results has a title and URL")
    return results


def _classify_status(resp: requests.Response) -> EngineFailure:
    """Turn a non-200 answer into the status it actually means.

    A 401 or 403 is only "the API key was rejected" when Brave says so. On a
    network with an authenticating firewall or proxy, measured on the
    development machine, a 401 arrives from the firewall before any request
    reaches Brave; calling that a rejected key would send the user off to
    rotate a key that works. Brave's own errors are JSON with an `error`
    object, so that is what decides it — and Brave's own error code decides
    it the other way too: a rejected key comes back as 422 (`_is_auth_error`).
    """
    status = resp.status_code
    brave_error = _brave_error(resp)

    if status == _HTTP_TOO_MANY_REQUESTS:
        detail = f"HTTP 429{_code_note(brave_error)}"
        quota = _quota(resp)
        if quota:
            detail += f" — {quota}"
        return EngineFailure("rate_limited", detail)

    if brave_error is not None and (status in {_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN} or _is_auth_error(brave_error)):
        return EngineFailure("error", f"API key rejected — HTTP {status}{_code_note(brave_error)}")
    if status in {_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN}:
        return EngineFailure("blocked", f"HTTP {status}, not from the Brave API (a proxy or firewall?)")

    if brave_error is not None and _HTTP_BAD_REQUEST <= status < _HTTP_SERVER_ERROR:
        # Brave refused the request itself (a 400 or 422): something sought
        # sent, not the route and not the quota.
        return EngineFailure("error", f"request rejected by the API — HTTP {status}{_code_note(brave_error)}")

    # Anything else is treated as marginalia and the vertical sources treat
    # it: a route or upstream failure.
    return EngineFailure("blocked", f"HTTP {status}{_code_note(brave_error)}")


def _brave_error(resp: requests.Response) -> dict[str, Any] | None:
    """Return Brave's `error` object when the body is one of its error responses."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"]
    return None


def _is_auth_error(error: dict[str, Any]) -> bool:
    """Brave's way of saying the key is wrong, whatever the status line says.

    Measured: an invalid key gets HTTP 422 — not 401 — with
    `{"code": "SUBSCRIPTION_TOKEN_INVALID", "meta": {"component":
    "authentication"}}`. Read by status alone, that is "request rejected", and
    the user would go looking for a bug in sought instead of at their key.
    """
    code = error.get("code")
    meta = error.get("meta")
    return (isinstance(code, str) and code.startswith("SUBSCRIPTION_")) or (
        isinstance(meta, dict) and meta.get("component") == "authentication"
    )


def _code_note(error: dict[str, Any] | None) -> str:
    if error is None:
        return ""
    code = error.get("code")
    return f" ({code})" if isinstance(code, str) and code else ""


def _quota(resp: requests.Response) -> str:
    """Describe each quota window the headers report, by its length, and which one is spent.

    Window lengths come from `X-RateLimit-Policy`; without it the windows are
    numbered rather than guessed. A window with a positive limit and nothing
    remaining is marked exhausted; one whose limit is 0 is reported as having
    no cap, never as spent (see the header comment above).
    """
    limits = _header_list(resp, _LIMIT_HEADER)
    remaining = _header_list(resp, _REMAINING_HEADER)
    resets = _header_list(resp, _RESET_HEADER)
    windows = [m.group(1) for m in map(_WINDOW_RE.search, _header_list(resp, _POLICY_HEADER)) if m]
    count = max(len(limits), len(remaining), len(resets))
    parts = []
    for i in range(count):
        name = f"{_duration(windows[i])} window" if i < len(windows) else f"window {i + 1}"
        limit = limits[i] if i < len(limits) else ""
        left = remaining[i] if i < len(remaining) else ""
        facts = []
        if limit == "0":
            facts.append("no cap reported (limit 0)")
        else:
            if limit:
                facts.append(f"limit {limit}")
            if left:
                facts.append(f"remaining {left}")
        if i < len(resets) and resets[i]:
            facts.append(f"resets in {_duration(resets[i])}")
        if limit not in {"", "0"} and left == "0":
            facts.append("exhausted")
        parts.append(f"{name}: {', '.join(facts)}")
    return "quota — " + "; ".join(parts) if parts else ""


def _header_list(resp: requests.Response, header: str) -> list[str]:
    value = resp.headers.get(header) or ""
    return [item.strip() for item in value.split(",")] if value.strip() else []


def _duration(seconds: str) -> str:
    """`1209600` → `14d`; anything that is not a whole number of seconds is passed through."""
    if not seconds.isdigit():
        return seconds
    n = int(seconds)
    for unit, size in _SECONDS_PER:
        if n >= size:
            return f"{n // size}{unit}"
    return f"{n}s"


def _scrub(detail: str, key: str) -> str:
    """Remove the key from a detail, in case anything upstream ever echoes it."""
    return detail.replace(key, "***") if key else detail


def _text(fragment: str) -> str:
    """Brave marks query terms with <strong> in titles and descriptions."""
    return " ".join(html.unescape(_TAG_RE.sub("", fragment)).split())

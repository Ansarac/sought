"""Keyless vertical sources: one well-known API each, all plain HTTP.

These are not part of the fallback chain. They answer a narrower question far
better than any general engine, and they are reached with `--source`.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Any

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from sought.engines.base import EngineFailure, Result
from sought.net import session

if TYPE_CHECKING:
    from collections.abc import Callable

    import requests

    from sought.config import Config

_TAG_RE = re.compile(r"<[^>]+>")
_HTTP_BAD_REQUEST = 400
_HTTP_FORBIDDEN = 403
_HTTP_TOO_MANY_REQUESTS = 429

# Enough of the body to find a reason code in, and not enough to hold a whole
# error page in memory.
_BODY_PEEK = 500


def search(source: str, query: str, *, config: Config) -> list[Result]:
    """Query one vertical source by name."""
    handler = _HANDLERS.get(source)
    if handler is None:
        msg = f"unknown source {source!r}"
        raise EngineFailure("error", msg)

    sess = session(config)
    results = _guard(lambda: handler(sess, query, config))[: config.limit]
    if not results:
        raise EngineFailure("empty", f"{source} matched nothing")
    return results


def _guard(fn: Callable[[], list[Result]]) -> list[Result]:
    """Run a handler, turning transport failures into classified EngineFailures."""
    import requests

    try:
        return fn()
    except requests.Timeout as exc:
        raise EngineFailure("timeout", "no response in time") from exc
    except requests.HTTPError as exc:
        raise _classify_http_error(exc) from exc
    except requests.RequestException as exc:
        raise EngineFailure("blocked", str(exc).strip().splitlines()[0]) from exc
    except (ValueError, KeyError, ElementTree.ParseError, DefusedXmlException) as exc:
        raise EngineFailure("error", f"unparseable response: {exc}") from exc


def _classify_http_error(exc: requests.HTTPError) -> EngineFailure:
    """Tell quota exhaustion apart from refusal, including where it does not say 429.

    `rate_limited` and `blocked` earn opposite advice — wait, versus stop
    asking this way — so guessing from the status line alone is not good
    enough. Two of these sources are known to spell quota exhaustion in their
    own way:

    * GitHub answers an over-quota unauthenticated search with 403, not 429,
      and puts the counter in `x-ratelimit-remaining`.
    * StackExchange answers a throttled request with 400 and says
      `throttle_violation` only in the body.

    Both used to be reported as `blocked`, which sent the user off to debug a
    network that was working.
    """
    resp = exc.response
    status = resp.status_code if resp is not None else 0

    if status == _HTTP_TOO_MANY_REQUESTS:
        return EngineFailure("rate_limited", "HTTP 429")

    if resp is not None:
        body = resp.text[:_BODY_PEEK].lower()
        if status == _HTTP_FORBIDDEN and (resp.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in body):
            return EngineFailure("rate_limited", "HTTP 403: API rate limit exceeded")
        if status == _HTTP_BAD_REQUEST and "throttle_violation" in body:
            return EngineFailure("rate_limited", "HTTP 400: throttle_violation")

    return EngineFailure("blocked", f"HTTP {status}")


def _json(sess: requests.Session, url: str, params: dict[str, Any], config: Config) -> Any:  # noqa: ANN401
    resp = sess.get(url, params=params, timeout=config.timeout_seconds)
    resp.raise_for_status()
    return resp.json()


def _wikipedia(sess: requests.Session, query: str, config: Config) -> list[Result]:
    data = _json(
        sess,
        "https://en.wikipedia.org/w/api.php",
        {"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": config.limit},
        config,
    )
    return [
        Result(
            title=item["title"],
            url=f"https://en.wikipedia.org/wiki/{item['title'].replace(' ', '_')}",
            snippet=_strip(item.get("snippet", "")),
        )
        for item in data["query"]["search"]
    ]


def _github(sess: requests.Session, query: str, config: Config) -> list[Result]:
    data = _json(
        sess,
        "https://api.github.com/search/repositories",
        {"q": query, "per_page": config.limit, "sort": "stars"},
        config,
    )
    return [
        Result(
            title=f"{item['full_name']} ★{item['stargazers_count']}",
            url=item["html_url"],
            snippet=item.get("description") or "",
        )
        for item in data.get("items", [])
    ]


def _hn(sess: requests.Session, query: str, config: Config) -> list[Result]:
    data = _json(
        sess,
        "https://hn.algolia.com/api/v1/search",
        {"query": query, "hitsPerPage": config.limit},
        config,
    )
    results = []
    for hit in data.get("hits", []):
        object_id = hit.get("objectID", "")
        discussion = f"https://news.ycombinator.com/item?id={object_id}"
        title = hit.get("title") or hit.get("story_title") or _strip(hit.get("comment_text", ""))[:80]
        results.append(
            Result(
                title=f"{title} ({hit.get('points', 0)} points, {hit.get('num_comments', 0)} comments)",
                url=hit.get("url") or discussion,
                snippet=f"discussion: {discussion}",
            )
        )
    return results


def _stackoverflow(sess: requests.Session, query: str, config: Config) -> list[Result]:
    data = _json(
        sess,
        "https://api.stackexchange.com/2.3/search/advanced",
        {
            "order": "desc",
            "sort": "relevance",
            "q": query,
            "site": "stackoverflow",
            "pagesize": config.limit,
        },
        config,
    )
    return [
        Result(
            title=html.unescape(item["title"]),
            url=item["link"],
            snippet=f"score {item.get('score', 0)}, {item.get('answer_count', 0)} answers"
            + (", accepted" if item.get("is_answered") else ", unanswered"),
        )
        for item in data.get("items", [])
    ]


def _npm(sess: requests.Session, query: str, config: Config) -> list[Result]:
    data = _json(
        sess,
        "https://registry.npmjs.org/-/v1/search",
        {"text": query, "size": config.limit},
        config,
    )
    results = []
    for obj in data.get("objects", []):
        pkg = obj["package"]
        results.append(
            Result(
                title=f"{pkg['name']}@{pkg.get('version', '?')}",
                url=pkg.get("links", {}).get("npm", f"https://www.npmjs.com/package/{pkg['name']}"),
                snippet=pkg.get("description") or "",
            )
        )
    return results


def _pypi(sess: requests.Session, query: str, config: Config) -> list[Result]:
    """Resolve an exact package name.

    PyPI removed its search API; the JSON endpoint only answers for an exact
    name. That is a real limitation, so say so rather than return nothing.
    """
    name = query.strip().split()[0] if query.strip() else query
    resp = sess.get(f"https://pypi.org/pypi/{name}/json", timeout=config.timeout_seconds)
    if resp.status_code == 404:  # noqa: PLR2004
        msg = f"no package named {name!r} (PyPI has no full-text search API; --source pypi resolves exact names only)"
        raise EngineFailure("empty", msg)
    resp.raise_for_status()
    info = resp.json()["info"]
    return [
        Result(
            title=f"{info['name']} {info['version']}",
            url=info.get("package_url") or f"https://pypi.org/project/{info['name']}/",
            snippet=info.get("summary") or "",
        )
    ]


def _news(sess: requests.Session, query: str, config: Config) -> list[Result]:
    resp = sess.get(
        "https://news.google.com/rss/search",
        params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
        timeout=config.timeout_seconds,
    )
    resp.raise_for_status()
    # defusedxml, not the stdlib parser: this is remote XML from the network.
    root = ElementTree.fromstring(resp.content)
    results = []
    for item in root.iterfind(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title and link:
            results.append(Result(title=title, url=link, snippet=pub))
    return results


_HANDLERS: dict[str, Callable[[requests.Session, str, Config], list[Result]]] = {
    "wikipedia": _wikipedia,
    "github": _github,
    "hn": _hn,
    "stackoverflow": _stackoverflow,
    "npm": _npm,
    "pypi": _pypi,
    "news": _news,
}


def _strip(fragment: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", fragment)).split())

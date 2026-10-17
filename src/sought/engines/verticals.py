"""Keyless vertical sources: one well-known API each, all plain HTTP.

These are not part of the fallback chain. They answer a narrower question far
better than any general engine, and they are reached with `--source`.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from sought.browser import classify_dead_page
from sought.engines.base import EngineFailure, Result, ResultList
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
    answer = _guard(lambda: handler(sess, query, config))
    # Slicing a ResultList returns a plain list, so the detail is carried over
    # by hand; otherwise a handler's note would never reach its `ok` attempt.
    detail = answer.detail if isinstance(answer, ResultList) else ""
    results = answer[: config.limit]
    if not results:
        raise EngineFailure("empty", f"{source} matched nothing")
    return ResultList(results, detail=detail)


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
    """Search Google News through its RSS feed, then decode each article link.

    The feed's `<link>` is a news.google.com redirect, not the article, and
    not one an agent can follow: it answers with a JavaScript page rather than
    an HTTP redirect, so the article can be neither read nor cited.
    `_decode_news_links` recovers the publisher's URL, best effort, and the
    `ok` detail says how many it could not.

    Google appends " - <Publisher>" to every title. It is stripped only when it
    is exactly the item's own `<source>` text, so a title that merely ends in a
    dash is never cut; the publisher moves to the snippet, with the date.
    Before, the snippet was the date alone.
    """
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
        publisher = (item.findtext("source") or "").strip()
        if publisher and title.endswith(f" - {publisher}"):
            title = title.removesuffix(f" - {publisher}").rstrip()
        if title and link:
            snippet = " · ".join(part for part in (publisher, pub) if part)
            results.append(Result(title=title, url=link, snippet=snippet))

    # Sliced before decoding: each decoded link costs two requests to
    # news.google.com, and one cut from the output afterwards would be two
    # requests spent on nothing.
    results = results[: config.limit]
    results, unresolved, stopped = _decode_news_links(sess, results)
    detail = ""
    if unresolved:
        detail = f"{unresolved} of {len(results)} article URLs left as news.google.com redirects"
        if stopped:
            detail += f" (decoding stopped: {stopped})"
    return ResultList(results, detail=detail)


# --- Google News link decoding ----------------------------------------------

_NEWS_ARTICLE = "https://news.google.com/rss/articles/"
_NEWS_BATCHEXECUTE = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
# Short, as for google's `/goto`: an undecoded link still works, and one slow
# article is not worth holding the whole search for.
_DECODE_TIMEOUT = 5
# The article page's two parameters for the decode call. Matched separately,
# so a reordering of the attributes does not break decoding.
_SIGNATURE_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_TIMESTAMP_RE = re.compile(r'data-n-a-ts="(\d+)"')
# Fallback for a batchexecute body whose framing changed but whose payload did
# not: the URL right after the `garturlres` tag, JSON-escaped inside the outer
# JSON as `garturlres\",\"<url>\"`.
_GARTURL_RE = re.compile(r'garturlres\\",\\"(https?://[^"\\]+)\\"')
_BATCHEXECUTE_PREFIX = ")]}'"
_RPC_ENVELOPE = ["wrb.fr", "Fbv4je"]


class _DecodingRefusedError(Exception):
    """news.google.com has started refusing. The message says how, for the `ok` detail."""


def _decode_news_links(sess: requests.Session, results: list[Result]) -> tuple[list[Result], int, str]:
    """Replace news.google.com article links with the publisher URLs they stand for.

    Returns the results, how many links stayed undecoded, and why decoding
    stopped early ("" if it did not).

    The `/goto` approach of `sought.engines.google` does not carry over.
    Measured through a proxy: a plain GET of an article link answers 302 to
    itself and then a JavaScript page, with the destination in no `Location`.
    What works is the call Google News' own page makes: the article page
    carries a signature and a timestamp (`data-n-a-sg`, `data-n-a-ts`), and
    posting them with the article id to the `batchexecute` RPC (`Fbv4je`,
    `garturlreq`) returns the URL.

    That RPC is undocumented and internal, costs two requests to
    news.google.com per article, and may break any day. When it does, every
    link simply stays a news.google.com redirect — still a working link, just
    a worse one — and the `ok` detail counts them, so the breakage shows.

    Best effort per article: one that fails keeps its link. A 429, a
    `/sorry/` redirect, a recognised bot wall or a timeout stops the whole
    pass instead: asking nine more times after a refusal is the load that
    turns a refusal into a ban, and waiting out the timeout once per article
    would turn one silent endpoint into a search that takes a minute.
    """
    pending = [r.url for r in results if r.url.startswith(_NEWS_ARTICLE)]
    if not pending:
        return results, 0, ""

    import requests

    resolved: dict[str, str] = {}
    stopped = ""
    for url in pending:
        try:
            target = _decode_news_link(sess, url)
        except _DecodingRefusedError as exc:
            stopped = str(exc)
            break
        except requests.Timeout:
            stopped = f"no answer from news.google.com within {_DECODE_TIMEOUT}s"
            break
        except requests.RequestException:
            continue
        if target is not None:
            resolved[url] = target
    decoded = [replace(r, url=resolved.get(r.url, r.url)) for r in results]
    return decoded, len(pending) - len(resolved), stopped


def _decode_news_link(sess: requests.Session, url: str) -> str | None:
    """Decode one article link, or return None to keep it as it is."""
    article_id = urlsplit(url).path.rsplit("/", 1)[-1]
    if not article_id:
        return None

    page = sess.get(_NEWS_ARTICLE + article_id, timeout=_DECODE_TIMEOUT)
    if not _readable(page):
        return None
    signature = _SIGNATURE_RE.search(page.text)
    timestamp = _TIMESTAMP_RE.search(page.text)
    if signature is None or timestamp is None:
        _check_wall(page.text)
        return None

    resp = sess.post(
        _NEWS_BATCHEXECUTE,
        data={"f.req": garturl_request(article_id, int(timestamp.group(1)), signature.group(1))},
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        timeout=_DECODE_TIMEOUT,
    )
    if not _readable(resp):
        return None
    target = parse_batchexecute(resp.text)
    if target is None:
        _check_wall(resp.text)
    return target


def _readable(resp: requests.Response) -> bool:
    """Whether a response is worth parsing. A refusal is not a `False`: it stops the pass."""
    if resp.status_code == _HTTP_TOO_MANY_REQUESTS:
        msg = "HTTP 429"
        raise _DecodingRefusedError(msg)
    # requests follows redirects, so Google's block page shows in the final URL.
    if "/sorry/" in str(getattr(resp, "url", "") or ""):
        msg = "google /sorry wall"
        raise _DecodingRefusedError(msg)
    return resp.status_code < _HTTP_BAD_REQUEST


def _check_wall(body: str) -> None:
    """Stop the pass if a body that lacked what was expected is a known bot wall.

    Only asked of a body that already failed to parse, as everywhere
    `classify_dead_page` is used. The real article page is half a megabyte of
    script; searching all of that for "captcha" unprompted would be asking for
    a false alarm.
    """
    failure = classify_dead_page(body, empty_detail="")
    if failure.status == "rate_limited":
        raise _DecodingRefusedError(failure.detail)


def garturl_request(article_id: str, timestamp: int, signature: str) -> str:
    """Build the `f.req` form value for the `Fbv4je` / `garturlreq` RPC, as Google News' own page sends it."""
    context = [
        ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
        "X",
        "X",
        1,
        [1, 1, 1],
        1,
        1,
        None,
        0,
        0,
        None,
        0,
    ]
    inner = ["garturlreq", context, article_id, timestamp, signature]
    return json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])


def parse_batchexecute(body: str) -> str | None:
    """Pull the decoded article URL out of a `batchexecute` response, or None.

    The body is `)]}'` and then JSON arrays, one per line, each optionally
    preceded by a line holding its length. The answer is the envelope
    `["wrb.fr", "Fbv4je", "<json>", ...]`, whose third element is JSON again:
    `["garturlres", "<url>", ...]`. That structure is parsed first; a regex
    over the raw text is the fallback, for a change in the framing that left
    the payload alone. Either way only an absolute http(s) URL off
    news.google.com is accepted: anything else is not the article, and
    reporting it as one would be inventing a result.
    """
    for line in body.removeprefix(_BATCHEXECUTE_PREFIX).splitlines():
        candidate = _garturl_from_line(line.strip())
        if candidate is not None and _is_article_url(candidate):
            return candidate
    match = _GARTURL_RE.search(body)
    if match is not None and _is_article_url(match.group(1)):
        return match.group(1)
    return None


def _garturl_from_line(line: str) -> str | None:
    if not line.startswith("["):
        return None
    try:
        envelopes = json.loads(line)
    except ValueError:
        return None
    for envelope in envelopes if isinstance(envelopes, list) else []:
        if not (isinstance(envelope, list) and envelope[:2] == _RPC_ENVELOPE and len(envelope) > len(_RPC_ENVELOPE)):
            continue
        if not isinstance(envelope[2], str):
            continue
        try:
            payload = json.loads(envelope[2])
        except ValueError:
            continue
        if isinstance(payload, list) and payload[:1] == ["garturlres"] and len(payload) > 1:
            url = payload[1]
            if isinstance(url, str):
                return url
    return None


def _is_article_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and bool(parts.hostname) and parts.hostname != "news.google.com"


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

"""Marginalia, an independent index reachable over plain HTTP.

Last rung of the chain: a different crawl, a different operator, and the only
general engine here that needs no browser. Its index favours small and
non-commercial sites, so it is a weak substitute for Brave on product queries
and a good one on technical writing.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from scout.engines.base import EngineFailure, Result
from scout.net import session

if TYPE_CHECKING:
    from scout.config import Config

NAME = "marginalia"
ENDPOINT = "https://old-search.marginalia.nu/search"

_CARD_RE = re.compile(r'<section[^>]*class="card search-result"[^>]*>(.*?)</section>', re.DOTALL)
_URL_RE = re.compile(r'<div class="url"><a[^>]+href="([^"]+)"')
_TITLE_RE = re.compile(r'<a[^>]+class="title"[^>]*>(.*?)</a>', re.DOTALL)
_DESC_RE = re.compile(r'<p[^>]*class="description"[^>]*>(.*?)</p>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_BAD_REQUEST = 400


def search(query: str, *, config: Config, browser: object | None = None) -> list[Result]:  # noqa: ARG001
    """Query Marginalia and return parsed results."""
    import requests

    sess = session(config)
    try:
        resp = sess.get(ENDPOINT, params={"query": query}, timeout=config.timeout_seconds)
    except requests.Timeout as exc:
        raise EngineFailure("timeout", f"no response in {config.timeout_seconds}s") from exc
    except requests.RequestException as exc:
        raise EngineFailure("blocked", str(exc).strip().splitlines()[0]) from exc

    if resp.status_code == _HTTP_TOO_MANY_REQUESTS:
        raise EngineFailure("rate_limited", "HTTP 429")
    if resp.status_code >= _HTTP_BAD_REQUEST:
        raise EngineFailure("blocked", f"HTTP {resp.status_code}")

    results = parse(resp.text)[: config.limit]
    if not results:
        raise EngineFailure("empty", "no search-result cards in the response")
    return results


def parse(body: str) -> list[Result]:
    """Extract results from a Marginalia results page.

    Split out from `search` so the parser can be tested against a saved page
    without touching the network.
    """
    results: list[Result] = []
    for card in _CARD_RE.findall(body):
        url_m = _URL_RE.search(card)
        title_m = _TITLE_RE.search(card)
        if not url_m or not title_m:
            continue
        desc_m = _DESC_RE.search(card)
        results.append(
            Result(
                title=_text(title_m.group(1)),
                url=html.unescape(url_m.group(1)),
                snippet=_text(desc_m.group(1)) if desc_m else "",
            )
        )
    return results


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", fragment)).split())

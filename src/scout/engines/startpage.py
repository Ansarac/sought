"""Startpage, driven through Chromium — a proxy onto the Google index.

Google itself is unreachable from many corporate egress IPs (the whole NAT
address gets banned), while Startpage fronts the same index from its own
infrastructure.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

from scout.browser import goto
from scout.engines.base import EngineFailure, Result

if TYPE_CHECKING:
    from scout.browser import Browser
    from scout.config import Config

NAME = "startpage"
_RESULTS_CONTAINER = ".w-gl"
_ITEM = ".w-gl .result, .result.w-gl__result"

_GOOD_TITLE = re.compile(r"Search Results", re.IGNORECASE)

# Two traps in this markup, both of which silently produce garbage rather than
# an error:
#
#   1. Startpage inlines emotion CSS as <style> tags *inside* each result node,
#      so a naive textContent read returns a wall of CSS.
#   2. `.wgl-site-title` holds the bare domain ("claude.com") and appears
#      *before* the real `h2.wgl-title` in DOM order, so a combined
#      querySelector picks the domain every time. Ask for the heading first,
#      as its own query, and only then fall back.
_EXTRACT = """
(sel) => Array.from(document.querySelectorAll(sel)).map((e) => {
  const node = e.cloneNode(true);
  node.querySelectorAll('style,script,noscript').forEach((x) => x.remove());
  const link = node.querySelector('a.result-title[href], a.result-link[href]');
  const a = link || node.querySelector('a[href]');
  const t = node.querySelector('h2, h3')
         || link
         || node.querySelector('.wgl-site-title');
  const d = node.querySelector('p.description, .description');
  return {
    title: (t?.textContent || '').trim(),
    url: a?.href || '',
    snippet: (d?.textContent || '').trim(),
  };
}).filter((r) => r.url && r.title)
"""


def search(query: str, *, config: Config, browser: Browser | None) -> list[Result]:
    """Query Startpage and return parsed results."""
    if browser is None:
        msg = "startpage requires a browser session"
        raise EngineFailure("error", msg)

    url = f"https://www.startpage.com/sp/search?query={quote_plus(query)}"
    with browser.page() as page:
        goto(page, url, wait_for=_RESULTS_CONTAINER)

        # A challenge page can still render .w-gl-ish markup, so confirm the
        # document title before trusting anything on the page.
        doc_title = page.title() or ""
        if not _GOOD_TITLE.search(doc_title):
            raise EngineFailure("blocked", f"unexpected page title {doc_title!r}")

        rows = page.evaluate(_EXTRACT, _ITEM)

    results = [Result(title=r["title"], url=r["url"], snippet=r["snippet"]) for r in rows][: config.limit]
    if not results:
        raise EngineFailure("empty", "results container rendered but held no results")
    return results

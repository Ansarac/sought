"""Brave Search, driven through Chromium.

Brave answers plain HTTP clients with 429 and a real browser with results, so
this engine has no HTTP fallback by design.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote_plus

from scout.browser import goto
from scout.engines.base import EngineFailure, Result

if TYPE_CHECKING:
    from scout.browser import Browser
    from scout.config import Config

NAME = "brave"
_RESULTS_CONTAINER = "#results"
_ITEM = '#results .snippet[data-type="web"]'

# Runs inside the page. Each result node carries the title, href and snippet in
# one of two class schemes depending on which layout Brave serves.
_EXTRACT = """
(sel) => Array.from(document.querySelectorAll(sel)).map((e) => {
  const a = e.querySelector('a[href]');
  const t = e.querySelector('div.title, .snippet-title');
  const d = e.querySelector('div.content, .snippet-description');
  return {
    title: (t?.textContent || a?.textContent || '').trim(),
    url: a?.href || '',
    snippet: (d?.textContent || '').trim(),
  };
}).filter((r) => r.url && r.title)
"""


def search(query: str, *, config: Config, browser: Browser | None) -> list[Result]:
    """Query Brave and return parsed results."""
    if browser is None:
        msg = "brave requires a browser session"
        raise EngineFailure("error", msg)

    url = f"https://search.brave.com/search?q={quote_plus(query)}"
    with browser.page() as page:
        goto(page, url, wait_for=_RESULTS_CONTAINER)
        rows = page.evaluate(_EXTRACT, _ITEM)

    results = [Result(title=r["title"], url=r["url"], snippet=r["snippet"]) for r in rows][: config.limit]
    if not results:
        raise EngineFailure("empty", "results container rendered but held no web results")
    return results

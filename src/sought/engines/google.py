"""Google, driven through its own Chromium with the automation flag hidden. Opt-in, off by default.

Google refuses an automated browser per request, on `navigator.webdriver`
(docs/findings/2026-10-egress-google-windows.md, §3-4), not per IP. The only
way past that is to hide the flag, which is fingerprint evasion against
Google's policy on automated queries. The repository owner decided to offer
it anyway, under four conditions this module exists to keep:

* **Off unless asked for.** `[google] enabled = true` *and* "google" named in
  `engines.order` or `--source google`. Asked for while disabled, it records an
  `error` attempt saying which key to set, rather than vanishing.
* **Bounded.** A local hourly budget (`[google] max_per_hour`) is checked before
  Chromium is even launched. Heavy automated use from one shared egress IP is
  how Google comes to block that IP for everyone behind it.
* **Isolated.** It launches its own Chromium (`browser_session(...,
  hide_automation=True)`); nothing about how `brave` and `startpage` are
  launched changes when it is on.
* **Disclosed.** Its `ok` attempt says the flag was hidden, so the audit trail
  never presents these results as if they had been obtained like the others.

Result links are `https://www.google.com/goto?url=<token>`. Measured on the
captured pages: Google now serves organic links as an encrypted redirect
token, and the destination URL appears nowhere in the page, scripts included.
They are passed on as they are — guessing the destination from the breadcrumb
would be inventing a URL — and the `ok` detail says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit

from sought import state
from sought.browser import browser_session, classify_dead_page, extract, goto
from sought.engines.base import EngineFailure, Result, ResultList

if TYPE_CHECKING:
    from sought.config import Config

NAME = "google"
BASE = "https://www.google.com/"

ENABLE_HINT = "set `enabled = true` under [google] in the config file"
DISABLED_DETAIL = f"disabled — google is opt-in; {ENABLE_HINT} (see `sought config --init` for the trade-off)"

# Disclosed on every `ok`: these results were obtained with a spoofed signal.
EVASION_DETAIL = "automation flag hidden (google.enabled)"
GOTO_NOTE = "result URLs are google.com/goto redirects; the destination is not in the page"

# Organic results are `a h3` inside #search (measured on three live pages).
# Waiting on the anchor rather than on containers matters: Playwright judges
# visibility on the first match in document order, and an empty, hidden
# `#topstuff` ahead of a results page made a combined selector time out on a
# page that had nine results. A page without results therefore costs the full
# selector timeout before it is classified; that is the price of not guessing
# at markup that has not been seen.
_RESULTS = "#search a h3"

# Google's own wording for a query with no matches. The only page that may be
# reported as `empty`; see `_dead_page`.
NO_MATCH = "did not match any documents"

# The page as the browser holds it, minus everything that is not markup.
# Parsed in Python (`parse`), not in the page: that keeps the parser testable
# against a saved page, offline, the way `marginalia.parse` is — the `_EXTRACT`
# scripts of the other browser engines can only be tested with a live browser.
# Run through `extract`, so a wedged page is bounded like everywhere else.
_SERIALIZE = """
() => {
  const root = document.documentElement.cloneNode(true);
  root.querySelectorAll('script, style, noscript, svg, link, meta, iframe, template, img')
      .forEach((node) => node.remove());
  return root.outerHTML;
}
"""


def search(query: str, *, config: Config, browser: object | None = None) -> list[Result]:  # noqa: ARG001
    """Query Google in a dedicated Chromium with the automation flag hidden.

    The shared `browser` is deliberately ignored. The chain checks `enabled`,
    cooldowns and the budget before calling this (see `sought.search`), so a
    skip never costs a Chromium launch; `enabled` is checked again here so no
    caller can reach Google with it off.
    """
    if not config.google.enabled:
        raise EngineFailure("error", DISABLED_DETAIL)

    url = f"{BASE}search?q={quote_plus(query)}&hl=en&num={config.limit}"
    with browser_session(config, hide_automation=True) as own, own.page() as page:
        # Spent before the navigation, not after it succeeds: the budget bounds
        # what Google sees, and Google sees the attempt either way.
        state.spend_budget(NAME)
        try:
            goto(page, url, wait_for=_RESULTS, on_arrival=_check_url)
        except EngineFailure as exc:
            if exc.status != "empty":
                raise
            # The results anchor never appeared. That is only `empty` if
            # Google said so; see `_dead_page`.
            _check_url(page)
            raise _dead_page(extract(page, _SERIALIZE, None, timeout_ms=own.timeout_ms)) from exc
        _check_url(page)
        html = extract(page, _SERIALIZE, None, timeout_ms=own.timeout_ms)

    results = parse(html)[: config.limit]
    if not results:
        raise _dead_page(html)
    redirects = any(r.url.startswith(f"{BASE}goto?") for r in results)
    return ResultList(results, detail=f"{EVASION_DETAIL}; {GOTO_NOTE}" if redirects else EVASION_DETAIL)


def _check_url(page: object) -> None:
    """Classify the walls Google signals by where it sends the browser.

    `/sorry/` is the automated-query block, answered 302 then 429; it is
    recognised by URL first because it is Google's own name for it, whatever
    status it arrives with. A consent interstitial is not a refusal of sought
    in particular, but nothing can be searched past it without clicking
    "accept" on the user's behalf, which sought will not do.
    """
    url = str(getattr(page, "url", "") or "")
    if "/sorry/" in url:
        raise EngineFailure("rate_limited", "google /sorry wall")
    if "consent.google." in url:
        raise EngineFailure("blocked", "consent interstitial (consent.google.com); sought does not click through it")


def _dead_page(html: str) -> EngineFailure:
    """Classify a Google page that yielded no results.

    Bot walls first, as for every engine. After that, unlike the others, a
    page that is not a recognised wall is *not* `empty` by default: Google
    says "did not match any documents" when a query has no answer, so a page
    without that sentence is one sought did not understand — markup changed,
    a JavaScript-only shell, an interstitial nobody has seen yet. Calling that
    `empty` would tell the caller to reword a query the web was never asked.
    """
    text = page_text(html)
    failure = classify_dead_page(text, empty_detail=f"Google: {NO_MATCH!r}")
    if failure.status != "empty" or NO_MATCH in text.lower():
        return failure
    return EngineFailure("error", "page not recognised: no results, no known wall, no 'did not match' notice")


def unwrap(href: str) -> str | None:
    """Turn a result link into the URL to report, or None to drop it.

    * `/url?q=<target>` is Google's older redirect form; the target is in the
      query string and is returned directly.
    * `/goto?url=<token>` is the current form; the token is opaque, so the
      link is returned absolute, with nothing but the token.
    * Any other Google-internal link (`/search?...` refinements, `#`) is
      navigation, not a result.
    * Only http(s) survives: `javascript:` and friends are not results.
    """
    absolute = urljoin(BASE, href)
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"}:
        return None
    if parts.netloc.endswith("google.com") and parts.netloc.startswith("www."):
        params = parse_qs(parts.query)
        if parts.path == "/url":
            target = (params.get("q") or params.get("url") or [""])[0]
            return target if urlsplit(target).scheme in {"http", "https"} else None
        if parts.path == "/goto":
            token = (params.get("url") or [""])[0]
            return f"{BASE}goto?url={quote_plus(token)}" if token else None
        return None
    return absolute


# --- parsing ----------------------------------------------------------------

# Containers that hold ads ("Sponsored") rather than results.
_AD_IDS = frozenset({"tads", "tadsb", "bottomads", "tvcap"})
# "People also ask" and similar expanders: questions, not results.
_EXCLUDED_CLASSES = frozenset({"related-question-pair", "cUnQKe"})
_EXCLUDED_ATTRS = frozenset({"data-text-ad", "data-initq"})
# One organic result's block, newest layout first.
_BLOCK_CLASSES = ("MjjYud", "tF2Cxc", "g")
# The snippet inside a block, in the order Google has used them.
_SNIPPET_CLASSES = ("VwiC3b",)

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"},
)


@dataclass(eq=False)
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None = None
    children: list[_Node | str] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def ancestors(self) -> list[_Node]:
        out = []
        node = self.parent
        while node is not None:
            out.append(node)
            node = node.parent
        return out

    def walk(self) -> list[_Node]:
        out: list[_Node] = []
        stack: list[_Node] = [self]
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(reversed([c for c in node.children if isinstance(c, _Node)]))
        return out

    def text(self, *, sep: str = "", skip: frozenset[str] = frozenset()) -> str:
        """Concatenated text, whitespace collapsed.

        `sep=""` for a title or a snippet, where adjacent text nodes are one
        run of inline text (`tokio` `::select!` is "tokio::select!", not
        "tokio ::select!"); `sep=" "` for a whole page, so that separate
        blocks do not run together into words that were never written.
        """
        parts: list[str] = []
        stack: list[_Node | str] = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item.tag not in skip:
                stack.extend(reversed(item.children))
        return " ".join(sep.join(parts).split())


class _TreeBuilder(HTMLParser):
    """Just enough of a DOM to ask `closest()`-style questions of a page.

    Fed the browser's own serialization (`outerHTML`), which closes every
    non-void element, so mismatched tags only need tolerating, not repairing.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", {})
        self._open = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k: v or "" for k, v in attrs}, parent=self._open)
        self._open.children.append(node)
        if tag not in _VOID:
            self._open = node

    def handle_endtag(self, tag: str) -> None:
        node: _Node | None = self._open
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self._open = node.parent

    def handle_data(self, data: str) -> None:
        self._open.children.append(data)


def _tree(html: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def page_text(html: str) -> str:
    """Return the visible text of a serialized page, for bot-wall and no-match detection."""
    return _tree(html).text(sep=" ")


def parse(html: str) -> list[Result]:
    """Extract organic results from a Google results page.

    Takes the serialization `_SERIALIZE` produces, which is also what the
    fixtures hold, so the tests exercise exactly what runs. Results are the
    links with a heading inside `#search`; the AI overview sits outside it,
    ads live in their own containers, and "People also ask" is excluded by
    name.
    """
    root = _tree(html)
    scope = next((n for n in root.walk() if n.attrs.get("id") == "search"), None)
    if scope is None:
        return []

    results: list[Result] = []
    seen: set[str] = set()
    for node in scope.walk():
        if node.tag != "a" or "href" not in node.attrs or _excluded(node):
            continue
        heading = next((n for n in node.walk() if n.tag == "h3"), None)
        if heading is None:
            continue
        url = unwrap(node.attrs["href"])
        title = heading.text()
        if url is None or not title or url in seen:
            continue
        seen.add(url)
        results.append(Result(title=title, url=url, snippet=_snippet(node)))
    return results


def _excluded(anchor: _Node) -> bool:
    for node in anchor.ancestors():
        if node.attrs.get("id") in _AD_IDS or node.classes & _EXCLUDED_CLASSES:
            return True
        if any(attr in node.attrs for attr in _EXCLUDED_ATTRS):
            return True
    return False


def _snippet(anchor: _Node) -> str:
    """Find the description under a result, or "" when the layout has none we recognise.

    Found from the result's block, never from the anchor: the heading link
    and the description are siblings. Links inside the snippet ("Read more")
    are not part of it.
    """
    block = next((n for n in anchor.ancestors() if n.classes & set(_BLOCK_CLASSES)), None)
    if block is None:
        return ""
    for node in block.walk():
        if "data-sncf" in node.attrs or node.classes & set(_SNIPPET_CLASSES):
            text = node.text(skip=frozenset({"a"}))
            if text:
                return text
    return ""

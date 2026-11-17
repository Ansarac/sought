"""`sought fetch`: read one known URL and return its readable content as markdown.

Search finds URLs; this reads one. The discipline is the same: the answer is
either content or a classified reason there is none — never an empty string
that looks like a page with nothing on it.

Plain HTTP only, through `sought.net.session` (OS trust store, sought's proxy
rules), with sought's own User-Agent rather than a browser's. A page that only
renders under JavaScript is reported as such (`error`, "page needs
JavaScript"), not worked around.

`extract` is pure — HTML in, markdown out, or an `EngineFailure` — so it can be
tested against saved pages without the network.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from sought import __version__
from sought.browser import classify_dead_page
from sought.engines.base import EngineFailure
from sought.errors import UsageError
from sought.net import session
from sought.output import fetch_envelope

if TYPE_CHECKING:
    from collections.abc import Iterator

    import requests
    from lxml.html import HtmlElement

    from sought.config import Config

# sought's own name, not a browser's. `net.session` sends a desktop Chrome
# string because several keyless search APIs reject anything else; a page
# fetch is a different act, and pretending to be a browser while reading
# someone's page is exactly the kind of quiet disguise this tool does not do.
USER_AGENT = f"sought/{__version__} (+https://github.com/Ansarac/sought)"
_ACCEPT = "text/html,application/xhtml+xml;q=0.9,text/markdown;q=0.9,text/plain;q=0.8,application/json;q=0.7,*/*;q=0.1"

# Read at most this much. A larger body is refused rather than cut: half an
# HTML document extracts to something that looks complete and is not.
MAX_BODY_BYTES = 10 * 1024 * 1024
_CHUNK = 64 * 1024

# Fewer readable words than this is "no readable content". Low on purpose:
# example.com has about twenty, and it is a real page.
MIN_WORDS = 10
# A page this thin that says it needs JavaScript is a shell around an app, and
# whatever few words it has (a header, "Loading…") are not the page.
JS_SUSPECT_WORDS = 50
# Readability keeping fewer words than this, where a content container holds
# at least twice as many, missed the page: measured on the docs.python.org
# index, readability kept the 22-word intro and dropped every section list.
THIN_EXTRACT_WORDS = 50
# An extract under this share of the page's words gets the ratio in `detail`.
# Not a failure — PyPI's project page is 8% README and 92% sidebar, and
# readability picks the README correctly — but the reader should know.
PARTIAL_SHARE = 0.1
# An extract under this share of the page did not pick the page, it picked a
# fragment: measured on RFC 9110, readability scored the comma-heavy
# acknowledgements highest and kept 346 of 67,573 words (0.5%). The content
# containers are tried then, `body` included — RFC pages have no `main`. PyPI's
# README at 8% is a correct pick, and well above this.
LOST_SHARE = 0.02
# A container standing in for such an extract must be mostly prose: under this
# share of its words inside links. RFC 9110's body is 11% links; a sidebar or a
# mega-menu is mostly links, and swapping to it would trade a fragment for chrome.
MAX_LINK_SHARE = 0.5
# Bot-wall markers are only checked on a page this thin. `classify_dead_page`
# searches for words like "captcha" and "access denied", which an article may
# well contain; a wall is a short page, so a long one is never second-guessed.
WALL_CHECK_WORDS = 150

_JS_MARKERS = (
    "enable javascript",
    "javascript is required",
    "javascript is disabled",
    "requires javascript",
    "javascript to run this app",
    "turn on javascript",
    "javascript must be enabled",
)

_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_TEXT_TYPES = frozenset({"application/json", "application/xml", "application/x-ndjson", "application/javascript"})

# Where content.js-style fallbacks look when readability finds too little, in
# order. `body` last: a link list such as the Hacker News front page has no
# container and nothing readability scores, and the method says what was used.
_FALLBACK_XPATHS = (
    ("main", "//main"),
    ("article", "//article"),
    ("[role=main]", "//*[@role='main']"),
    ("#content", "//*[@id='content']"),
    ("body", "//body"),
)
_INVISIBLE = ("script", "style", "noscript", "template", "svg", "head")

_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_BAD_REQUEST = 400


@dataclass(frozen=True)
class Extracted:
    """What came out of a page: its title, its content as markdown, and how it was found."""

    title: str
    content: str
    method: str  # "readability", "fallback:<selector>", or "verbatim"
    detail: str = ""


def check_url(url: str) -> str:
    """Accept an absolute http(s) URL, or raise UsageError saying what is wrong with it."""
    parts = urlsplit(url.strip())
    if parts.scheme in {"http", "https"} and parts.netloc:
        return url.strip()
    if not parts.scheme and not parts.netloc and parts.path:
        raise UsageError(f"not an absolute URL: {url!r} — include the scheme, e.g. https://{url.strip()}")
    raise UsageError(f"only http:// and https:// URLs can be fetched (got {url!r})")


def fetch(url: str, *, config: Config, max_chars: int) -> dict[str, Any]:
    """Fetch one URL and return the fetch envelope. Failures are in the envelope, not raised."""
    url = check_url(url)
    meta: dict[str, Any] = {"final_url": None, "http_status": None, "content_type": None}
    try:
        extracted = _fetch(url, config=config, meta=meta)
    except EngineFailure as exc:
        return fetch_envelope(url=url, status=exc.status, detail=exc.detail, max_chars=max_chars, **meta)

    content, total, truncated = truncate(extracted.content, max_chars)
    return fetch_envelope(
        url=url,
        status="ok",
        detail=extracted.detail,
        method=extracted.method,
        title=extracted.title,
        content=content,
        total_chars=total,
        truncated=truncated,
        max_chars=max_chars,
        **meta,
    )


def _fetch(url: str, *, config: Config, meta: dict[str, Any]) -> Extracted:
    """Do the request, classify what came back, and extract it. Fills `meta` as facts arrive."""
    import requests

    sess = session(config)
    sess.headers["User-Agent"] = USER_AGENT
    sess.headers["Accept"] = _ACCEPT
    try:
        resp = sess.get(url, timeout=config.timeout_seconds, stream=True)
        try:
            meta["final_url"] = getattr(resp, "url", None) or url
            meta["http_status"] = resp.status_code
            raw_type = resp.headers.get("Content-Type", "")
            meta["content_type"] = raw_type or None
            _check_status(resp)
            kind = _kind(raw_type)
            body = _read(resp)
        finally:
            resp.close()
    except requests.Timeout as exc:
        raise EngineFailure("timeout", f"no response in {config.timeout_seconds}s") from exc
    except requests.RequestException as exc:
        first_line = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        raise EngineFailure("blocked", first_line) from exc

    if kind is None:
        kind = _sniff(body)
    if kind == "html":
        return extract(_decode(body, raw_type, html=True), meta["final_url"])
    text = _decode(body, raw_type, html=False)
    if not text.strip():
        raise EngineFailure("error", f"empty response body ({raw_type or 'no Content-Type'})")
    return Extracted(title="", content=text, method="verbatim")


def _check_status(resp: requests.Response) -> None:
    """Turn an HTTP error status into a classified failure; let anything below 400 through."""
    code = resp.status_code
    if code == _HTTP_TOO_MANY_REQUESTS:
        raise EngineFailure("rate_limited", "HTTP 429")
    if code >= _HTTP_BAD_REQUEST:
        # Cloudflare marks its own interstitial with this header. The body is
        # "Just a moment... Enable JavaScript and cookies to continue": a bot
        # wall, which is a status — sought does not run it in a browser.
        if resp.headers.get("cf-mitigated", "").lower() == "challenge":
            raise EngineFailure("rate_limited", f"bot wall: Cloudflare challenge (HTTP {code})")
        raise EngineFailure("blocked", f"HTTP {code}")


def _mime(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _kind(content_type: str) -> str | None:
    """Say how to read a body from its Content-Type: "html", "text", None to sniff — or refuse it."""
    mime = _mime(content_type)
    if not mime:
        return None
    if mime in _HTML_TYPES:
        return "html"
    if mime.startswith("text/") or mime in _TEXT_TYPES or mime.endswith(("+json", "+xml")):
        return "text"
    raise EngineFailure("error", f"unsupported content type {mime!r} — fetch reads HTML and text only")


def _sniff(body: bytes) -> str:
    """Classify a body that arrived without a Content-Type."""
    head = body[:1024].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")) or b"<body" in head:
        return "html"
    if b"\x00" in body[:8192]:
        raise EngineFailure("error", "no Content-Type, and the body is binary — fetch reads HTML and text only")
    return "text"


def _read(resp: requests.Response) -> bytes:
    """Read the body, refusing one larger than MAX_BODY_BYTES instead of cutting it."""
    limit_mb = MAX_BODY_BYTES // (1024 * 1024)
    declared = resp.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise EngineFailure("error", f"response is {int(declared):,} bytes, over the {limit_mb} MB fetch limit")
    chunks: list[bytes] = []
    size = 0
    for chunk in resp.iter_content(_CHUNK):
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise EngineFailure("error", f"response exceeds the {limit_mb} MB fetch limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode(body: bytes, content_type: str, *, html: bool) -> str:
    """Decode with the declared charset, else the page's own declaration, else UTF-8."""
    match = re.search(r"charset=[\"']?([\w.:-]+)", content_type, re.IGNORECASE)
    if match:
        try:
            return body.decode(match.group(1), errors="replace")
        except LookupError:
            pass  # an unknown charset name; fall through to sniffing
    if html:
        from readability.encoding import get_encoding

        encoding = get_encoding(body) or "utf-8"
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


def extract(html: str, url: str) -> Extracted:
    """Extract a page's readable content as markdown, or raise EngineFailure saying why there is none.

    Readability first, then the usual content containers (`main`, `article`,
    `[role=main]`, `#content`). A page that yields too little is never `ok`
    with an empty string: it is a bot wall (`rate_limited`), a JavaScript
    shell, or an extraction failure (`error`) — and never `empty`, which would
    claim the page said "nothing here".
    """
    import lxml.html
    from lxml.etree import ParserError

    # lxml refuses a str that still carries an XML encoding declaration, and
    # XHTML pages often start with one. The text is already decoded.
    html = re.sub(r"^\s*<\?xml[^>]*\?>", "", html)
    if not html.strip():
        raise EngineFailure("error", "empty response body")
    try:
        doc = lxml.html.document_fromstring(html)
    except (ParserError, ValueError) as exc:
        raise EngineFailure("error", f"HTML could not be parsed: {exc}") from exc
    # Before the links go absolute, while a self-link still starts with "#".
    _unwrap_heading_anchors(doc)
    doc.make_links_absolute(url, resolve_base_href=True, handle_failures="discard")

    title = _title(doc)
    page_text = _visible_text(doc)
    noscript = " ".join(" ".join(n.text_content().split()) for n in doc.iter("noscript")).strip()

    markdown, words = _readability(lxml.html.tostring(doc, encoding="unicode"))
    method, detail = "readability", ""
    if words < THIN_EXTRACT_WORDS:
        fallback = _fallback(doc)
        if fallback is not None and (words < MIN_WORDS or fallback[2] >= 2 * words):
            selector, markdown, fallback_words = fallback
            method, detail = f"fallback:{selector}", f"readability found {words} words; used <{selector}>"
            words = fallback_words
    page_words = len(page_text.split())
    if THIN_EXTRACT_WORDS <= words < page_words * LOST_SHARE:
        rescue = _rescue(doc, words=words, page_words=page_words)
        if rescue is not None:
            selector, markdown, rescue_words = rescue
            method = f"fallback:{selector}"
            detail = (
                f"readability kept {words:,} of the page's {page_words:,} words "
                f"({words / page_words:.1%}); used <{selector}> ({rescue_words:,} words) instead"
            )
            words = rescue_words
    if MIN_WORDS <= words < page_words * PARTIAL_SHARE:
        detail = f"extracted {words:,} of the page's {page_words:,} words; the rest was judged navigation"

    _refuse_thin(words, page_words=page_words, title=title, page_text=page_text, noscript=noscript)
    return Extracted(title=title, content=markdown, method=method, detail=detail)


def _refuse_thin(words: int, *, page_words: int, title: str, page_text: str, noscript: str) -> None:
    """Raise EngineFailure for a page whose extract is too thin to be the page; return for a real one."""
    if words < WALL_CHECK_WORDS:
        wall = classify_dead_page(f"{title} {page_text} {noscript}", empty_detail="")
        if wall.status != "empty":
            raise wall
    if words < JS_SUSPECT_WORDS:
        said = _says_needs_js(f"{page_text} {noscript}")
        if said:
            raise EngineFailure("error", f"no readable content — page needs JavaScript ({said!r}; {words} words)")
    if words < MIN_WORDS:
        # Every fallback down to <body> came up short as well.
        raise EngineFailure("error", f"no readable content ({page_words} words on the page) — page may need JavaScript")


def _unwrap_heading_anchors(doc: HtmlElement) -> None:
    """Keep the text of self-links inside headings, dropping the link; drop bare permalink glyphs.

    Readability discards a heading whose link density is over a third, and a
    heading that is its own permalink (`<h3><a href="#id">…</a></h3>`, the
    usual static-site markup) is all link. Measured on a blog article: every
    section heading vanished from the extract. A self-link with no words in
    it (Sphinx's `¶`, a `#`) is decoration and goes entirely.
    """
    for anchor in doc.xpath("//a[starts-with(@href, '#')]"):
        if not any(c.isalnum() for c in anchor.text_content()):
            anchor.drop_tree()
    for anchor in doc.xpath("(//h1|//h2|//h3|//h4|//h5|//h6)//a[starts-with(@href, '#')]"):
        anchor.drop_tag()


def _title(doc: HtmlElement) -> str:
    title = doc.findtext(".//title") or ""
    if not title.strip():
        h1 = doc.find(".//h1")
        title = h1.text_content() if h1 is not None else ""
    return " ".join(title.split())


def _visible(element: HtmlElement) -> HtmlElement:
    """Return a copy of the element without scripts, styles, noscript fallbacks and the head."""
    import copy

    clone = copy.deepcopy(element)
    for tag in _INVISIBLE:
        for node in list(clone.iter(tag)):
            node.drop_tree()
    return clone


def _visible_text(element: HtmlElement) -> str:
    """Return the text a reader would see, whitespace collapsed."""
    return " ".join(_visible(element).text_content().split())


def _readability(html: str) -> tuple[str, int]:
    """Run readability over the page and return (markdown, word count); ("", 0) when it finds nothing."""
    import lxml.html
    from readability import Document
    from readability.readability import Unparseable

    # readability logs its own failures with a traceback; the failure is
    # reported here, through the envelope, instead.
    logging.getLogger("readability").addHandler(logging.NullHandler())
    logging.getLogger("readability").propagate = False
    try:
        summary = Document(html).summary(html_partial=True)
    except Unparseable:
        return "", 0
    if not summary.strip():
        return "", 0
    words = len(lxml.html.fragment_fromstring(summary, create_parent="div").text_content().split())
    return to_markdown(summary), words


def _containers(doc: HtmlElement) -> Iterator[tuple[str, HtmlElement, int]]:
    """Yield (selector, visible copy, words) for each content container, in `_FALLBACK_XPATHS` order."""
    for selector, xpath in _FALLBACK_XPATHS:
        for element in doc.xpath(xpath):
            clone = _visible(element)
            yield selector, clone, len(clone.text_content().split())


def _fallback(doc: HtmlElement) -> tuple[str, str, int] | None:
    """Try the usual content containers; return (selector, markdown, words) for the first with text."""
    import lxml.html

    for selector, clone, words in _containers(doc):
        if words >= MIN_WORDS:
            return selector, to_markdown(lxml.html.tostring(clone, encoding="unicode")), words
    return None


def _rescue(doc: HtmlElement, *, words: int, page_words: int) -> tuple[str, str, int] | None:
    """Find a container to replace an extract that kept under LOST_SHARE of the page, or None to keep it.

    The first container that is itself no fragment (at least PARTIAL_SHARE of
    the page), is mostly prose (under MAX_LINK_SHARE links) and holds at least
    twice the extract's words outside links. RFC 9110 has a 877-word
    `#content` section before its `body`: the first rule skips it.
    """
    import lxml.html

    for selector, clone, total in _containers(doc):
        linked = sum(len(a.text_content().split()) for a in clone.iter("a"))
        if total >= page_words * PARTIAL_SHARE and linked < total * MAX_LINK_SHARE and total - linked >= 2 * words:
            return selector, to_markdown(lxml.html.tostring(clone, encoding="unicode")), total
    return None


def _says_needs_js(text: str) -> str:
    """Return the sentence in which a page asks for JavaScript, or ""."""
    lowered = text.lower()
    for marker in _JS_MARKERS:
        at = lowered.find(marker)
        if at >= 0:
            start = max(lowered.rfind(".", 0, at) + 1, at - 80)
            end = lowered.find(".", at)
            end = len(text) if end < 0 else end + 1
            return " ".join(text[start : min(end, at + 120)].split())
    return ""


def to_markdown(html: str) -> str:
    """Convert an HTML fragment to markdown, with blank-line runs collapsed."""
    from markdownify import markdownify

    markdown = markdownify(html, heading_style="ATX", bullets="-")
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    return re.sub(r"\n{3,}", "\n\n", markdown).strip()


def truncate(content: str, max_chars: int) -> tuple[str, int, bool]:
    """Cut content to max_chars (0 = unlimited); return (content, original length, truncated)."""
    total = len(content)
    if max_chars <= 0 or total <= max_chars:
        return content, total, False
    cut = content[:max_chars]
    # Prefer ending at a line break if one is near the limit.
    newline = cut.rfind("\n")
    if newline >= max_chars * 0.8:
        cut = cut[:newline]
    return cut.rstrip(), total, True

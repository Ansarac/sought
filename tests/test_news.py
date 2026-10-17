"""`--source news`: publisher URLs instead of Google News redirects, and an honest snippet.

The feed is a saved capture (`news_rss.xml`, cut to four items), and so is
one batchexecute answer (`news_batchexecute.txt`, byte for byte as received).
The article page is reduced to the one element decoding reads: the real page
is half a megabyte of script around it. Nothing here touches the network.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest
import requests

from sought import search as search_mod
from sought.config import Config
from sought.engines import verticals
from sought.engines.base import ResultList

ARTICLE = "https://news.google.com/rss/articles/"
BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
TS = "1792227984"


class Resp:
    def __init__(self, status: int = 200, text: str = "", url: str = "") -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)  # type: ignore[arg-type]


def article_page(article_id: str) -> str:
    """Reduce a real article page to the element that carries the decode parameters."""
    return (
        f'<c-wiz><div jscontroller="aLI87" data-n-a-id="{article_id}" '
        f'data-n-a-ts="{TS}" data-n-a-sg="SIG-{article_id[-6:]}" data-n-dnlg="false"></div></c-wiz>'
    )


def batch_answer(url: str) -> str:
    """Answer a decode call in the captured framing, for an arbitrary destination."""
    payload = json.dumps(["garturlres", url, 1])
    return ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", payload, None, None, None, "generic"], ["di", 9]])


class NewsSession:
    """Serves the saved feed and answers every decode step, unless told otherwise.

    `pages` / `batches` override the answer for the n-th article (0-based, in
    the order asked); a value may be an exception to raise.
    """

    def __init__(self, feed: str) -> None:
        self.feed = feed
        self.pages: dict[int, object] = {}
        self.batches: dict[int, object] = {}
        self.calls: list[tuple[str, str]] = []
        self.posted: list[dict] = []
        self._page_n = 0
        self._batch_n = 0

    def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        if url == "https://news.google.com/rss/search":
            return Resp(200, self.feed)
        assert url.startswith(ARTICLE)
        assert "?" not in url  # the bare id, not the `?oc=5` link that only bounces
        assert kwargs["timeout"] == verticals._DECODE_TIMEOUT  # noqa: SLF001
        n = self._page_n
        self._page_n += 1
        return _answer(self.pages.get(n, Resp(200, article_page(url.rsplit("/", 1)[-1]))))

    def post(self, url, *, data, headers, timeout):
        assert url == BATCH
        assert headers["Content-Type"].startswith("application/x-www-form-urlencoded")
        assert timeout == verticals._DECODE_TIMEOUT  # noqa: SLF001
        self.calls.append(("POST", url))
        self.posted.append(data)
        n = self._batch_n
        self._batch_n += 1
        return _answer(self.batches.get(n, Resp(200, batch_answer(f"https://publisher.example/{n}"))))


def _answer(value):
    if isinstance(value, Exception):
        raise value
    return value


@pytest.fixture
def news(saved):
    return NewsSession(saved("news_rss.xml"))


def _run(sess, *, limit=10):
    return verticals._news(sess, "berkshire abel earnings", Config(limit=limit))  # noqa: SLF001


# --- parsing the feed ---------------------------------------------------------


def test_the_publisher_suffix_moves_from_the_title_to_the_snippet(news):
    results = _run(news)

    assert results[0].title == "Greg Abel-Led Berkshire Hathaway Owns 26 Dividend Stocks. Here's the One I'd Buy First."
    assert results[0].snippet == "Yahoo Finance · Mon, 05 Oct 2026 07:00:00 GMT"
    assert results[1].snippet.startswith("CNBC · ")


def test_a_title_is_cut_only_by_its_own_exact_source(news):
    """A dash in the title is not a publisher suffix unless it names this item's source."""
    news.feed = news.feed.replace("Buy First. - Yahoo Finance</title>", "Buy First. - Yahoo</title>", 1)
    title = _run(news)[0].title
    assert title == "Greg Abel-Led Berkshire Hathaway Owns 26 Dividend Stocks. Here's the One I'd Buy First. - Yahoo"


def test_an_item_without_a_source_keeps_its_title_and_shows_the_date(news):
    feed = news.feed.replace('<source url="https://finance.yahoo.com">Yahoo Finance</source>', "", 1)
    news.feed = feed
    first = _run(news)[0]
    assert first.title.endswith(" - Yahoo Finance")
    assert first.snippet == "Mon, 05 Oct 2026 07:00:00 GMT"


# --- decoding links -----------------------------------------------------------


def test_every_link_is_decoded_to_its_publisher(news):
    results = _run(news)

    assert [r.url for r in results] == [f"https://publisher.example/{n}" for n in range(4)]
    assert isinstance(results, ResultList)
    assert results.detail == ""  # all decoded: nothing to disclose


def test_the_decode_request_carries_the_page_parameters(news):
    _run(news, limit=1)

    (form,) = news.posted
    rpc = json.loads(form["f.req"])[0][0]
    assert rpc[0] == "Fbv4je"
    inner = json.loads(rpc[1])
    assert inner[0] == "garturlreq"
    article_id = inner[2]
    assert news.calls[1] == ("GET", ARTICLE + article_id)
    assert inner[3] == int(TS)
    assert inner[4] == f"SIG-{article_id[-6:]}"
    # And it is a plain form body requests can encode, not pre-encoded text.
    assert parse_qs(f"f.req={form['f.req']}")


def test_the_limit_is_applied_before_decoding(news):
    """Two requests per article: a link cut from the output must not be decoded first."""
    results = _run(news, limit=2)

    assert len(results) == 2
    assert [m for m, _ in news.calls] == ["GET", "GET", "POST", "GET", "POST"]


def test_one_failed_article_keeps_its_link_and_is_counted(news):
    news.pages[1] = requests.ConnectionError("reset")
    results = _run(news)

    assert results[1].url.startswith(ARTICLE)
    assert [r.url.startswith("https://publisher.example/") for r in results] == [True, False, True, True]
    assert results.detail == "1 of 4 article URLs left as news.google.com redirects"


def test_a_page_without_parameters_keeps_its_link(news):
    news.pages[0] = Resp(200, "<html>a page that changed shape</html>")
    results = _run(news)

    assert results[0].url.startswith(ARTICLE)
    assert results.detail.startswith("1 of 4 ")
    assert "stopped" not in results.detail


def test_a_malformed_batchexecute_answer_keeps_its_link(news):
    news.batches[0] = Resp(200, ')]}\'\n\n[["wrb.fr","Fbv4je","not json"]]')
    news.batches[1] = Resp(200, ')]}\'\n\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"/relative\\"]"]]')
    results = _run(news)

    assert results[0].url.startswith(ARTICLE)
    assert results[1].url.startswith(ARTICLE)
    assert results.detail == "2 of 4 article URLs left as news.google.com redirects"


def test_a_429_stops_the_pass(news):
    news.pages[1] = Resp(429, "Too Many Requests")
    results = _run(news)

    assert results[0].url == "https://publisher.example/0"
    assert all(r.url.startswith(ARTICLE) for r in results[1:])
    # Nothing asked after the refusal.
    assert [m for m, _ in news.calls] == ["GET", "GET", "POST", "GET"]
    assert results.detail == "3 of 4 article URLs left as news.google.com redirects (decoding stopped: HTTP 429)"


def test_a_429_on_the_decode_call_stops_the_pass_too(news):
    news.batches[0] = Resp(429, "")
    results = _run(news)

    assert len(news.calls) == 3
    assert results.detail.endswith("(decoding stopped: HTTP 429)")


def test_a_bot_wall_stops_the_pass(news):
    news.pages[0] = Resp(200, "<html>Our systems have detected unusual traffic from your computer network.</html>")
    results = _run(news)

    assert len(news.calls) == 2
    assert results.detail == (
        "4 of 4 article URLs left as news.google.com redirects (decoding stopped: bot wall: 'unusual traffic')"
    )


def test_a_sorry_redirect_stops_the_pass(news):
    news.pages[0] = Resp(200, "<html></html>", url="https://www.google.com/sorry/index?continue=x")
    results = _run(news)

    assert len(news.calls) == 2
    assert results.detail.endswith("(decoding stopped: google /sorry wall)")


def test_a_timeout_stops_the_pass(news):
    news.pages[0] = requests.Timeout()
    results = _run(news)

    assert len(news.calls) == 2
    assert results.detail.endswith("(decoding stopped: no answer from news.google.com within 5s)")


def test_the_disclosure_reaches_the_ok_attempt(news, monkeypatch):
    """`verticals.search` slices the handler's answer; the note must survive that."""
    monkeypatch.setattr(verticals, "session", lambda _cfg: news)
    news.pages[0] = requests.ConnectionError("reset")

    out = search_mod.run("berkshire abel earnings", config=Config(limit=3), source="news", cache="off")

    assert out["attempts"] == [
        {
            "engine": "news",
            "status": "ok",
            "count": 3,
            "detail": "1 of 3 article URLs left as news.google.com redirects",
        }
    ]


# --- the batchexecute answer --------------------------------------------------


def test_the_captured_batchexecute_answer_parses(saved):
    url = "https://www.cnbc.com/2026/08/08/berkshire-hathaway-earnings-q2-2026.html"
    assert verticals.parse_batchexecute(saved("news_batchexecute.txt")) == url


def test_length_prefixed_framing_parses():
    framed = batch_answer("https://publisher.example/a").replace(")]}'\n\n", ")]}'\n\n123\n") + '\n25\n[["e",4]]\n'
    assert verticals.parse_batchexecute(framed) == "https://publisher.example/a"


def test_the_regex_fallback_survives_a_framing_change(saved):
    """Broken outer framing, intact payload: the URL is still there to be read."""
    mangled = saved("news_batchexecute.txt").replace("[[", "{{", 1)
    assert verticals.parse_batchexecute(mangled).startswith("https://www.cnbc.com/")


@pytest.mark.parametrize(
    "body",
    [
        "",
        ")]}'\n\n",
        ')]}\'\n\n[["wrb.fr","Fbv4je",null]]',
        ')]}\'\n\n[["wrb.fr","Fbv4je","[\\"garturlres\\"]"]]',
        ')]}\'\n\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"https://news.google.com/x\\"]"]]',
        ')]}\'\n\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"javascript:alert(1)\\"]"]]',
        ')]}\'\n\n[["er",null,null,null,null,400]]',
    ],
)
def test_anything_but_an_article_url_is_rejected(body):
    assert verticals.parse_batchexecute(body) is None

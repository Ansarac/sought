"""`sought fetch`: extraction against saved pages, classification against a fake session.

The fixtures are live captures (sanitised): a Python docs page, a blog
article, a JavaScript-only app shell, a Cloudflare challenge and a plain-text
RFC. The rule under test is the same as for search: content, or a classified
reason there is none — never an `ok` with nothing in it.
"""

from __future__ import annotations

import json

import pytest

from sought import cli, fetch
from sought.config import Config, FetchConfig
from sought.engines.base import EngineFailure
from sought.errors import UsageError

URL = "https://docs.python.org/3/library/textwrap.html"


# --- extraction, offline ----------------------------------------------------


def test_a_docs_page_extracts_to_markdown(saved):
    page = fetch.extract(saved("fetch_docs_python_textwrap.html"), URL)

    assert page.method == "readability"
    assert page.title.startswith("textwrap — Text wrapping and filling")
    assert page.content.startswith("# `textwrap` — Text wrapping and filling")
    assert "class textwrap.TextWrapper" in page.content
    # Sphinx's permalink glyph is decoration, not content.
    assert "¶" not in page.content
    # Relative links come out absolute, against the page's own URL.
    assert "](https://docs.python.org/3/library/textwrap.html#textwrap.TextWrapper" in page.content
    # Navigation chrome stays out.
    assert "Previous topic" not in page.content


def test_a_blog_article_keeps_its_section_headings(saved):
    """Measured: readability drops a heading that is all permalink, which is every heading here."""
    page = fetch.extract(
        saved("fetch_blog_article.html"), "https://jvns.ca/blog/2024/02/16/popular-git-config-options/"
    )

    assert page.title == "Popular git config options"
    assert "### `pull.ff only` or `pull.rebase true`" in page.content
    assert "### `merge.conflictstyle zdiff3`" in page.content
    assert page.content.startswith("Hello! I always wish")


def test_a_thin_readability_extract_falls_back_to_the_content_container(saved):
    """Measured on the docs index: readability kept the 22-word intro and dropped every section list."""
    page = fetch.extract(saved("fetch_docs_python_index.html"), "https://docs.python.org/3/")

    assert page.method == "fallback:[role=main]"
    assert "readability found 22 words" in page.detail
    assert "Library reference" in page.content


def test_a_link_list_with_no_container_falls_back_to_body():
    rows = "".join(f"<tr><td><a href='/item?id={i}'>Story number {i} about things</a></td></tr>" for i in range(30))
    page = fetch.extract(f"<html><body><table>{rows}</table></body></html>", "https://news.example/")
    assert page.method == "fallback:body"
    assert "Story number 7" in page.content


def test_an_extract_that_is_a_small_share_of_the_page_says_so():
    article = "<article><p>" + " ".join(["Real article sentence here."] * 10) + "</p></article>"
    links = "".join(f"<li><a href='/r/{i}'>Release {i} of the package, with notes</a></li>" for i in range(100))
    sidebar = f"<div class='sidebar'><ul>{links}</ul></div>"
    page = fetch.extract(f"<html><body>{article}{sidebar}</body></html>", URL)
    assert "of the page's" in page.detail
    assert "Real article sentence" in page.content


def test_a_javascript_shell_is_an_error_not_an_empty_page(saved):
    with pytest.raises(EngineFailure) as exc:
        fetch.extract(saved("fetch_js_shell.html"), "https://excalidraw.com/")

    assert exc.value.status == "error"
    assert "needs JavaScript" in exc.value.detail
    assert "You need to enable JavaScript to run this app." in exc.value.detail


def test_a_page_with_no_text_is_error_never_ok_with_empty_content():
    with pytest.raises(EngineFailure) as exc:
        fetch.extract("<html><head><title>App</title></head><body><div id='root'></div></body></html>", URL)

    assert exc.value.status == "error"
    assert exc.value.status != "empty"
    assert "no readable content" in exc.value.detail


def test_an_empty_body_is_an_error():
    with pytest.raises(EngineFailure) as exc:
        fetch.extract("   ", URL)
    assert exc.value.status == "error"


def test_a_bot_wall_page_is_rate_limited_through_the_shared_marker_list(saved):
    with pytest.raises(EngineFailure) as exc:
        fetch.extract(saved("marginalia_access_denied.html"), URL)

    assert exc.value.status == "rate_limited"
    assert "access denied" in exc.value.detail.lower()


def test_the_bot_wall_marker_list_is_not_duplicated():
    from sought import browser

    assert fetch.classify_dead_page is browser.classify_dead_page


def test_a_long_article_that_mentions_captcha_is_not_a_wall():
    """Markers are only checked on thin pages; an article about CAPTCHAs is content."""
    body = " ".join(["Solving a captcha is a task some services outsource to people."] * 40)
    page = fetch.extract(f"<html><body><article><p>{body}</p></article></body></html>", URL)
    assert "captcha" in page.content


def test_a_short_real_page_is_ok():
    """example.com has about twenty words, and it is a page, not a failure."""
    html = (
        "<html><head><title>Example Domain</title></head><body><div><h1>Example Domain</h1>"
        "<p>This domain is for use in documentation examples without needing permission. "
        "Avoid use in operations.</p><p><a href='https://iana.org/domains/example'>Learn more</a></p>"
        "</div></body></html>"
    )
    page = fetch.extract(html, "https://example.com/")
    assert "documentation examples" in page.content
    assert page.title == "Example Domain"


def test_an_xml_declaration_does_not_break_parsing(saved):
    html = '<?xml version="1.0" encoding="utf-8"?>\n' + saved("fetch_blog_article.html")
    assert fetch.extract(html, "https://jvns.ca/").title == "Popular git config options"


def test_truncation_reports_both_lengths():
    content, total, truncated = fetch.truncate("a" * 100, 40)
    assert (len(content), total, truncated) == (40, 100, True)
    assert fetch.truncate("abc", 0) == ("abc", 3, False)
    assert fetch.truncate("abc", 3) == ("abc", 3, False)


# --- classification, through a fake session ---------------------------------


@pytest.fixture
def http(monkeypatch, http_session):
    """Serve one canned response to fetch."""

    def serve(canned):
        sess = http_session(canned)
        monkeypatch.setattr(fetch, "session", lambda _cfg: sess)
        return sess

    return serve


HTML = {"Content-Type": "text/html; charset=utf-8"}


def test_an_ok_fetch_fills_the_whole_envelope(http, response, saved):
    sess = http(response(200, saved("fetch_docs_python_textwrap.html"), HTML, url=URL))

    out = fetch.fetch(URL, config=Config(), max_chars=0)

    assert out["status"] == "ok"
    assert out["final_url"] == URL
    assert out["http_status"] == 200
    assert out["content_type"] == "text/html; charset=utf-8"
    assert out["method"] == "readability"
    assert out["truncated"] is False
    assert out["chars"] == out["total_chars"] == len(out["content"])
    assert set(out) == {
        "url",
        "final_url",
        "status",
        "detail",
        "http_status",
        "content_type",
        "method",
        "title",
        "content",
        "chars",
        "total_chars",
        "truncated",
        "max_chars",
    }
    assert sess.calls[0][1]["timeout"] == Config().timeout_seconds


def test_sought_names_itself_instead_of_posing_as_a_browser(http, response, saved):
    sess = http(response(200, saved("fetch_docs_python_textwrap.html"), HTML))
    fetch.fetch(URL, config=Config(), max_chars=0)
    assert sess.headers["User-Agent"].startswith("sought/")
    assert "Chrome" not in sess.headers["User-Agent"]


def test_a_redirect_reports_the_final_url(http, response, saved):
    final = "https://docs.python.org/3.15/library/textwrap.html"
    http(response(200, saved("fetch_docs_python_textwrap.html"), HTML, url=final))
    out = fetch.fetch(URL, config=Config(), max_chars=0)
    assert (out["url"], out["final_url"]) == (URL, final)


def test_truncation_is_disclosed_in_the_envelope(http, response, saved):
    http(response(200, saved("fetch_docs_python_textwrap.html"), HTML))
    out = fetch.fetch(URL, config=Config(), max_chars=500)
    assert out["truncated"] is True
    assert out["chars"] <= 500 < out["total_chars"]
    assert out["max_chars"] == 500


def test_plain_text_passes_through_verbatim(http, response, saved):
    text = saved("fetch_rfc2119.txt")
    http(response(200, text, {"Content-Type": "text/plain;charset=utf-8"}))
    out = fetch.fetch("https://www.rfc-editor.org/rfc/rfc2119.txt", config=Config(), max_chars=0)
    assert out["status"] == "ok"
    assert out["method"] == "verbatim"
    assert out["content"] == text


def test_json_passes_through(http, response):
    http(response(200, '{"a": 1}', {"Content-Type": "application/vnd.api+json"}))
    out = fetch.fetch("https://api.example.com/x", config=Config(), max_chars=0)
    assert (out["status"], out["content"]) == ("ok", '{"a": 1}')


def test_an_empty_text_body_is_an_error(http, response):
    http(response(200, "", {"Content-Type": "text/plain"}))
    out = fetch.fetch("https://example.com/x.txt", config=Config(), max_chars=0)
    assert out["status"] == "error"
    assert out["content"] is None


def test_a_body_without_content_type_is_sniffed(http, response, saved):
    http(response(200, saved("fetch_blog_article.html"), {}))
    out = fetch.fetch("https://jvns.ca/x", config=Config(), max_chars=0)
    assert (out["status"], out["method"]) == ("ok", "readability")


def test_an_unsupported_type_is_an_error_naming_it_and_is_not_downloaded(http, response):
    canned = response(200, "%PDF-1.7 ...", {"Content-Type": "application/pdf"})
    http(canned)
    out = fetch.fetch("https://example.com/a.pdf", config=Config(), max_chars=0)
    assert out["status"] == "error"
    assert "application/pdf" in out["detail"]
    assert canned.body_read is False
    assert canned.closed is True


def test_an_oversized_body_is_refused_not_cut(http, response):
    http(response(200, "<html>", {"Content-Type": "text/html", "Content-Length": str(fetch.MAX_BODY_BYTES + 1)}))
    out = fetch.fetch(URL, config=Config(), max_chars=0)
    assert out["status"] == "error"
    assert "limit" in out["detail"]


def test_a_javascript_shell_through_the_whole_path_is_an_error(http, response, saved):
    http(response(200, saved("fetch_js_shell.html"), HTML))
    out = fetch.fetch("https://excalidraw.com/", config=Config(), max_chars=0)
    assert out["status"] == "error"
    assert out["http_status"] == 200
    assert "JavaScript" in out["detail"]


def test_a_cloudflare_challenge_is_a_bot_wall(http, response, saved):
    headers = {"Content-Type": "text/html; charset=UTF-8", "cf-mitigated": "challenge"}
    http(response(403, saved("fetch_cloudflare_challenge.html"), headers))
    out = fetch.fetch("https://www.npmjs.com/package/left-pad", config=Config(), max_chars=0)
    assert out["status"] == "rate_limited"
    assert "Cloudflare challenge" in out["detail"]
    assert out["http_status"] == 403


@pytest.mark.parametrize(
    ("code", "status"),
    [(404, "blocked"), (401, "blocked"), (500, "blocked"), (429, "rate_limited")],
)
def test_http_errors_are_classified(http, response, code, status):
    http(response(code, "nope", HTML))
    out = fetch.fetch(URL, config=Config(), max_chars=0)
    assert out["status"] == status
    assert out["detail"] == f"HTTP {code}"
    assert out["http_status"] == code
    assert out["content"] is None


def test_a_timeout_is_a_timeout(http):
    import requests

    http(requests.Timeout("too slow"))
    out = fetch.fetch(URL, config=Config(timeout_seconds=5), max_chars=0)
    assert out["status"] == "timeout"
    assert "5s" in out["detail"]


def test_a_connection_failure_is_blocked(http):
    import requests

    http(requests.ConnectionError("Failed to establish a new connection\nmore"))
    out = fetch.fetch(URL, config=Config(), max_chars=0)
    assert out["status"] == "blocked"
    assert out["detail"] == "Failed to establish a new connection"


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "example.com", "https://", ""])
def test_a_bad_url_is_a_usage_error(url):
    with pytest.raises(UsageError):
        fetch.check_url(url)


def test_a_bare_host_gets_a_hint_with_the_scheme():
    with pytest.raises(UsageError, match=r"https://example\.com"):
        fetch.check_url("example.com")


# --- CLI ---------------------------------------------------------------------


@pytest.fixture
def cli_http(http, monkeypatch):
    monkeypatch.setattr(cli, "_load_config", lambda **k: Config(fetch=FetchConfig(max_chars=500)))  # noqa: ARG005
    return http


def test_cli_ok_exits_zero_with_json(cli_http, response, saved, capsys):
    cli_http(response(200, saved("fetch_docs_python_textwrap.html"), HTML))
    assert cli.main(["fetch", URL]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    # The config's limit applies when no flag is given.
    assert (out["max_chars"], out["truncated"]) == (500, True)


def test_cli_max_chars_flag_overrides_config(cli_http, response, saved, capsys):
    cli_http(response(200, saved("fetch_docs_python_textwrap.html"), HTML))
    assert cli.main(["fetch", URL, "--max-chars", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["max_chars"], out["truncated"]) == (0, False)


def test_cli_pretty_discloses_truncation(cli_http, response, saved, capsys):
    cli_http(response(200, saved("fetch_docs_python_textwrap.html"), HTML))
    assert cli.main(["fetch", URL, "--pretty"]) == 0
    out = capsys.readouterr().out
    assert "TRUNCATED" in out
    assert "# textwrap" in out


def test_cli_failure_exits_two_and_still_prints_the_envelope(cli_http, response, capsys):
    cli_http(response(404, "missing", HTML))
    assert cli.main(["fetch", URL]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "blocked"
    assert "HTTP 404" in captured.err


def test_cli_pretty_failure_names_the_reason(cli_http, response, capsys):
    cli_http(response(429, "slow", HTML))
    assert cli.main(["fetch", URL, "--pretty"]) == 2
    assert "rate_limited" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["fetch", "ftp://x"], ["fetch", URL, "--max-chars", "-1"]])
def test_cli_bad_input_is_a_usage_error(cli_http, argv, capsys):
    assert cli.main(argv) == 2
    assert capsys.readouterr().out == ""

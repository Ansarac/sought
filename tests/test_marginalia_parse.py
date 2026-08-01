"""Marginalia HTML parsing, offline."""

from __future__ import annotations

from scout.engines.marginalia import parse

PAGE = """
<html><body>
<section class="card search-result">
  <div class="url"><a href="https://example.com/unix?a=1&amp;b=2">example.com</a></div>
  <a class="title" href="/x"><b>Unix</b> philosophy</a>
  <p class="description">Do one thing &amp; do it well.</p>
</section>
<section class="card search-result">
  <div class="url"><a href="https://second.example/page">second.example</a></div>
  <a class="title">Second
  result</a>
</section>
<section class="card search-result">
  <p class="description">malformed card with no url or title</p>
</section>
</body></html>
"""


def test_cards_are_extracted():
    results = parse(PAGE)
    assert len(results) == 2


def test_entities_are_decoded_in_url_title_and_snippet():
    first = parse(PAGE)[0]
    assert first.url == "https://example.com/unix?a=1&b=2"
    assert first.title == "Unix philosophy"
    assert first.snippet == "Do one thing & do it well."


def test_inline_tags_and_newlines_collapse_to_plain_text():
    second = parse(PAGE)[1]
    assert second.title == "Second result"
    assert second.snippet == ""


def test_malformed_cards_are_skipped_rather_than_yielding_blanks():
    """A result with no URL is worse than no result — it cannot be followed."""
    assert all(r.url and r.title for r in parse(PAGE))


def test_a_page_with_no_cards_parses_to_nothing():
    assert parse("<html><body>no results</body></html>") == []

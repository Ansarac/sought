"""The numbers behind `low_relevance`, for a reader to judge the flag rather than obey it.

`test_relevance.py` pins the decision and is deliberately left alone; nothing
here changes what is flagged. These tests pin only the description of it,
which used to claim "no result mentions any query term" — false whenever a
flagged result's title held a query word, which is exactly what a user saw.
"""

from __future__ import annotations

from sought.engines.base import Result, detect_low_relevance, query_tokens, relevance_report, relevance_score


def test_an_unflagged_result_set_has_no_report():
    results = [Result("Playwright docs", "https://playwright.dev", "browser automation")]
    assert relevance_report("playwright automation", results) is None


def test_no_results_and_unjudgeable_queries_have_no_report():
    assert relevance_report("anything", []) is None
    assert relevance_report("what is it", [Result("Unrelated", "https://example.com", "")]) is None


def test_a_flagged_set_reports_the_best_result_and_what_it_lacks():
    """The reported case: a query word in the title, and flagged all the same."""
    results = [
        Result("Berkshire Hathaway buys more Apple", "https://news.example/a", "Example Times · Sat, 08 Aug 2026"),
        Result("Quarterly results roundup", "https://news.example/b", "Example Wire · Fri, 07 Aug 2026"),
    ]
    query = "berkshire greg abel quarterly earnings"
    assert detect_low_relevance(query, results) is True

    report = relevance_report(query, results)

    assert report == {
        "best_coverage": round(relevance_score(query_tokens(query), results[0]), 2),
        "matched": ["berkshire"],
        "missing": ["greg", "abel", "earnings"],
    }
    assert report["best_coverage"] < 0.5


def test_the_report_exists_exactly_when_the_flag_is_raised():
    """`search._finish` reads the flag off the report; the two must never disagree."""
    cases = [
        ("playwright automation", [Result("Playwright docs", "https://playwright.dev", "browser automation")]),
        ("zigbee2mqtt", [Result("guide", "https://zigbee2mqtt.io/flashing", "")]),
        ("rust async runtime", [Result("Trustpilot Reviews", "https://trustpilot.example", "rate a business")]),
        ("what is it", [Result("Unrelated", "https://example.com", "")]),
        ("anything", []),
    ]
    for query, results in cases:
        assert (relevance_report(query, results) is not None) is detect_low_relevance(query, results), query


def test_a_token_only_another_result_mentions_is_neither_matched_nor_missing():
    results = [
        Result("zigbee2mqtt", "https://a.example", ""),
        Result("coordinator", "https://b.example", ""),
    ]
    report = relevance_report("zigbee2mqtt coordinator firmware flashing", results)
    assert report is not None
    assert report["matched"] == ["zigbee2mqtt"]
    assert report["missing"] == ["firmware", "flashing"]


def test_a_url_only_match_counts_in_the_coverage_but_is_not_called_a_mention():
    url_only = Result("guide", "https://zigbee2mqtt.io/flashing", "")
    assert relevance_report("zigbee2mqtt", [url_only]) == {
        "best_coverage": 0.4,
        "matched": [],
        "missing": ["zigbee2mqtt"],
    }


def test_a_repeated_query_word_is_listed_once():
    report = relevance_report("lidl lidl zigbee2mqtt firmware", [Result("Lidl", "https://lidl.example", "")])
    assert report is not None
    assert report["matched"] == ["lidl"]
    assert report["missing"] == ["zigbee2mqtt", "firmware"]

"""The poison detector.

The scenario under test is real: a search engine answering a blocked request
with HTTP 200 and a page of unrelated results. There is no exception to catch,
so this heuristic is the only thing standing between that page and a confident
wrong answer.
"""

from __future__ import annotations

from scout.engines.base import Result, detect_low_relevance, query_tokens


def test_stopwords_and_short_tokens_are_dropped():
    assert query_tokens("what is the go of it") == []
    assert query_tokens("How to use Playwright") == ["use", "playwright"]


def test_relevant_results_are_not_flagged():
    results = [Result("Playwright docs", "https://playwright.dev", "browser automation")]
    assert detect_low_relevance("playwright automation", results) is False


def test_the_measured_poisoning_case_is_flagged():
    """The actual output a search fetch returned for a zigbee query."""
    results = [
        Result("Lidl Online Shop", "https://www.lidl.de", "Angebote der Woche"),
        Result("Outlook, formerly Hotmail", "https://support.microsoft.com/hotmail", "sign in help"),
    ]
    assert detect_low_relevance("zigbee2mqtt coordinator firmware flashing", results) is True


def test_a_match_anywhere_in_the_record_clears_the_flag():
    for r in (
        Result("zigbee2mqtt guide", "https://example.com", ""),
        Result("guide", "https://zigbee2mqtt.io/flashing", ""),
        Result("guide", "https://example.com", "how to flash a zigbee2mqtt coordinator"),
    ):
        assert detect_low_relevance("zigbee2mqtt", [r]) is False


def test_one_relevant_result_among_junk_clears_the_flag():
    results = [
        Result("Lidl Online Shop", "https://www.lidl.de", ""),
        Result("Zigbee2MQTT flashing", "https://zigbee2mqtt.io", ""),
    ]
    assert detect_low_relevance("zigbee2mqtt flashing", results) is False


def test_matching_is_case_insensitive():
    results = [Result("ZIGBEE2MQTT", "https://example.com", "")]
    assert detect_low_relevance("zigbee2mqtt", results) is False


def test_empty_results_are_not_flagged():
    """Nothing found is reported through `attempts`, not through this flag."""
    assert detect_low_relevance("anything", []) is False


def test_an_all_stopword_query_cannot_be_judged():
    results = [Result("Unrelated", "https://example.com", "")]
    assert detect_low_relevance("what is it", results) is False

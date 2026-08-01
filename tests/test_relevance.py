"""The poison detector.

The scenario under test is real: a search engine answering a blocked request
with HTTP 200 and a page of unrelated results. There is no exception to catch,
so this heuristic is the only thing standing between that page and a confident
wrong answer.
"""

from __future__ import annotations

from sought.engines.base import Result, detect_low_relevance, query_tokens


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


def test_a_match_in_the_title_or_the_snippet_clears_the_flag():
    for r in (
        Result("zigbee2mqtt guide", "https://example.com", ""),
        Result("guide", "https://example.com", "how to flash a zigbee2mqtt coordinator"),
    ):
        assert detect_low_relevance("zigbee2mqtt", [r]) is False


def test_a_match_only_in_the_url_does_not_clear_the_flag_on_its_own():
    """A domain that contains the word is not the page discussing the subject.

    Otherwise anything hosted on npmjs.com vouches for "npm registry".
    """
    url_only = Result("guide", "https://zigbee2mqtt.io/flashing", "")
    assert detect_low_relevance("zigbee2mqtt", [url_only]) is True


def test_the_url_still_counts_toward_a_result_the_text_already_supports():
    """Partial credit, not zero: it is corroboration, just not evidence by itself."""
    r = Result("Coordinator flashing walkthrough", "https://zigbee2mqtt.io/guide", "flash the coordinator")
    assert detect_low_relevance("zigbee2mqtt coordinator flashing", [r]) is False


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


def test_pure_nonsense_is_always_flagged():
    """The sanity property. Nothing on the web covers this, so nothing may clear it.

    Measured live before the fix: this exact query came back `low_relevance:
    false`. `77` is below the minimum token length, both distinctive tokens
    matched nothing, and one title containing "unfindable" cleared the flag for
    the whole set.
    """
    results = [
        Result("Unscramble Letters : UNFINDABLE To Find Words", "https://wordfinder.example/u", "anagram solver"),
        Result("Cheap flights to Blorptown", "https://travel.example", "book now"),
        Result("Home | Acme Corp", "https://acme.example", "we make things"),
    ]
    assert detect_low_relevance("zqxjkvwmpf blorptangle 77 unfindable", results) is True


def test_one_weak_token_matching_does_not_excuse_every_distinctive_miss():
    """The union bug in miniature: coverage is judged per result, and as a fraction."""
    results = [Result("A collection of vintage postcards", "https://postcards.example", "browse the collection")]
    assert detect_low_relevance("wasm garbage collection proposal", results) is True


def test_substrings_are_not_mentions():
    """Three measured false negatives, all from `token in haystack`."""
    cases = [
        ("rust async runtime", Result("Trustpilot Reviews", "https://trustpilot.example", "rate a business")),
        ("mail server config", Result("Outlook, formerly Hotmail", "https://support.example/hotmail", "sign in")),
        ("nat traversal", Result("Alternative approaches", "https://alt.example", "an alternative to consider")),
    ]
    for query, result in cases:
        assert detect_low_relevance(query, [result]) is True, query


def test_a_single_good_result_among_junk_still_clears_the_flag():
    """Per-result scoring must not become "every result has to be relevant"."""
    results = [
        Result("Lidl Online Shop", "https://www.lidl.de", "Angebote der Woche"),
        Result("Trustpilot Reviews", "https://trustpilot.example", ""),
        Result("Tokio: an async runtime for Rust", "https://tokio.rs", "the Rust async runtime"),
    ]
    assert detect_low_relevance("rust async runtime", results) is False

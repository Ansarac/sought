"""Brave's selectors, pinned to the layouts that have been seen live.

The extraction script itself runs only in a browser, which this suite never
starts; what can drift silently here is which container counts as "a results
page", and that is exactly what broke.
"""

from __future__ import annotations

from sought.engines import brave


def test_both_the_old_and_the_2026_10_results_column_count_as_a_results_page():
    """Brave replaced `#results` with `section#mixed-main` beside an AI answer."""
    containers = {c.strip() for c in brave._RESULTS_CONTAINER.split(",")}  # noqa: SLF001
    assert containers == {"#results", "#mixed-main"}


def test_items_are_not_scoped_to_the_old_container():
    """Scoped to `#results`, the new layout yields nothing even once the page is recognised."""
    assert "#results" not in brave._ITEM  # noqa: SLF001
    assert brave._ITEM == '.snippet[data-type="web"]'  # noqa: SLF001

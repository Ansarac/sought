"""Shared test helpers, exposed as fixtures.

Engines are tested against saved upstream responses, never against the live
network: the whole point of this tool is that it runs on a network that
misbehaves, and a suite that needs that network working tests nothing
reliably.

Helpers arrive as fixtures rather than importable names because the suite runs
under `--import-mode=importlib` with no package around it, so `tests/` is not
on the import path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    """Just enough of `requests.Response` for the classification paths."""

    def __init__(self, status_code: int = 200, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        import requests

        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """A `requests.Session` stand-in that replays one canned response, or raises."""

    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture(scope="session")
def saved():
    """Read a saved upstream response out of tests/fixtures."""

    def read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return read


@pytest.fixture
def response():
    """Build a minimal `requests.Response` stand-in."""
    return FakeResponse


@pytest.fixture
def http_session():
    """Build a one-shot `requests.Session` stand-in."""
    return FakeSession

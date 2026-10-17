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
import os
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class NetworkUseInTestsError(RuntimeError):
    """A test reached for the real network or a real browser."""


def pytest_configure(config):
    """Make the network and a real Chromium unreachable for the whole run.

    The rule above used to be a convention, and conventions leak: a test that
    called `monkeypatch.undo()` also undid its engine stubs, and a real chain
    search went out through the proxy and wrote into the user's real cache.
    Installed here, at configure time, rather than through `monkeypatch`, so
    that undoing a test's patches restores these guards — never the real thing.

    Every TCP connect is refused, loopback included: the developer's
    `HTTPS_PROXY` points at localhost, so a leaked request's first hop is a
    loopback connection. Unix sockets stay open; nothing here uses them for the
    network. `sync_playwright` is replaced as well, because Chromium is a
    separate process the socket guard cannot see; the browser tests' fake
    driver is patched over it per test, as before.
    """
    import socket

    import playwright.sync_api

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def refuse(sock: socket.socket, address: object) -> None:
        if sock.family != socket.AF_UNIX:
            msg = f"test tried to open a network connection to {address!r}; use a fake session"
            raise NetworkUseInTestsError(msg)

    def connect(sock: socket.socket, address: object) -> None:
        refuse(sock, address)
        real_connect(sock, address)

    def connect_ex(sock: socket.socket, address: object) -> int:
        refuse(sock, address)
        return real_connect_ex(sock, address)

    def no_browser() -> None:
        msg = "test tried to start a real Playwright driver; use the `playwright` fake"
        raise NetworkUseInTestsError(msg)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    playwright.sync_api.sync_playwright = no_browser

    # Same reasoning for state: `isolated_cache` redirects it per test, but an
    # undone monkeypatch fell back to the real ~/.cache/sought. Point the
    # process-wide default somewhere disposable too.
    os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="sought-tests-cache-")


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


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path_factory, monkeypatch):
    """Point sought's state dir somewhere disposable, for every test.

    Any test that runs the chain with a `rate_limited` stub would otherwise
    write a real cooldown into ~/.cache/sought and hold back the user's next
    real search — and leave state behind that makes the next test run differ.
    """
    root = tmp_path_factory.mktemp("xdg-cache")
    monkeypatch.setenv("XDG_CACHE_HOME", str(root))
    return root / "sought"


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

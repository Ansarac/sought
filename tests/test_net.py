"""The HTTP session, and the bypass list that used to be decoration.

`proxy.no_proxy` was parsed, validated and advertised in the shipped config
template, and then read by nothing. Worse, configuring any proxy at all set
`trust_env = False`, which threw away the environment's NO_PROXY as well.
"""

from __future__ import annotations

import sys
from typing import Self

import pytest

from sought import net
from sought.config import Config, ProxyConfig

PROXIED = ProxyConfig(https="http://proxy.example:8080", no_proxy=("localhost", "127.0.0.1", ".internal"))


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch):
    """Keep the ambient environment from deciding the outcome of these tests."""
    for var in ("no_proxy", "NO_PROXY", "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(var, raising=False)


def test_no_configured_proxy_leaves_the_environment_in_charge():
    """None is not {}: it means "inherit", which requests already does well."""
    s = net.session(Config())
    assert s.proxies == {}
    assert s.trust_env is True


def test_a_configured_proxy_reaches_the_session():
    s = net.session(Config(proxy=PROXIED))
    assert s.proxies["https"] == "http://proxy.example:8080"
    assert s.trust_env is False


def test_the_bypass_list_actually_bypasses(monkeypatch):
    """Requests applies `no_proxy` only to proxies it takes from the environment.

    Against one set on the session it does nothing at all, so this is the only
    thing that makes the setting mean anything.
    """
    s = net.session(Config(proxy=PROXIED))

    settings = s.merge_environment_settings("http://localhost:8000/x", {}, None, None, None)

    assert settings["proxies"] == {}


def test_a_host_outside_the_bypass_list_still_goes_through_the_proxy():
    s = net.session(Config(proxy=PROXIED))

    settings = s.merge_environment_settings("https://example.com/x", {}, None, None, None)

    assert settings["proxies"]["https"] == "http://proxy.example:8080"


def test_a_suffix_entry_covers_its_subdomains():
    s = net.session(Config(proxy=PROXIED))
    settings = s.merge_environment_settings("https://wiki.internal/x", {}, None, None, None)
    assert settings["proxies"] == {}


def test_the_environments_no_proxy_survives_trust_env_being_switched_off(monkeypatch):
    """Turning trust_env off to pin the proxy must not silently widen it."""
    monkeypatch.setenv("no_proxy", "intranet.example")
    cfg = Config(proxy=ProxyConfig(https="http://proxy.example:8080"))

    s = net.session(cfg)

    assert s.merge_environment_settings("https://intranet.example/x", {}, None, None, None)["proxies"] == {}


def test_the_config_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("no_proxy", "intranet.example")
    assert net.proxy_bypass_list(Config(proxy=PROXIED)) == "localhost,127.0.0.1,.internal"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows environment names are case-insensitive: one variable")
def test_the_lowercase_variable_wins_over_the_uppercase_one(monkeypatch):
    """Matching Chromium, which prefers the lowercase name when the two disagree."""
    monkeypatch.setenv("no_proxy", "lower.example")
    monkeypatch.setenv("NO_PROXY", "upper.example")
    assert net.proxy_bypass_list(Config()) == "lower.example"


def test_nothing_set_anywhere_is_an_empty_list():
    assert net.proxy_bypass_list(Config()) == ""


def test_the_requests_mapping_carries_the_bypass_list_under_its_documented_key():
    assert PROXIED.as_requests_mapping()["no_proxy"] == "localhost,127.0.0.1,.internal"


def test_the_mapping_is_none_when_no_proxy_is_configured():
    """A bypass list with nothing to bypass is not a reason to stop inheriting."""
    assert ProxyConfig(no_proxy=("localhost",)).as_requests_mapping() is None


def test_there_are_no_system_proxy_settings_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert net.system_proxy() is None


class _FakeKey:
    def __init__(self, values) -> None:
        self.values = values

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture
def registry(monkeypatch):
    """Serve WinINET values from a dict instead of the real registry."""
    import types

    def install(values):
        fake = types.SimpleNamespace(
            HKEY_CURRENT_USER=object(),
            OpenKey=lambda _root, _path: _FakeKey(values),
            QueryValueEx=lambda key, name: (key.values[name], 1) if name in key.values else _missing(name),
        )
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "winreg", fake)

    return install


def _missing(name):
    raise FileNotFoundError(name)


def test_a_pac_script_is_reported(registry):
    registry({"ProxyEnable": 0, "AutoConfigURL": "http://wpad.example/proxy.pac", "ProxyServer": "stale:3128"})
    assert net.system_proxy() == net.SystemProxy(pac_url="http://wpad.example/proxy.pac", server="")


def test_a_manual_proxy_counts_only_while_it_is_switched_on(registry):
    """ProxyServer outlives ProxyEnable=0; urllib ignores it then, and so must the report."""
    registry({"ProxyEnable": 1, "ProxyServer": "proxy.example:3128"})
    assert net.system_proxy() == net.SystemProxy(server="proxy.example:3128")


def test_nothing_configured_in_windows_is_none(registry):
    registry({"ProxyEnable": 0, "ProxyServer": "stale:3128"})
    assert net.system_proxy() is None


def test_the_session_still_announces_a_desktop_user_agent():
    assert "Mozilla/5.0" in net.session(Config()).headers["User-Agent"]

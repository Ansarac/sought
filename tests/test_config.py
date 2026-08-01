"""Config parsing, precedence, and validation."""

from __future__ import annotations

import pytest

from sought import config as config_mod
from sought.config import DEFAULT_ORDER, Config, load
from sought.errors import ConfigError


def test_absent_file_yields_builtin_defaults(tmp_path):
    cfg = load(tmp_path / "nope.toml")
    assert cfg.order == DEFAULT_ORDER
    assert cfg.system_trust is True
    assert cfg.source_path is None


def test_full_file_is_read(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [proxy]
        https = "http://p:8080"
        no_proxy = ["localhost"]
        [engines]
        order = ["marginalia"]
        timeout_seconds = 5
        limit = 3
        [tls]
        system_trust = false
        browser_ignore_cert_errors = false
        """,
        encoding="utf-8",
    )
    cfg = load(path)
    assert cfg.order == ("marginalia",)
    assert cfg.timeout_seconds == 5
    assert cfg.limit == 3
    assert cfg.system_trust is False
    assert cfg.browser_ignore_cert_errors is False
    assert cfg.proxy.https == "http://p:8080"


def test_partial_file_keeps_defaults_for_the_rest(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[engines]\nlimit = 2\n", encoding="utf-8")
    cfg = load(path)
    assert cfg.limit == 2
    assert cfg.order == DEFAULT_ORDER


@pytest.mark.parametrize(
    "body",
    [
        "[engines]\norder = []\n",
        "[engines]\ntimeout_seconds = 0\n",
        "[engines]\ntimeout_seconds = -1\n",
        "[engines]\nlimit = 'ten'\n",
        "[engines]\norder = [1, 2]\n",
        "[tls]\nsystem_trust = 'yes'\n",
        "[proxy]\nhttp = 8080\n",
        "not valid toml [[[",
    ],
)
def test_bad_values_are_rejected(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load(path)


def test_bools_are_not_accepted_as_ints(tmp_path):
    """`timeout_seconds = true` is a mistake, not a truthy 1."""
    path = tmp_path / "config.toml"
    path.write_text("[engines]\ntimeout_seconds = true\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(path)


def test_ensure_config_file_is_idempotent(tmp_path):
    path = tmp_path / "sub" / "config.toml"
    written, created = config_mod.ensure_config_file(path)
    assert created is True
    assert written.exists()

    marker = written.read_text(encoding="utf-8")
    _again, created_again = config_mod.ensure_config_file(path)
    assert created_again is False
    assert written.read_text(encoding="utf-8") == marker


def test_the_written_default_parses_back_to_the_builtin_defaults(tmp_path):
    """The shipped template must not drift from the dataclass defaults."""
    path = tmp_path / "config.toml"
    config_mod.ensure_config_file(path)
    loaded = load(path)
    builtin = Config()
    assert loaded.order == builtin.order
    assert loaded.timeout_seconds == builtin.timeout_seconds
    assert loaded.limit == builtin.limit
    assert loaded.system_trust == builtin.system_trust
    assert loaded.browser_ignore_cert_errors == builtin.browser_ignore_cert_errors


def test_proxy_mapping_is_none_when_nothing_is_set():
    """None means "inherit from the environment", which is not the same as {}."""
    assert Config().proxy.as_requests_mapping() is None
    assert Config().proxy.browser_server() is None

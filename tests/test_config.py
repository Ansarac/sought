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
        "[engines]\ncooldown_seconds = -1\n",
        "[engines]\ncooldown_seconds = true\n",
        "[engines]\ncooldown_seconds = 1.5\n",
        "[cache]\nttl_seconds = -1\n",
        "[engines]\nchallenge_wait_seconds = 0\n",
        "[engines]\nchallenge_wait_seconds = -5\n",
        "[engines]\nchallenge_wait_seconds = true\n",
        "[engines]\nchallenge_wait_seconds = '60'\n",
        "google = true\n",
        "[google]\nenabled = 'yes'\n",
        "[google]\nenabled = 1\n",
        "[google]\nmax_per_hour = 0\n",
        "[google]\nmax_per_hour = -1\n",
        "[google]\nmax_per_hour = true\n",
        "[cache]\nttl_seconds = 'long'\n",
        "cache = 5\n",
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
    assert loaded.cooldown_seconds == builtin.cooldown_seconds
    assert loaded.cache_ttl_seconds == builtin.cache_ttl_seconds
    assert loaded.challenge_wait_seconds == builtin.challenge_wait_seconds
    assert loaded.google == builtin.google


def test_google_is_off_by_default_and_not_in_the_default_order():
    cfg = Config()
    assert cfg.google.enabled is False
    assert cfg.google.max_per_hour == 20
    assert "google" not in cfg.order


def test_the_google_table_is_read(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[google]\nenabled = true\nmax_per_hour = 5\n", encoding="utf-8")
    cfg = load(path)
    assert (cfg.google.enabled, cfg.google.max_per_hour) == (True, 5)


def test_the_template_explains_the_google_trade_off_before_the_switch():
    template = config_mod.DEFAULT_CONFIG_TEMPLATE
    section = template[template.index("[google]") :]
    for phrase in ("--disable-blink-features=AutomationControlled", "fingerprint evasion", "egress IP", "max_per_hour"):
        assert phrase in section
    assert section.index("fingerprint evasion") < section.index("enabled = false")


def test_the_challenge_wait_is_read_and_defaults_to_a_minute(tmp_path):
    """39 s was measured; the default leaves room above that."""
    assert Config().challenge_wait_seconds == 60
    path = tmp_path / "config.toml"
    path.write_text("[engines]\nchallenge_wait_seconds = 90\n", encoding="utf-8")
    assert load(path).challenge_wait_seconds == 90


def test_cooldown_and_cache_ttl_are_read(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[engines]\ncooldown_seconds = 120\n[cache]\nttl_seconds = 60\n", encoding="utf-8")
    cfg = load(path)
    assert (cfg.cooldown_seconds, cfg.cache_ttl_seconds) == (120, 60)


def test_zero_switches_cooldown_and_cache_off_rather_than_being_rejected(tmp_path):
    """Unlike a limit or a timeout, zero is a meaningful duration here."""
    path = tmp_path / "config.toml"
    path.write_text("[engines]\ncooldown_seconds = 0\n[cache]\nttl_seconds = 0\n", encoding="utf-8")
    cfg = load(path)
    assert (cfg.cooldown_seconds, cfg.cache_ttl_seconds) == (0, 0)


def test_the_non_negative_validator_did_not_loosen_positive_int():
    with pytest.raises(ConfigError):
        config_mod.positive_int(0, "engines.limit")


def test_proxy_mapping_is_none_when_nothing_is_set():
    """None means "inherit from the environment", which is not the same as {}."""
    assert Config().proxy.as_requests_mapping() is None
    assert Config().proxy.browser_server() is None

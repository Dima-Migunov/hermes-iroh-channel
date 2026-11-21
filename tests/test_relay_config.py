"""Relay configuration parsing tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transport
from conftest import requires_iroh


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_unset_or_blank_uses_default_mode(value):
    config = transport.parse_relay_setting(value)

    assert config.kind == "default"
    assert config.describe() == "default"


@pytest.mark.parametrize("value", ["disabled", "off", "none", "DISABLED", " Off "])
def test_disabled_spellings(value):
    config = transport.parse_relay_setting(value)

    assert config.kind == "disabled"
    assert config.describe() == "disabled"


def test_single_url():
    config = transport.parse_relay_setting("https://relay.example.com")

    assert config.kind == "custom"
    assert config.urls == ("https://relay.example.com",)


def test_comma_separated_urls_are_trimmed():
    config = transport.parse_relay_setting(
        " https://a.example.com , https://b.example.com:8443/path "
    )

    assert config.kind == "custom"
    assert config.urls == (
        "https://a.example.com",
        "https://b.example.com:8443/path",
    )
    assert config.describe() == "https://a.example.com,https://b.example.com:8443/path"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("relay.example.com", "entry 1 must use the http or https scheme"),
        ("https://", "entry 1 has no hostname"),
        (
            "not a url",
            "entry 1 contains whitespace or control characters",
        ),
        (
            "https://a.example.com,,https://b.example.com",
            "entry 2 is empty",
        ),
        ("https://a.example.com,", "entry 2 is empty"),
        ("ftp://relay.example.com", "entry 1 must use the http or https scheme"),
        ("https://:443", "entry 1 has no hostname"),
        (
            "https://relay.example.com:notaport",
            "entry 1 has an invalid port",
        ),
        (
            "https://exa mple.com",
            "entry 1 contains whitespace or control characters",
        ),
    ],
)
def test_invalid_values_fail_with_position_and_reason(value, reason):
    with pytest.raises(transport.RelayConfigError) as excinfo:
        transport.parse_relay_setting(value)

    message = str(excinfo.value)
    assert "HERMES_IROH_RELAY" in message
    assert reason in message
    assert value.strip() not in message


def test_relay_credentials_are_refused_without_echoing_them():
    with pytest.raises(transport.RelayConfigError) as excinfo:
        transport.parse_relay_setting(
            "https://user:secret@relay.example.com"
        )

    message = str(excinfo.value)
    assert "entry 1 must not include userinfo" in message
    assert "user:" not in message
    assert "secret" not in message
    assert "relay.example.com" not in message


def test_describe_redacts_userinfo_query_and_fragment():
    config = transport.RelayConfig(
        "raw",
        "custom",
        ("https://user:secret@relay.example.com:8443/path?token=abc#frag",),
    )

    assert config.describe() == "https://relay.example.com:8443/path"


def test_describe_redacts_a_query_string():
    config = transport.parse_relay_setting(
        "https://relay.example.com/path?token=abc"
    )

    assert config.describe() == "https://relay.example.com/path"
    assert "token" not in config.describe()


def test_build_relay_mode_error_does_not_echo_the_setting(monkeypatch):
    class FailingRelayMode:
        @staticmethod
        def custom_from_urls(urls):
            raise RuntimeError(f"rejected {urls[0]}")

    fake_iroh = type(sys)("iroh")
    fake_iroh.RelayMode = FailingRelayMode
    monkeypatch.setitem(sys.modules, "iroh", fake_iroh)
    config = transport.parse_relay_setting(
        "https://relay.example.com/path?token=abc"
    )

    with pytest.raises(transport.RelayConfigError) as excinfo:
        transport.build_relay_mode(config)

    message = str(excinfo.value)
    assert "token" not in message
    assert "relay.example.com" not in message
    assert "RuntimeError" in message


@requires_iroh
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "default"), ("disabled", "disabled"), ("https://relay.example.com", "custom")],
)
def test_relay_mode_construction(value, expected):
    import iroh

    config = transport.parse_relay_setting(value)
    mode = transport.build_relay_mode(config)

    assert isinstance(mode, iroh.RelayMode)
    relay_map = mode.relay_map()
    if expected == "disabled":
        assert relay_map.is_empty() is True
    elif expected == "custom":
        assert relay_map.urls() == ["https://relay.example.com/"]
    else:
        assert relay_map.len() > 0


@requires_iroh
def test_custom_relay_list_is_used_verbatim():
    import iroh

    config = transport.parse_relay_setting(
        "https://a.example.com,https://b.example.com"
    )
    mode = transport.build_relay_mode(config)

    assert isinstance(mode, iroh.RelayMode)
    assert mode.relay_map().urls() == [
        "https://a.example.com/",
        "https://b.example.com/",
    ]

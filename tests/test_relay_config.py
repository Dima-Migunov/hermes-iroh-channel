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
    "value",
    [
        "relay.example.com",
        "https://",
        "not a url",
        "https://a.example.com,,https://b.example.com",
        "https://a.example.com,",
        "ftp://relay.example.com",
    ],
)
def test_invalid_values_fail_with_diagnostic(value):
    with pytest.raises(transport.RelayConfigError) as excinfo:
        transport.parse_relay_setting(value)

    assert "HERMES_IROH_RELAY" in str(excinfo.value)
    assert value.strip() in str(excinfo.value)


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

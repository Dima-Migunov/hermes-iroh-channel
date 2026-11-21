"""Server limit parsing and propagation tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway_adapter
import transport

LIMIT_ENV = (
    "HERMES_IROH_MAX_PENDING_HANDSHAKES",
    "HERMES_IROH_MAX_STREAMS_PER_NODE",
    "HERMES_IROH_MAX_PROMPTS_PER_MINUTE",
    "HERMES_IROH_MAX_RESPONSE_BYTES",
)


def test_defaults_match_the_documented_values(clean_env):
    limits = transport.server_limits_from_env()

    assert limits == transport.ServerLimits(
        max_pending_handshakes=32,
        max_streams_per_node=2,
        max_prompts_per_minute=30,
        max_response_bytes=4 * 1024 * 1024,
    )


def test_env_overrides_are_applied(clean_env, monkeypatch):
    monkeypatch.setenv("HERMES_IROH_MAX_PENDING_HANDSHAKES", "7")
    monkeypatch.setenv("HERMES_IROH_MAX_STREAMS_PER_NODE", "3")
    monkeypatch.setenv("HERMES_IROH_MAX_PROMPTS_PER_MINUTE", "11")
    monkeypatch.setenv("HERMES_IROH_MAX_RESPONSE_BYTES", "1024")

    limits = transport.server_limits_from_env()

    assert limits.max_pending_handshakes == 7
    assert limits.max_streams_per_node == 3
    assert limits.max_prompts_per_minute == 11
    assert limits.max_response_bytes == 1024


@pytest.mark.parametrize("name", LIMIT_ENV)
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "lots"])
def test_invalid_limit_values_fail_with_the_variable_name(
    clean_env, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)

    with pytest.raises(transport.TransportError) as excinfo:
        transport.server_limits_from_env()

    assert name in str(excinfo.value)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-5", "86401", "abc"])
def test_turn_timeout_rejects_non_finite_and_out_of_range(
    clean_env, monkeypatch, value
):
    monkeypatch.setenv("HERMES_IROH_TURN_TIMEOUT", value)

    with pytest.raises(transport.TransportError) as excinfo:
        transport.turn_timeout_from_env()

    assert "HERMES_IROH_TURN_TIMEOUT" in str(excinfo.value)


@pytest.mark.parametrize(("value", "expected"), [("1", 1.0), ("600", 600.0), ("86400", 86400.0), ("1.5", 1.5)])
def test_turn_timeout_accepts_finite_in_range_values(
    clean_env, monkeypatch, value, expected
):
    monkeypatch.setenv("HERMES_IROH_TURN_TIMEOUT", value)

    assert transport.turn_timeout_from_env() == expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_pending_handshakes": 0},
        {"max_streams_per_node": 0},
        {"max_prompts_per_minute": 0},
        {"max_response_bytes": 0},
    ],
)
def test_server_rejects_non_positive_constructor_limits(kwargs):
    with pytest.raises(ValueError):
        transport.ChannelServer(**kwargs)


class FakeChannelServer:
    running = False

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    def session_for(self, chat_id):
        return None


def make_service(tmp_path) -> gateway_adapter.ChannelService:
    return gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(path=tmp_path / "node.key"),
        allowlist=transport.Allowlist(
            path=tmp_path / "allowlist.json", env_reader=lambda: ""
        ),
    )


async def test_service_passes_parsed_limits_to_the_server(
    tmp_path, monkeypatch, clean_env
):
    monkeypatch.setenv("HERMES_IROH_MAX_PENDING_HANDSHAKES", "5")
    monkeypatch.setenv("HERMES_IROH_MAX_STREAMS_PER_NODE", "6")
    monkeypatch.setenv("HERMES_IROH_MAX_PROMPTS_PER_MINUTE", "7")
    monkeypatch.setenv("HERMES_IROH_MAX_RESPONSE_BYTES", "8192")
    monkeypatch.setattr(transport, "ChannelServer", FakeChannelServer)
    service = make_service(tmp_path)

    server = await service.start()

    assert server.kwargs["max_pending_handshakes"] == 5
    assert server.kwargs["max_streams_per_node"] == 6
    assert server.kwargs["max_prompts_per_minute"] == 7
    assert server.kwargs["max_response_bytes"] == 8192


async def test_invalid_limit_env_fails_startup(
    tmp_path, monkeypatch, clean_env
):
    monkeypatch.setattr(transport, "ChannelServer", FakeChannelServer)
    monkeypatch.setenv("HERMES_IROH_MAX_STREAMS_PER_NODE", "0")
    service = make_service(tmp_path)

    with pytest.raises(transport.TransportError):
        await service.start()

    assert service.server is None

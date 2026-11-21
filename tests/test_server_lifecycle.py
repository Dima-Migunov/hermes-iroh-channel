"""Lifecycle tests for ChannelServer and ChannelService using a fake Iroh SDK."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway_adapter
import transport

NODE_A = "a" * 64


class FakeSecret:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def to_bytes(self) -> bytes:
        return self.raw

    def public(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


class FakeBackend:
    def generate(self) -> FakeSecret:
        return FakeSecret(b"\x2a" * transport.SECRET_KEY_BYTES)

    def load(self, raw: bytes) -> FakeSecret:
        return FakeSecret(raw)

    def to_bytes(self, secret: FakeSecret) -> bytes:
        return secret.raw

    def node_id(self, secret: FakeSecret) -> str:
        return secret.public()


class FakeAllowlist(transport.Allowlist):
    def __init__(self, path=None) -> None:
        super().__init__(path=path, env_reader=lambda: "")


class FakeSend:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed = False

    async def write_all(self, data: bytes) -> None:
        self.chunks.append(data)

    async def finish(self) -> None:
        self.closed = True

    def frames(self) -> list[dict]:
        return [json.loads(chunk.decode("utf-8")) for chunk in self.chunks]


class FakeEndpoint:
    def __init__(self) -> None:
        self.closed = False
        self.accept_calls = 0

    def id(self) -> str:
        return "fake-node-id"

    def addr(self) -> tuple[str]:
        return ("fake-addr",)

    async def accept_next(self) -> None:
        self.accept_calls += 1
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


def install_fake_iroh(
    monkeypatch, *, endpoint=None, ticket_factory=None, bind=None
):
    endpoint = endpoint if endpoint is not None else FakeEndpoint()
    if ticket_factory is None:
        def ticket_factory(addr):
            return f"ticket:{addr[0]}"

    async def default_bind(options):
        return endpoint

    class FakeEndpointOptions:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class FakeRelayMode:
        @staticmethod
        def default_mode():
            return "default"

        @staticmethod
        def disabled():
            return "disabled"

        @staticmethod
        def custom_from_urls(urls):
            return ("custom", tuple(urls))

    class FakeEndpointTicket:
        @staticmethod
        def from_addr(addr):
            return ticket_factory(addr)

    bind_func = bind if bind is not None else default_bind

    class FakeEndpointClass:
        bind = staticmethod(bind_func)

    fake = SimpleNamespace(
        iroh_ffi=SimpleNamespace(uniffi_set_event_loop=lambda loop: None),
        preset_n0=lambda: "preset",
        RelayMode=FakeRelayMode,
        EndpointOptions=FakeEndpointOptions,
        Endpoint=FakeEndpointClass,
        EndpointTicket=FakeEndpointTicket,
    )
    monkeypatch.setitem(sys.modules, "iroh", fake)
    return endpoint


def make_server(tmp_path) -> transport.ChannelServer:
    return transport.ChannelServer(
        key_store=transport.NodeKeyStore(
            path=tmp_path / "node.key", backend=FakeBackend()
        ),
        allowlist=FakeAllowlist(path=tmp_path / "allowlist.json"),
    )


async def test_partial_start_rolls_back_the_bound_endpoint(
    tmp_path, monkeypatch, clean_env
):
    def failing_ticket(addr):
        raise RuntimeError("ticket conversion failed")

    endpoint = install_fake_iroh(monkeypatch, ticket_factory=failing_ticket)
    server = make_server(tmp_path)

    with pytest.raises(transport.TransportError):
        await server.start()

    assert server.running is False
    assert server.node_id is None
    assert server.ticket is None
    assert server._accept_task is None
    assert endpoint.closed is True


async def test_concurrent_starts_bind_exactly_once(
    tmp_path, monkeypatch, clean_env
):
    first_entered = asyncio.Event()
    release = asyncio.Event()
    binds: list[object] = []

    async def gated_bind(options):
        binds.append(options)
        first_entered.set()
        await release.wait()
        return endpoint

    endpoint = FakeEndpoint()
    install_fake_iroh(monkeypatch, endpoint=endpoint, bind=gated_bind)
    server = make_server(tmp_path)

    first = asyncio.create_task(server.start())
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    second = asyncio.create_task(server.start())
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.wait_for(
        asyncio.gather(first, second), timeout=1
    )

    assert results == [None, None]
    assert len(binds) == 1
    assert server.running is True
    assert server.ticket == "ticket:fake-addr"
    assert server._accept_task is not None
    await server.stop()


def make_service(tmp_path) -> gateway_adapter.ChannelService:
    return gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(
            path=tmp_path / "node.key", backend=FakeBackend()
        ),
        allowlist=FakeAllowlist(path=tmp_path / "allowlist.json"),
    )


async def test_service_concurrent_starts_create_one_server(
    tmp_path, monkeypatch
):
    created: list[object] = []
    release = asyncio.Event()

    class FakeChannelServer:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.running = False
            created.append(self)

        async def start(self):
            await release.wait()
            self.running = True

        async def stop(self):
            self.running = False

        def session_for(self, chat_id):
            return None

    monkeypatch.setattr(transport, "ChannelServer", FakeChannelServer)
    service = make_service(tmp_path)

    first = asyncio.create_task(service.start())
    await asyncio.sleep(0)
    second = asyncio.create_task(service.start())
    await asyncio.sleep(0)
    release.set()
    first_server, second_server = await asyncio.wait_for(
        asyncio.gather(first, second), timeout=1
    )

    assert len(created) == 1
    assert first_server is second_server is service.server
    assert service.server.running is True


async def test_service_failed_start_leaves_server_none(tmp_path, monkeypatch):
    class FailingChannelServer:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.running = False

        async def start(self):
            raise transport.TransportError("bind failed")

        async def stop(self):
            self.running = False

        def session_for(self, chat_id):
            return None

    monkeypatch.setattr(transport, "ChannelServer", FailingChannelServer)
    service = make_service(tmp_path)

    with pytest.raises(transport.TransportError):
        await service.start()

    assert service.server is None


async def test_reject_stream_writes_one_terminal_error(tmp_path):
    send = FakeSend()

    class FakeBiStream:
        def send(self):
            return send

    server = make_server(tmp_path)
    await server._reject_stream(FakeBiStream())

    assert send.frames() == [
        {"type": "error", "message": transport.ERR_TOO_MANY_STREAMS}
    ]
    assert send.closed is True

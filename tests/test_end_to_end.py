"""End-to-end tests: real Iroh loopback through the adapter bridge."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway_adapter
import transport
from conftest import requires_iroh

pytestmark = requires_iroh


class FakeSource:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class FakeEvent:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class FakeSendResult:
    def __init__(self, success: bool, message_id=None, error=None) -> None:
        self.success = success
        self.message_id = message_id
        self.error = error


class FakeAdapterBase:
    def __init__(self, config, platform) -> None:
        self.config = config
        self.platform = platform
        self.connected = False
        self.events: list[FakeEvent] = []

    def _mark_connected(self) -> None:
        self.connected = True

    def _mark_disconnected(self) -> None:
        self.connected = False

    def build_source(self, **kwargs):
        return FakeSource(**kwargs)

    async def handle_message(self, event) -> None:
        self.events.append(event)


class GatewayResponder:
    """Fake gateway turn: streams through the adapter's send/edit path."""

    def __init__(self, adapter, service, reply: str, *, streamed: bool = True) -> None:
        self.adapter = adapter
        self.service = service
        self.reply = reply
        self.streamed = streamed
        self.turns = 0
        self.events: list[FakeEvent] = []

    async def __call__(self, event) -> None:
        self.turns += 1
        self.events.append(event)
        chat_id = event.source.chat_id
        if not self.streamed:
            await self.adapter.send(chat_id, self.reply, metadata={"notify": True})
            return
        third = max(1, len(self.reply) // 3)
        preview = self.reply[:third]
        await self.adapter.send(
            chat_id, preview, metadata={"expect_edits": True}
        )
        await self.adapter.edit_message(chat_id, "m", self.reply[: 2 * third])
        await self.adapter.edit_message(
            chat_id, "m", self.reply, finalize=True
        )


class Client:
    def __init__(self, connection, bi) -> None:
        self.connection = connection
        self.send = bi.send()
        self.recv = bi.recv()
        self._buffer = b""

    @classmethod
    async def dial(cls, endpoint, addr) -> "Client":
        connection = await endpoint.connect(addr, transport.ALPN)
        bi = await connection.open_bi()
        return cls(connection, bi)

    async def send_prompt(self, text: str, session_id: str = "default") -> None:
        await self.send.write_all(
            json.dumps(
                {"type": "prompt", "session_id": session_id, "text": text}
            ).encode()
            + b"\n"
        )
        await self.send.finish()

    async def frame(self, timeout: float = 10.0) -> dict:
        async def _next() -> dict:
            while b"\n" not in self._buffer:
                chunk = await self.recv.read(65536)
                if not chunk:
                    raise EOFError
                self._buffer += chunk
            line, self._buffer = self._buffer.split(b"\n", 1)
            return json.loads(line)

        return await asyncio.wait_for(_next(), timeout=timeout)

    async def until_terminal(self, timeout: float = 10.0) -> list[dict]:
        frames = []
        while True:
            item = await self.frame(timeout=timeout)
            frames.append(item)
            if item["type"] in ("done", "error"):
                return frames

    async def all_frames(self, timeout: float = 3.0) -> list[dict]:
        frames = []
        try:
            while True:
                frames.append(await self.frame(timeout=timeout))
        except (EOFError, asyncio.TimeoutError):
            return frames

    async def close(self) -> None:
        try:
            self.connection.close(0, b"done")
        except Exception:
            pass


async def build_stack(tmp_path, responder=None, allow_ids=None, **kwargs):
    import iroh

    server_key = transport.NodeKeyStore(path=tmp_path / "server.key")
    client_key = transport.NodeKeyStore(path=tmp_path / "client.key")
    client_secret = client_key.load_or_create()
    client_id = str(client_secret.public())

    allowlist = transport.Allowlist(
        path=tmp_path / "allowlist.json", env_reader=lambda: ""
    )
    allow_ids = {client_id} if allow_ids is None else allow_ids
    if allow_ids:
        (tmp_path / "allowlist.json").write_text(
            json.dumps({"allowed_node_ids": sorted(allow_ids)}),
            encoding="utf-8",
        )

    service = gateway_adapter.ChannelService(
        key_store=server_key, allowlist=allowlist
    )
    adapter_class = gateway_adapter.build_adapter_class(
        service, FakeAdapterBase, "iroh", FakeSendResult, FakeEvent
    )
    adapter = adapter_class(None)

    server = transport.ChannelServer(
        key_store=server_key,
        allowlist=allowlist,
        relay=transport.parse_relay_setting("disabled"),
        bind_addr="127.0.0.1:0",
        preset=iroh.preset_minimal(),
        **kwargs,
    )
    server.set_prompt_handler(
        gateway_adapter.build_prompt_handler(service, adapter, FakeEvent)
    )
    service.server = server
    adapter._server = server
    adapter._mark_connected()
    if responder is not None:
        adapter.handle_message = responder

    client_endpoint = await iroh.Endpoint.bind(
        iroh.EndpointOptions(
            preset=iroh.preset_minimal(),
            bind_addr="127.0.0.1:0",
            secret_key=client_secret.to_bytes(),
            alpns=[transport.ALPN],
            relay_mode=iroh.RelayMode.disabled(),
        )
    )
    return server, client_endpoint, adapter, service, client_id


def addr_of(server):
    import iroh

    return iroh.EndpointTicket.from_string(server.ticket).endpoint_addr()


def assert_turn_metadata(metadata: dict) -> None:
    assert metadata["schema"] == 1
    assert isinstance(metadata["generated_at"], str)
    assert metadata["generated_at"].endswith("Z")
    assert isinstance(metadata["latency_ms"], int)
    assert metadata["latency_ms"] >= 0


async def test_streamed_turn_end_to_end(tmp_path):
    reply = "The answer is forty-two, computed in three streamed parts."
    server, client_endpoint, adapter, service, _client_id = await build_stack(
        tmp_path
    )
    responder = GatewayResponder(adapter, service, reply, streamed=True)
    adapter.handle_message = responder
    client = None
    try:
        await server.start()
        client = await Client.dial(client_endpoint, addr_of(server))
        await client.send_prompt("what is the answer?", session_id="c1")
        frames = await client.until_terminal()

        types = [frame["type"] for frame in frames]
        assert types[0] == "start"
        assert types[-1] == "done"
        assert types.count("done") == 1
        assert types.count("start") == 1
        deltas = [frame["content"] for frame in frames if frame["type"] == "delta"]
        assert "".join(deltas) == reply
        assert all(frame for frame in deltas)
        assert frames[0]["session_id"] == "c1"
        assert_turn_metadata(frames[-1]["metadata"])
        assert responder.turns == 1
        assert len(responder.events) == 1
        event = responder.events[0]
        assert event.text == "what is the answer?"
        assert event.source.chat_id.endswith(":c1")
    finally:
        if client:
            await client.close()
        await server.stop()
        await client_endpoint.close()


async def test_tool_frame_end_to_end(tmp_path):
    query = "heavy lift project cargo vessel voyage analysis"
    progress = '🔍 Searching the web for "heavy lift project cargo vessel voya..."'
    server, client_endpoint, adapter, service, _client_id = await build_stack(
        tmp_path
    )
    agent = type(
        "Agent",
        (),
        {
            "_session_messages": [
                {"role": "user", "content": "research heavy lift"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "web_search",
                                "arguments": json.dumps({"query": query}),
                            },
                        }
                    ],
                },
            ]
        },
    )()

    class Runner:
        def _session_key_for_source(self, source):
            return source.chat_id

        def _resident_agent_for(self, key):
            return agent

    class Responder:
        async def __call__(self, event):
            chat_id = event.source.chat_id
            await adapter.send(chat_id, progress)
            await adapter.send(
                chat_id, "Here is the answer", metadata={"notify": True}
            )

    adapter.gateway_runner = Runner()
    adapter.handle_message = Responder()
    client = None
    try:
        await server.start()
        client = await Client.dial(client_endpoint, addr_of(server))
        await client.send_prompt("research heavy lift", session_id="tool-1")
        frames = await client.until_terminal()

        types = [frame["type"] for frame in frames]
        assert types == ["start", "tool", "delta", "delta", "done"]
        assert types.count("done") == 1
        tool = frames[1]
        assert tool["name"] == "web_search"
        assert tool["arguments"] == {"query": query}
        deltas = [frame["content"] for frame in frames if frame["type"] == "delta"]
        assert "".join(deltas) == progress + "\n\nHere is the answer"
    finally:
        if client:
            await client.close()
        await server.stop()
        await client_endpoint.close()


async def test_non_streamed_turn_end_to_end(tmp_path):
    reply = "A single complete reply."
    server, client_endpoint, adapter, service, _client_id = await build_stack(
        tmp_path
    )
    responder = GatewayResponder(adapter, service, reply, streamed=False)
    adapter.handle_message = responder
    client = None
    try:
        await server.start()
        client = await Client.dial(client_endpoint, addr_of(server))
        await client.send_prompt("hello", session_id="c2")
        frames = await client.until_terminal()

        assert [frame["type"] for frame in frames] == [
            "start",
            "delta",
            "done",
        ]
        assert frames[1]["content"] == reply
        assert_turn_metadata(frames[-1]["metadata"])
    finally:
        if client:
            await client.close()
        await server.stop()
        await client_endpoint.close()


async def test_error_terminal_has_no_metadata(tmp_path):
    server, client_endpoint, adapter, service, _client_id = await build_stack(
        tmp_path
    )

    async def responder(event) -> None:
        await adapter.send(
            event.source.chat_id,
            "partial",
            metadata={"expect_edits": True},
        )
        raise RuntimeError("turn failed")

    adapter.handle_message = responder
    client = None
    try:
        await server.start()
        client = await Client.dial(client_endpoint, addr_of(server))
        await client.send_prompt("boom", session_id="err")
        frames = await client.until_terminal()

        assert frames[0]["type"] == "start"
        assert "".join(
            frame["content"] for frame in frames if frame["type"] == "delta"
        ) == "partial"
        assert frames[-1]["type"] == "error"
        assert frames[-1]["message"] == transport.ERR_INTERNAL
        assert "metadata" not in frames[-1]
        assert not any(frame["type"] == "done" for frame in frames)
    finally:
        if client:
            await client.close()
        await server.stop()
        await client_endpoint.close()


async def test_multiple_segments_end_to_end(tmp_path):
    server, client_endpoint, adapter, service, _client_id = await build_stack(
        tmp_path
    )

    async def responder(event) -> None:
        chat_id = event.source.chat_id
        await adapter.send(chat_id, "first", metadata={"expect_edits": True})
        await adapter.edit_message(chat_id, "m", "first + more")
        await adapter.send(chat_id, "second", metadata={"notify": True})

    adapter.handle_message = responder
    client = None
    try:
        await server.start()
        client = await Client.dial(client_endpoint, addr_of(server))
        await client.send_prompt("go", session_id="seg")
        frames = await client.until_terminal()

        deltas = "".join(
            frame["content"] for frame in frames if frame["type"] == "delta"
        )
        assert deltas == "first + more\n\nsecond"
        assert [f["type"] for f in frames].count("done") == 1
    finally:
        if client:
            await client.close()
        await server.stop()
        await client_endpoint.close()


async def test_unauthorized_node_gets_no_frames_and_allowlisted_still_works(
    tmp_path,
):
    import iroh

    allowed_key = transport.NodeKeyStore(path=tmp_path / "allowed.key")
    allowed_secret = allowed_key.load_or_create()
    allowed_id = str(allowed_secret.public())
    unknown_secret = iroh.SecretKey.generate()

    server, client_endpoint, adapter, service, _client_id = await build_stack(
        tmp_path, allow_ids={allowed_id}
    )
    responder = GatewayResponder(adapter, service, "hello from agent", streamed=False)
    adapter.handle_message = responder
    unknown_endpoint = None
    allowed_client = None
    try:
        await server.start()
        addr = addr_of(server)

        unknown_endpoint = await iroh.Endpoint.bind(
            iroh.EndpointOptions(
                preset=iroh.preset_minimal(),
                bind_addr="127.0.0.1:0",
                secret_key=unknown_secret.to_bytes(),
                alpns=[transport.ALPN],
                relay_mode=iroh.RelayMode.disabled(),
            )
        )
        with pytest.raises(Exception):
            connection = await asyncio.wait_for(
                unknown_endpoint.connect(addr, transport.ALPN), timeout=10
            )
            bi = await connection.open_bi()
            await bi.send().write_all(b'{"type":"prompt","text":"hi"}\n')
            await bi.send().finish()
            await asyncio.wait_for(bi.recv().read(4096), timeout=5)

        rejection = await server.wait_for_rejection(timeout=5)
        assert rejection is not None
        assert rejection != allowed_id
        assert responder.turns == 0
        assert responder.events == []

        allowed_endpoint = await iroh.Endpoint.bind(
            iroh.EndpointOptions(
                preset=iroh.preset_minimal(),
                bind_addr="127.0.0.1:0",
                secret_key=allowed_secret.to_bytes(),
                alpns=[transport.ALPN],
                relay_mode=iroh.RelayMode.disabled(),
            )
        )
        try:
            allowed_client = await Client.dial(allowed_endpoint, addr)
            await allowed_client.send_prompt("allowed now", session_id="ok")
            frames = await allowed_client.until_terminal()

            assert [f["type"] for f in frames] == ["start", "delta", "done"]
            assert frames[1]["content"] == "hello from agent"
            assert responder.turns == 1
        finally:
            await allowed_endpoint.close()
    finally:
        if allowed_client:
            await allowed_client.close()
        if unknown_endpoint is not None:
            await unknown_endpoint.close()
        await server.stop()
        await client_endpoint.close()


async def test_two_sessions_get_isolated_replies(tmp_path):
    server, client_endpoint, adapter, service, _client_id = await build_stack(
        tmp_path
    )

    async def responder(event) -> None:
        await adapter.send(
            event.source.chat_id,
            f"reply:{event.source.chat_id}",
            metadata={"notify": True},
        )

    adapter.handle_message = responder
    clients = {}
    try:
        await server.start()
        addr = addr_of(server)
        for session_id in ("x", "y"):
            client = await Client.dial(client_endpoint, addr)
            await client.send_prompt("hi", session_id=session_id)
            clients[session_id] = client

        for session_id, client in clients.items():
            frames = await client.until_terminal()
            text = "".join(
                f["content"] for f in frames if f["type"] == "delta"
            )
            assert text.startswith("reply:")
            assert text.endswith(f":{session_id}")
    finally:
        for client in clients.values():
            await client.close()
        await server.stop()
        await client_endpoint.close()

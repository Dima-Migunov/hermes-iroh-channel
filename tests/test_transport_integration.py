"""Loopback integration tests for the Iroh endpoint lifecycle."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transport
from conftest import requires_iroh

pytestmark = requires_iroh


class Client:
    """Minimal protocol client used by the integration tests."""

    def __init__(self, endpoint, connection, bi) -> None:
        self.endpoint = endpoint
        self.connection = connection
        self.send = bi.send()
        self.recv = bi.recv()
        self._buffer = b""

    @classmethod
    async def dial(cls, endpoint, addr, alpn: bytes = transport.ALPN) -> "Client":
        connection = await endpoint.connect(addr, alpn)
        bi = await connection.open_bi()
        return cls(endpoint, connection, bi)

    async def send_prompt(self, text: str, session_id: str | None = "default") -> None:
        payload = {"type": "prompt", "text": text}
        if session_id is not None:
            payload["session_id"] = session_id
        await self.send.write_all(json.dumps(payload).encode() + b"\n")
        await self.send.finish()

    async def read_frame(self, timeout: float = 10.0) -> dict:
        async def _next() -> dict:
            while b"\n" not in self._buffer:
                chunk = await self.recv.read(65536)
                if not chunk:
                    raise EOFError("stream closed")
                self._buffer += chunk
            line, self._buffer = self._buffer.split(b"\n", 1)
            return json.loads(line)

        return await asyncio.wait_for(_next(), timeout=timeout)

    async def read_frames_until_terminal(self, timeout: float = 10.0) -> list[dict]:
        frames = []
        while True:
            frame = await self.read_frame(timeout=timeout)
            frames.append(frame)
            if frame["type"] in ("done", "error"):
                return frames

    async def read_all_frames(self, timeout: float = 10.0) -> list[dict]:
        frames = []
        try:
            while True:
                frames.append(await self.read_frame(timeout=timeout))
        except (EOFError, asyncio.TimeoutError):
            return frames

    async def close(self) -> None:
        try:
            self.connection.close(0, b"done")
        except Exception:
            pass


async def make_endpoints(tmp_path, allow=None, **server_kwargs):
    import iroh

    server_key = transport.NodeKeyStore(
        path=tmp_path / "server.key",
    )
    client_key = transport.NodeKeyStore(
        path=tmp_path / "client.key",
    )
    server_secret = server_key.load_or_create()
    client_secret = client_key.load_or_create()
    server_id = str(server_secret.public())
    client_id = str(client_secret.public())

    allowlist = transport.Allowlist(
        path=tmp_path / "allowlist.json",
        env_reader=lambda: "",
    )
    if allow is None:
        allow = {client_id}
    if allow:
        (tmp_path / "allowlist.json").write_text(
            json.dumps({"allowed_node_ids": sorted(allow)}), encoding="utf-8"
        )

    server = transport.ChannelServer(
        key_store=server_key,
        allowlist=allowlist,
        relay=transport.parse_relay_setting("disabled"),
        bind_addr="127.0.0.1:0",
        preset=iroh.preset_minimal(),
        **server_kwargs,
    )
    client_endpoint = await iroh.Endpoint.bind(
        iroh.EndpointOptions(
            preset=iroh.preset_minimal(),
            bind_addr="127.0.0.1:0",
            secret_key=client_secret.to_bytes(),
            alpns=[transport.ALPN],
            relay_mode=iroh.RelayMode.disabled(),
        )
    )
    return server, client_endpoint, server_id, client_id


async def test_start_reports_node_id_and_parseable_ticket(tmp_path):
    import iroh

    server, client_endpoint, server_id, _client_id = await make_endpoints(tmp_path)
    try:
        await server.start()

        assert server.node_id == server_id
        assert server.node_id
        assert server.ticket

        parsed = iroh.EndpointTicket.from_string(server.ticket)
        assert str(parsed.endpoint_addr().id()) == server_id
        assert parsed.endpoint_addr().direct_addresses()
    finally:
        await server.stop()
        await client_endpoint.close()


async def test_start_stop_twice_leaves_no_tasks(tmp_path):
    server, client_endpoint, _server_id, client_id = await make_endpoints(tmp_path)
    try:
        await server.start()
        first_ticket = server.ticket
        await server.stop()

        assert server.running is False
        assert server.ticket is None
        assert server._accept_task is None

        await server.start()
        assert server.running is True
        assert server.node_id
        await server.stop()
        await server.stop()

        assert server.running is False
        assert not [t for t in server._stream_tasks if not t.done()]
        assert not [t for t in server._connection_tasks if not t.done()]
        assert first_ticket is not None
        assert client_id
    finally:
        await server.stop()
        await client_endpoint.close()


async def test_stop_then_start_serves_clients_again(tmp_path):
    server, client_endpoint, _server_id, _client_id = await make_endpoints(tmp_path)

    async def handler(session, text):
        await session.append(f"echo:{text}")
        await session.finish()

    server.set_prompt_handler(handler)
    try:
        await server.start()
        addr = _addr(server)
        client = await Client.dial(client_endpoint, addr)
        await client.send_prompt("one")
        frames = await client.read_frames_until_terminal()
        assert frames[-1] == {"type": "done"}
        await client.close()

        await server.stop()
        await server.start()

        client = await Client.dial(client_endpoint, _addr(server))
        await client.send_prompt("two")
        frames = await client.read_frames_until_terminal()
        assert [f["type"] for f in frames] == ["start", "delta", "done"]
        assert frames[1]["content"] == "echo:two"
        await client.close()
    finally:
        await server.stop()
        await client_endpoint.close()


def _addr(server):
    import iroh

    return iroh.EndpointTicket.from_string(server.ticket).endpoint_addr()


async def test_unauthorized_client_is_rejected_before_any_read(tmp_path):
    prompts: list[str] = []

    async def handler(session, text):
        prompts.append(text)
        await session.finish()

    server, client_endpoint, _server_id, _client_id = await make_endpoints(
        tmp_path, allow=set()
    )
    server.set_prompt_handler(handler)
    try:
        await server.start()

        with pytest.raises(Exception):
            connection = await asyncio.wait_for(
                client_endpoint.connect(_addr(server), transport.ALPN), timeout=10
            )
            bi = await connection.open_bi()
            await bi.send().write_all(b'{"type":"prompt","text":"nope"}\n')
            await bi.send().finish()
            await asyncio.wait_for(bi.recv().read(1024), timeout=5)

        rejected = await server.wait_for_rejection(timeout=5)
        assert rejected is not None
        assert prompts == []
    finally:
        await server.stop()
        await client_endpoint.close()


async def test_wrong_alpn_is_refused(tmp_path):
    prompts: list[str] = []

    async def handler(session, text):
        prompts.append(text)
        await session.finish()

    server, client_endpoint, _server_id, client_id = await make_endpoints(tmp_path)
    server.set_prompt_handler(handler)
    try:
        await server.start()

        with pytest.raises(Exception):
            await asyncio.wait_for(
                client_endpoint.connect(_addr(server), b"other/9"), timeout=10
            )

        assert prompts == []
    finally:
        await server.stop()
        await client_endpoint.close()


async def test_allowlisted_client_streams_start_delta_done(tmp_path):
    async def handler(session, text):
        await session.append("hello ")
        await session.append("world")
        await session.finish()

    server, client_endpoint, _server_id, _client_id = await make_endpoints(tmp_path)
    server.set_prompt_handler(handler)
    try:
        await server.start()
        client = await Client.dial(client_endpoint, _addr(server))
        await client.send_prompt("hi", session_id="s1")
        frames = await client.read_frames_until_terminal()

        assert frames[0] == {"type": "start", "session_id": "s1"}
        deltas = [f for f in frames if f["type"] == "delta"]
        assert "".join(f["content"] for f in deltas) == "hello world"
        assert frames[-1] == {"type": "done"}
        assert sum(1 for f in frames if f["type"] == "done") == 1
        await client.close()
    finally:
        await server.stop()
        await client_endpoint.close()


async def test_handler_failure_emits_internal_error_without_done(tmp_path):
    async def handler(session, text):
        await session.append("partial")
        raise RuntimeError("boom")

    server, client_endpoint, _server_id, _client_id = await make_endpoints(tmp_path)
    server.set_prompt_handler(handler)
    try:
        await server.start()
        client = await Client.dial(client_endpoint, _addr(server))
        await client.send_prompt("hi")
        frames = await client.read_frames_until_terminal()

        assert [f["type"] for f in frames] == ["start", "delta", "error"]
        assert frames[-1]["message"] == transport.ERR_INTERNAL
        await client.close()
    finally:
        await server.stop()
        await client_endpoint.close()


async def test_session_busy_rejects_second_prompt(tmp_path):
    release = asyncio.Event()
    prompts: list[str] = []

    async def handler(session, text):
        prompts.append(text)
        await release.wait()
        await session.append("slow")
        await session.finish()

    server, client_endpoint, _server_id, _client_id = await make_endpoints(tmp_path)
    server.set_prompt_handler(handler)
    first = second = None
    try:
        await server.start()
        first = await Client.dial(client_endpoint, _addr(server))
        await first.send_prompt("first", session_id="same")
        assert (await first.read_frame())["type"] == "start"

        second = await Client.dial(client_endpoint, _addr(server))
        await second.send_prompt("second", session_id="same")
        frames = await second.read_frames_until_terminal()

        assert frames == [{"type": "error", "message": transport.ERR_SESSION_BUSY}]

        release.set()
        first_frames = await first.read_frames_until_terminal()
        assert first_frames[-1] == {"type": "done"}
        assert "".join(
            f["content"] for f in first_frames if f["type"] == "delta"
        ) == "slow"
        assert prompts == ["first"]
    finally:
        release.set()
        if first:
            await first.close()
        if second:
            await second.close()
        await server.stop()
        await client_endpoint.close()


async def test_different_sessions_run_concurrently(tmp_path):
    release = asyncio.Event()

    async def handler(session, text):
        await release.wait()
        await session.append(f"done:{session.session_id}")
        await session.finish()

    server, client_endpoint, _server_id, _client_id = await make_endpoints(tmp_path)
    server.set_prompt_handler(handler)
    clients = []
    try:
        await server.start()
        for session_id in ("a", "b"):
            client = await Client.dial(client_endpoint, _addr(server))
            await client.send_prompt("hi", session_id=session_id)
            assert (await client.read_frame())["type"] == "start"
            clients.append((session_id, client))

        release.set()
        for session_id, client in clients:
            frames = await client.read_frames_until_terminal()
            assert "".join(
                f["content"] for f in frames if f["type"] == "delta"
            ) == f"done:{session_id}"
    finally:
        release.set()
        for _session_id, client in clients:
            await client.close()
        await server.stop()
        await client_endpoint.close()


async def test_invalid_prompt_gets_error_without_handler(tmp_path):
    prompts: list[str] = []

    async def handler(session, text):
        prompts.append(text)
        await session.finish()

    server, client_endpoint, _server_id, _client_id = await make_endpoints(tmp_path)
    server.set_prompt_handler(handler)
    try:
        await server.start()
        client = await Client.dial(client_endpoint, _addr(server))
        await client.send.write_all(b'{"type":"delta","content":"x"}\n')
        await client.send.finish()
        frames = await client.read_all_frames()

        assert frames == [{"type": "error", "message": transport.ERR_INVALID_PROMPT}]
        assert prompts == []
        await client.close()
    finally:
        await server.stop()
        await client_endpoint.close()


async def test_oversized_prompt_gets_error(tmp_path):
    async def handler(session, text):
        await session.finish()

    server, client_endpoint, _server_id, _client_id = await make_endpoints(
        tmp_path, max_prompt_bytes=256
    )
    server.set_prompt_handler(handler)
    try:
        await server.start()
        client = await Client.dial(client_endpoint, _addr(server))
        payload = json.dumps({"type": "prompt", "text": "x" * 1024}).encode()
        await client.send.write_all(payload + b"\n")
        await client.send.finish()
        frames = await client.read_all_frames()

        assert frames == [
            {"type": "error", "message": transport.ERR_PROMPT_TOO_LARGE}
        ]
        await client.close()
    finally:
        await server.stop()
        await client_endpoint.close()


async def test_turn_timeout_emits_timeout_error(tmp_path):
    hang = asyncio.Event()

    async def handler(session, text):
        await hang.wait()

    server, client_endpoint, _server_id, _client_id = await make_endpoints(
        tmp_path, turn_timeout=0.3
    )
    server.set_prompt_handler(handler)
    try:
        await server.start()
        client = await Client.dial(client_endpoint, _addr(server))
        await client.send_prompt("hi")
        frames = await client.read_frames_until_terminal(timeout=10)

        assert frames[0]["type"] == "start"
        assert frames[-1] == {"type": "error", "message": transport.ERR_TIMEOUT}
        await client.close()
    finally:
        hang.set()
        await server.stop()
        await client_endpoint.close()


async def test_stream_limit_rejects_extra_streams(tmp_path):
    release = asyncio.Event()

    async def handler(session, text):
        await release.wait()
        await session.append("ok")
        await session.finish()

    server, client_endpoint, _server_id, _client_id = await make_endpoints(
        tmp_path, max_streams_per_connection=1
    )
    server.set_prompt_handler(handler)
    clients = []
    try:
        await server.start()
        addr = _addr(server)
        connection = await client_endpoint.connect(addr, transport.ALPN)
        first_bi = await connection.open_bi()
        first = Client(client_endpoint, connection, first_bi)
        await first.send_prompt("one", session_id="a")
        assert (await first.read_frame())["type"] == "start"

        second_bi = await connection.open_bi()
        second = Client(client_endpoint, connection, second_bi)
        await second.send_prompt("two", session_id="b")
        frames = await second.read_all_frames()

        assert frames == [
            {"type": "error", "message": transport.ERR_TOO_MANY_STREAMS}
        ]

        release.set()
        first_frames = await first.read_frames_until_terminal()
        assert first_frames[-1] == {"type": "done"}
        clients.extend([first, second])
    finally:
        release.set()
        for client in clients:
            await client.close()
        await server.stop()
        await client_endpoint.close()


async def test_closing_one_connection_leaves_another_usable(tmp_path):
    async def handler(session, text):
        await session.append("ok")
        await session.finish()

    server, client_endpoint, _server_id, _client_id = await make_endpoints(tmp_path)
    server.set_prompt_handler(handler)
    try:
        await server.start()
        addr = _addr(server)
        first = await Client.dial(client_endpoint, addr)
        second = await Client.dial(client_endpoint, addr)
        await first.send_prompt("one", session_id="a")
        assert (await first.read_frames_until_terminal())[-1] == {"type": "done"}

        await first.close()

        await second.send_prompt("two", session_id="b")
        frames = await second.read_frames_until_terminal()
        assert frames[-1] == {"type": "done"}
        assert "".join(
            f["content"] for f in frames if f["type"] == "delta"
        ) == "ok"
        await second.close()
    finally:
        await server.stop()
        await client_endpoint.close()


async def test_client_close_mid_response_releases_session(tmp_path):
    started = asyncio.Event()

    async def handler(session, text):
        await session.append("first")
        started.set()
        for _ in range(200):
            if session.cancelled or session.terminal:
                return
            await asyncio.sleep(0.01)
            await session.append("more")

    server, client_endpoint, _server_id, _client_id = await make_endpoints(tmp_path)
    server.set_prompt_handler(handler)
    try:
        await server.start()
        client = await Client.dial(client_endpoint, _addr(server))
        await client.send_prompt("hi", session_id="s")
        await asyncio.wait_for(started.wait(), timeout=10)
        await client.close()

        for _ in range(200):
            if not server._sessions:
                break
            await asyncio.sleep(0.01)
        assert server._sessions == {}
        assert server.running is True
    finally:
        await server.stop()
        await client_endpoint.close()

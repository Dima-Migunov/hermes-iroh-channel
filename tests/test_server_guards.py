"""Server-side revocation, admission, and per-client quota tests."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transport

NODE_A = "a" * 64
NODE_B = "b" * 64


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


class FakeRecv:
    def __init__(self, payload: bytes | None) -> None:
        self.payload = payload
        self.reads = 0

    async def read(self, size_limit: int) -> bytes:
        self.reads += 1
        if self.payload is None:
            await asyncio.Event().wait()
        data, self.payload = self.payload or b"", None
        return data


class FakeBi:
    def __init__(self, payload: bytes | None = None) -> None:
        self._recv = FakeRecv(payload)
        self._send = FakeSend()

    def recv(self) -> FakeRecv:
        return self._recv

    def send(self) -> FakeSend:
        return self._send

    def frames(self) -> list[dict]:
        return self._send.frames()


def prompt_bytes(text: str = "hi", session_id: str = "default") -> bytes:
    return json.dumps(
        {"type": "prompt", "session_id": session_id, "text": text}
    ).encode() + b"\n"


def make_server(
    tmp_path, allow_ids=(NODE_A,), **kwargs
) -> transport.ChannelServer:
    path = tmp_path / "allowlist.json"
    if allow_ids is not None:
        path.write_text(
            json.dumps({"allowed_node_ids": list(allow_ids)}),
            encoding="utf-8",
        )
        path.chmod(0o600)
    return transport.ChannelServer(
        key_store=transport.NodeKeyStore(path=tmp_path / "node.key"),
        allowlist=transport.Allowlist(path=path, env_reader=lambda: ""),
        **kwargs,
    )


async def wait_until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not met in time")


def rewrite_allowlist(path: Path, ids) -> None:
    path.write_text(
        json.dumps({"allowed_node_ids": list(ids)}), encoding="utf-8"
    )
    path.chmod(0o600)


# --------------------------------------------------------------------------- #
# Session ids at the stream boundary
# --------------------------------------------------------------------------- #


async def test_invalid_session_id_is_rejected_without_invoking_the_handler(
    tmp_path,
):
    server = make_server(tmp_path)
    invoked: list[str] = []

    async def handler(session, text):
        invoked.append(text)
        await session.finish()

    server.set_prompt_handler(handler)
    bi = FakeBi(prompt_bytes("hello", session_id="../escape"))

    await server._serve_stream(bi, NODE_A)

    assert bi.frames() == [
        {"type": "error", "message": transport.ERR_INVALID_PROMPT}
    ]
    assert invoked == []
    assert server._sessions == {}
    assert server._node_prompt_times == {}


# --------------------------------------------------------------------------- #
# Revocation on established connections
# --------------------------------------------------------------------------- #


async def test_revoked_node_cannot_open_a_new_stream(tmp_path):
    server = make_server(tmp_path)
    invoked: list[str] = []

    async def handler(session, text):
        invoked.append(text)
        await session.append("ok")
        await session.finish()

    server.set_prompt_handler(handler)
    first = FakeBi(prompt_bytes("one"))
    await server._serve_stream(first, NODE_A)
    assert first.frames()[-1] == {"type": "done"}
    assert invoked == ["one"]

    rewrite_allowlist(tmp_path / "allowlist.json", [])
    revoked = FakeBi(prompt_bytes("two"))
    await server._serve_stream(revoked, NODE_A)

    assert revoked.frames() == [
        {"type": "error", "message": transport.ERR_UNAUTHORIZED}
    ]
    assert revoked._recv.reads == 0
    assert invoked == ["one"]
    assert server._sessions == {}


async def test_in_flight_turn_continues_and_readding_allows_new_streams(
    tmp_path,
):
    server = make_server(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    invoked: list[str] = []

    async def handler(session, text):
        invoked.append(text)
        started.set()
        await release.wait()
        await session.append("finished")
        await session.finish()

    server.set_prompt_handler(handler)
    first = FakeBi(prompt_bytes("slow"))
    first_task = asyncio.create_task(server._serve_stream(first, NODE_A))
    await asyncio.wait_for(started.wait(), timeout=2)

    rewrite_allowlist(tmp_path / "allowlist.json", [])
    blocked = FakeBi(prompt_bytes("blocked"))
    await server._serve_stream(blocked, NODE_A)
    assert blocked.frames() == [
        {"type": "error", "message": transport.ERR_UNAUTHORIZED}
    ]
    assert first_task.done() is False

    release.set()
    await asyncio.wait_for(first_task, timeout=2)
    assert first.frames()[-1] == {"type": "done"}
    assert "".join(
        frame["content"]
        for frame in first.frames()
        if frame["type"] == "delta"
    ) == "finished"

    rewrite_allowlist(tmp_path / "allowlist.json", [NODE_A])
    again = FakeBi(prompt_bytes("again"))
    await server._serve_stream(again, NODE_A)
    assert again.frames()[-1] == {"type": "done"}
    assert invoked == ["slow", "again"]


# --------------------------------------------------------------------------- #
# Admission and acceptance
# --------------------------------------------------------------------------- #


class QueueEndpoint:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()

    async def accept_next(self):
        return await self.queue.get()


class FakeConnection:
    def __init__(self, remote_id: str = NODE_A) -> None:
        self._remote_id = remote_id
        self.closed = False

    def alpn(self) -> bytes:
        return transport.ALPN

    def remote_id(self) -> str:
        return self._remote_id

    def close(self, code: int, reason: bytes) -> None:
        self.closed = True

    async def accept_bi(self):
        await asyncio.Event().wait()


class FakeIncoming:
    def __init__(self, gate: asyncio.Event, connection=None) -> None:
        self.gate = gate
        self.connection = connection or FakeConnection()
        self.closed = False

    async def accept(self):
        await self.gate.wait()
        return self

    async def connect(self):
        return self.connection

    def close(self) -> None:
        self.closed = True


async def test_admission_cap_bounds_handshake_tasks_and_releases_slots(
    tmp_path,
):
    server = make_server(tmp_path, max_pending_handshakes=2)
    endpoint = QueueEndpoint()
    server._endpoint = endpoint
    server._handshake_semaphore = asyncio.Semaphore(2)
    loop_task = asyncio.create_task(server._accept_loop())
    gate = asyncio.Event()
    try:
        endpoint.queue.put_nowait(FakeIncoming(gate))
        endpoint.queue.put_nowait(FakeIncoming(gate))
        await wait_until(lambda: len(server._connection_tasks) == 2)

        refused = FakeIncoming(gate)
        endpoint.queue.put_nowait(refused)
        await wait_until(lambda: refused.closed is True)

        assert server.rejection_count == 1
        assert len(server._connection_tasks) == 2

        gate.set()
        await wait_until(lambda: server._active_connections == 2)

        admitted = FakeIncoming(gate)
        endpoint.queue.put_nowait(admitted)
        await wait_until(lambda: server._active_connections == 3)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        tasks = list(server._connection_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        server._endpoint = None


async def test_accept_loop_retries_transient_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "ACCEPT_BACKOFF_MIN", 0.001)
    server = make_server(tmp_path)
    server._handshake_semaphore = asyncio.Semaphore(1)

    class FlakyEndpoint:
        def __init__(self) -> None:
            self.calls = 0
            self.retried = asyncio.Event()

        async def accept_next(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            self.retried.set()
            await asyncio.Event().wait()

    endpoint = FlakyEndpoint()
    server._endpoint = endpoint
    task = asyncio.create_task(server._accept_loop())
    try:
        await asyncio.wait_for(endpoint.retried.wait(), timeout=2)
        assert endpoint.calls == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server._endpoint = None


async def test_accept_loop_exits_when_the_endpoint_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "ACCEPT_BACKOFF_MIN", 0.01)
    server = make_server(tmp_path)

    class DownEndpoint:
        async def accept_next(self):
            raise RuntimeError("down")

    server._endpoint = DownEndpoint()
    task = asyncio.create_task(server._accept_loop())
    await asyncio.sleep(0.02)
    server._endpoint = None

    await asyncio.wait_for(task, timeout=2)


async def test_rejection_burst_is_throttled_but_counted(tmp_path, caplog):
    server = make_server(tmp_path)

    with caplog.at_level("INFO", logger="transport"):
        for _ in range(10):
            server._log_rejection("test", node_id=NODE_A)

    records = [
        record
        for record in caplog.records
        if "iroh audit: rejected" in record.getMessage()
    ]
    assert len(records) == 1
    assert server.rejection_count == 10

    caplog.clear()
    server._last_rejection_log -= transport.REJECTION_LOG_INTERVAL + 1
    with caplog.at_level("INFO", logger="transport"):
        server._log_rejection("test", node_id=NODE_A)

    records = [
        record
        for record in caplog.records
        if "iroh audit: rejected" in record.getMessage()
    ]
    assert len(records) == 1
    assert "suppressed=9" in records[0].getMessage()


# --------------------------------------------------------------------------- #
# Per-node quotas
# --------------------------------------------------------------------------- #


class QueueConnection:
    def __init__(self, remote_id: str) -> None:
        self._remote_id = remote_id
        self.queue: asyncio.Queue = asyncio.Queue()

    async def accept_bi(self):
        return await self.queue.get()


async def test_per_node_stream_limit_bounds_one_node_only(tmp_path):
    server = make_server(
        tmp_path, allow_ids=(NODE_A, NODE_B), max_streams_per_node=1
    )
    server._endpoint = object()
    connection_a = QueueConnection(NODE_A)
    connection_b = QueueConnection(NODE_B)
    serve_a = asyncio.create_task(
        server._serve_connection(connection_a, NODE_A)
    )
    serve_b = asyncio.create_task(
        server._serve_connection(connection_b, NODE_B)
    )
    try:
        first_a = FakeBi(None)
        connection_a.queue.put_nowait(first_a)
        await wait_until(lambda: server._node_streams.get(NODE_A) == 1)

        second_a = FakeBi(None)
        connection_a.queue.put_nowait(second_a)
        await wait_until(
            lambda: second_a.frames()
            and second_a.frames()[-1]["message"]
            == transport.ERR_TOO_MANY_STREAMS
        )
        assert server._node_streams[NODE_A] == 1

        stream_b = FakeBi(None)
        connection_b.queue.put_nowait(stream_b)
        await wait_until(lambda: server._node_streams.get(NODE_B) == 1)
        assert stream_b.frames() == []
    finally:
        for task in (serve_a, serve_b):
            task.cancel()
        stream_tasks = list(server._stream_tasks)
        for task in stream_tasks:
            task.cancel()
        await asyncio.gather(
            serve_a, serve_b, *stream_tasks, return_exceptions=True
        )
        server._endpoint = None


async def test_per_node_prompt_rate_limit_is_per_node(tmp_path, caplog):
    server = make_server(
        tmp_path, allow_ids=(NODE_A, NODE_B), max_prompts_per_minute=1
    )
    invoked: list[str] = []

    async def handler(session, text):
        invoked.append(text)
        await session.finish()

    server.set_prompt_handler(handler)
    first = FakeBi(prompt_bytes("one"))
    await server._serve_stream(first, NODE_A)
    assert first.frames()[-1] == {"type": "done"}

    with caplog.at_level("INFO", logger="transport"):
        second = FakeBi(prompt_bytes("two"))
        await server._serve_stream(second, NODE_A)

    assert second.frames() == [
        {"type": "error", "message": transport.ERR_RATE_LIMITED}
    ]
    assert invoked == ["one"]
    assert len(server._node_prompt_times[NODE_A]) == 2
    assert any(
        f"reason={transport.ERR_RATE_LIMITED}" in record.getMessage()
        and NODE_A in record.getMessage()
        for record in caplog.records
    )

    other = FakeBi(prompt_bytes("three"))
    await server._serve_stream(other, NODE_B)
    assert other.frames()[-1] == {"type": "done"}
    assert invoked == ["one", "three"]

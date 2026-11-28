"""StreamSession behavior tests."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transport


class FakeSend:
    def __init__(self, fail_after: int | None = None) -> None:
        self.chunks: list[bytes] = []
        self.closed = False
        self.fail_after = fail_after

    async def write_all(self, data: bytes) -> None:
        if self.fail_after is not None and len(self.chunks) >= self.fail_after:
            raise ConnectionError("stream closed")
        self.chunks.append(data)

    async def finish(self) -> None:
        self.closed = True

    def frames(self) -> list[dict]:
        return [json.loads(chunk.decode("utf-8")) for chunk in self.chunks]


def make_session(send: FakeSend | None = None, **kwargs) -> tuple[transport.StreamSession, FakeSend]:
    send = send or FakeSend()
    session = transport.StreamSession(
        send, node_id="n" * 64, session_id="default", chat_id="chat", **kwargs
    )
    return session, send


async def test_start_then_done():
    session, send = make_session()

    await session.start()
    await session.finish()

    assert send.frames() == [
        {"type": "start", "session_id": "default"},
        {"type": "done"},
    ]
    assert send.closed is True
    assert session.terminal is True


async def test_empty_response_sends_start_then_done():
    session, send = make_session()

    await session.start()
    await session.finish()

    assert [frame["type"] for frame in send.frames()] == ["start", "done"]


async def test_append_emits_delta_frames():
    session, send = make_session()
    await session.start()

    await session.append("hello")
    await session.append(" world")

    assert send.frames()[1:] == [
        {"type": "delta", "content": "hello"},
        {"type": "delta", "content": " world"},
    ]
    assert "".join(
        frame["content"] for frame in send.frames() if frame["type"] == "delta"
    ) == "hello world"


async def test_append_chunks_long_text():
    session, send = make_session()
    await session.start()
    text = "a" * (transport.MAX_DELTA_BYTES * 2 + 100)

    await session.append(text)

    deltas = [frame for frame in send.frames() if frame["type"] == "delta"]
    assert len(deltas) == 3
    assert "".join(frame["content"] for frame in deltas) == text
    for frame in deltas:
        assert len(frame["content"].encode("utf-8")) <= transport.MAX_DELTA_BYTES


async def test_empty_append_writes_nothing():
    session, send = make_session()
    await session.start()

    await session.append("")

    assert len(send.frames()) == 1


async def test_tool_frame_precedes_delta_and_done():
    session, send = make_session()
    await session.start()

    assert await session.emit_tool("call_1", "web_search", {"query": "q"}) is True
    await session.append("progress")
    await session.finish()

    assert [frame["type"] for frame in send.frames()] == [
        "start",
        "tool",
        "delta",
        "done",
    ]
    assert send.frames()[1] == {
        "type": "tool",
        "id": "call_1",
        "name": "web_search",
        "arguments": {"query": "q"},
    }
    assert session.tool_frames_sent == 1


async def test_tool_frame_after_terminal_is_dropped():
    session, send = make_session()
    await session.start()
    await session.finish()

    assert await session.emit_tool("call_1", "web_search", {"query": "q"}) is False

    assert [frame["type"] for frame in send.frames()] == ["start", "done"]
    assert session.tool_frames_sent == 0


async def test_tool_frame_does_not_consume_the_text_budget():
    session, send = make_session(max_response_bytes=1)
    await session.start()

    assert await session.emit_tool("call_1", "web_search", {"query": "q"}) is True
    await session.append("x")
    await session.finish()

    assert [frame["type"] for frame in send.frames()] == [
        "start",
        "tool",
        "delta",
        "done",
    ]
    assert session.terminal is True
    assert session.bytes_sent == 1
    assert session.tool_bytes_sent > 0


async def test_tool_frame_with_invalid_arguments_writes_nothing():
    session, send = make_session()
    await session.start()

    assert await session.emit_tool("call_1", "web_search", "not-an-object") is False

    assert [frame["type"] for frame in send.frames()] == ["start"]


async def test_duplicate_terminal_is_suppressed():
    session, send = make_session()
    await session.start()

    await session.finish()
    await session.finish()
    await session.fail("timeout")
    await session.finish()

    assert [frame["type"] for frame in send.frames()] == ["start", "done"]
    assert sum(1 for frame in send.frames() if frame["type"] == "done") == 1
    assert not any(frame["type"] == "error" for frame in send.frames())


async def test_write_after_terminal_is_dropped():
    session, send = make_session()
    await session.start()
    await session.finish()

    await session.append("late")
    await session.fail("timeout")

    assert [frame["type"] for frame in send.frames()] == ["start", "done"]


async def test_error_after_text_sends_one_error_and_no_done():
    session, send = make_session()
    await session.start()
    await session.append("partial")

    await session.fail(transport.ERR_INTERNAL)

    types = [frame["type"] for frame in send.frames()]
    assert types == ["start", "delta", "error"]
    assert send.frames()[-1] == {"type": "error", "message": "internal_error"}
    assert "done" not in types


async def test_cancel_prevents_further_writes():
    session, send = make_session()
    await session.start()

    session.cancel()
    await session.append("dropped")
    await session.finish()

    assert [frame["type"] for frame in send.frames()] == ["start"]
    assert send.closed is False
    assert session.finished.is_set()


async def test_cancel_after_terminal_is_a_noop():
    session, _send = make_session()
    await session.start()
    await session.finish()

    session.cancel()

    assert session.terminal is True
    assert session.cancelled is False


async def test_cancelled_session_cannot_start():
    session, send = make_session()
    session.cancel()

    await session.start()
    await session.append("x")
    await session.finish()

    assert send.chunks == []


async def test_write_failure_marks_session_cancelled():
    session, _send = make_session(FakeSend(fail_after=0))

    await session.start()
    await session.append("x")

    assert session.cancelled is True
    assert session.finished.is_set()


async def test_wait_terminal_times_out():
    session, _send = make_session()
    await session.start()

    assert await session.wait_terminal(timeout=0.01) is False


async def test_wait_terminal_returns_when_finished():
    session, _send = make_session()
    await session.start()

    async def finish_soon() -> None:
        await asyncio.sleep(0)
        await session.finish()

    task = asyncio.create_task(finish_soon())
    assert await session.wait_terminal(timeout=1.0) is True
    await task


async def test_start_is_idempotent():
    session, send = make_session()

    await session.start()
    await session.start()

    assert [frame["type"] for frame in send.frames()] == ["start"]


async def test_session_records_a_monotonic_start_timestamp():
    before = time.monotonic()
    session, _send = make_session()
    after = time.monotonic()

    assert before <= session.started_monotonic <= after


async def test_finish_with_metadata_writes_done_once():
    session, send = make_session()
    await session.start()
    metadata = {"schema": 1, "latency_ms": 5}

    await session.finish(metadata=metadata)
    await session.finish(metadata={"schema": 2})

    assert send.frames() == [
        {"type": "start", "session_id": "default"},
        {"type": "done", "metadata": metadata},
    ]
    assert send.closed is True


async def test_finish_with_empty_metadata_stays_bare():
    session, send = make_session()
    await session.start()

    await session.finish(metadata={})

    assert send.frames()[-1] == {"type": "done"}


async def test_error_terminal_carries_no_metadata():
    session, send = make_session()
    await session.start()

    await session.fail(transport.ERR_INTERNAL)

    assert send.frames()[-1] == {
        "type": "error",
        "message": transport.ERR_INTERNAL,
    }
    assert "metadata" not in send.frames()[-1]


async def test_failure_reserves_terminal_before_async_flush():
    session, send = make_session()
    flushing = asyncio.Event()
    release = asyncio.Event()

    async def before_fail(message):
        flushing.set()
        await release.wait()
        return "tail"

    session.before_fail = before_fail
    await session.start()
    failure = asyncio.create_task(session.fail(transport.ERR_TIMEOUT))
    await flushing.wait()

    finishing = asyncio.create_task(session.finish())
    await session.append("ignored")
    await asyncio.sleep(0)
    assert not finishing.done()
    release.set()
    await failure
    await finishing

    assert send.frames() == [
        {"type": "start", "session_id": "default"},
        {"type": "delta", "content": "tail"},
        {"type": "error", "message": transport.ERR_TIMEOUT},
    ]
    assert send.closed is True


async def test_concurrent_failure_takes_over_after_owner_cancellation():
    session, send = make_session()
    flushing = asyncio.Event()
    release = asyncio.Event()

    async def before_fail(_message):
        flushing.set()
        await release.wait()
        return "tail"

    session.before_fail = before_fail
    await session.start()
    owner = asyncio.create_task(session.fail(transport.ERR_INTERNAL))
    await flushing.wait()
    waiter = asyncio.create_task(session.fail(transport.ERR_TIMEOUT))
    await asyncio.sleep(0)

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    release.set()
    await waiter

    assert send.frames() == [
        {"type": "start", "session_id": "default"},
        {"type": "delta", "content": "tail"},
        {"type": "error", "message": transport.ERR_TIMEOUT},
    ]
    assert send.closed is True
    assert session.terminal is True


@pytest.mark.parametrize("terminal_type", ["done", "error"])
async def test_terminal_write_cancellation_blocks_takeover(terminal_type):
    class BlockingTerminalSend(FakeSend):
        def __init__(self) -> None:
            super().__init__()
            self.terminal_started = asyncio.Event()
            self.release_terminal = asyncio.Event()
            self.blocked = False

        async def write_all(self, data: bytes) -> None:
            frame = json.loads(data.decode("utf-8"))
            await super().write_all(data)
            if frame["type"] == terminal_type and not self.blocked:
                self.blocked = True
                self.terminal_started.set()
                await self.release_terminal.wait()

    send = BlockingTerminalSend()
    session, _send = make_session(send)
    await session.start()
    if terminal_type == "done":
        owner = asyncio.create_task(session.finish())
    else:
        owner = asyncio.create_task(session.fail(transport.ERR_INTERNAL))
    await send.terminal_started.wait()
    waiter = asyncio.create_task(session.fail(transport.ERR_TIMEOUT))
    await asyncio.sleep(0)

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    await asyncio.wait_for(waiter, timeout=1)

    assert [frame["type"] for frame in send.frames()] == ["start", terminal_type]
    assert session.cancelled is True
    assert session.terminal is False
    assert session.finished.is_set()


async def test_failure_flush_waits_for_in_progress_append():
    class BlockingDeltaSend(FakeSend):
        def __init__(self) -> None:
            super().__init__()
            self.delta_started = asyncio.Event()
            self.release_delta = asyncio.Event()
            self.blocked = False

        async def write_all(self, data: bytes) -> None:
            if not self.blocked and len(self.chunks) == 1:
                self.blocked = True
                self.delta_started.set()
                await self.release_delta.wait()
            await super().write_all(data)

    send = BlockingDeltaSend()
    session = transport.StreamSession(
        send,
        node_id="n" * 64,
        session_id="default",
        chat_id="chat",
        max_delta_bytes=2,
    )
    flushing = asyncio.Event()

    async def before_fail(message):
        flushing.set()
        return "tail"

    session.before_fail = before_fail
    await session.start()
    append_task = asyncio.create_task(session.append("abcdef"))
    await send.delta_started.wait()
    failure_task = asyncio.create_task(session.fail(transport.ERR_TIMEOUT))
    await flushing.wait()
    send.release_delta.set()
    await append_task
    await failure_task

    assert send.frames() == [
        {"type": "start", "session_id": "default"},
        {"type": "delta", "content": "ab"},
        {"type": "delta", "content": "ta"},
        {"type": "delta", "content": "il"},
        {"type": "error", "message": transport.ERR_TIMEOUT},
    ]
    assert session.bytes_sent == 6


async def test_failure_flush_budget_accounts_in_progress_append():
    class BlockingDeltaSend(FakeSend):
        def __init__(self) -> None:
            super().__init__()
            self.delta_started = asyncio.Event()
            self.release_delta = asyncio.Event()
            self.blocked = False

        async def write_all(self, data: bytes) -> None:
            if not self.blocked and len(self.chunks) == 1:
                self.blocked = True
                self.delta_started.set()
                await self.release_delta.wait()
            await super().write_all(data)

    send = BlockingDeltaSend()
    session = transport.StreamSession(
        send,
        node_id="n" * 64,
        session_id="default",
        chat_id="chat",
        max_delta_bytes=2,
        max_response_bytes=3,
    )
    flushing = asyncio.Event()

    async def before_fail(message):
        flushing.set()
        return "cd"

    session.before_fail = before_fail
    await session.start()
    append_task = asyncio.create_task(session.append("ab"))
    await send.delta_started.wait()
    failure_task = asyncio.create_task(session.fail(transport.ERR_TIMEOUT))
    await flushing.wait()
    send.release_delta.set()
    await append_task
    await failure_task

    assert send.frames() == [
        {"type": "start", "session_id": "default"},
        {"type": "delta", "content": "ab"},
        {"type": "error", "message": transport.ERR_RESPONSE_TOO_LARGE},
    ]
    assert session.bytes_sent == 2


async def test_response_cap_during_pre_fail_flush_sends_one_error():
    session, send = make_session(max_delta_bytes=2, max_response_bytes=3)

    async def before_fail(message):
        return "bcde"

    session.before_fail = before_fail
    await session.start()
    await session.append("a")

    await session.fail(transport.ERR_INTERNAL)

    assert send.frames() == [
        {"type": "start", "session_id": "default"},
        {"type": "delta", "content": "a"},
        {"type": "delta", "content": "bc"},
        {"type": "error", "message": transport.ERR_RESPONSE_TOO_LARGE},
    ]
    assert send.closed is True
    assert session.bytes_sent == 3


async def test_metadata_does_not_consume_the_response_budget():
    session, send = make_session(max_response_bytes=5)
    await session.start()
    await session.append("abcd")

    await session.finish(metadata={"schema": 1})

    assert [frame["type"] for frame in send.frames()] == [
        "start",
        "delta",
        "done",
    ]
    assert send.frames()[-1]["metadata"] == {"schema": 1}
    assert session.bytes_sent == 4


async def test_response_cap_sends_one_terminal_error_and_stops():
    session, send = make_session(max_response_bytes=5)
    await session.start()

    await session.append("abcd")
    await session.append("ef")
    await session.append("ignored")

    assert send.frames() == [
        {"type": "start", "session_id": "default"},
        {"type": "delta", "content": "abcd"},
        {"type": "error", "message": transport.ERR_RESPONSE_TOO_LARGE},
    ]
    assert session.terminal is True
    assert send.closed is True
    assert session.bytes_sent == 4


async def test_response_cap_counts_utf8_bytes():
    session, send = make_session(max_response_bytes=4)
    await session.start()

    await session.append("é")
    await session.append("é")
    await session.append("é")

    assert [frame["type"] for frame in send.frames()] == [
        "start",
        "delta",
        "delta",
        "error",
    ]
    assert send.frames()[-1]["message"] == transport.ERR_RESPONSE_TOO_LARGE


async def test_response_cap_must_be_positive():
    with pytest.raises(ValueError):
        transport.StreamSession(
            FakeSend(),
            node_id="n" * 64,
            session_id="default",
            chat_id="chat",
            max_response_bytes=0,
        )


async def test_session_tracks_delta_statistics():
    session, _send = make_session()
    await session.start()
    await session.append("abc")
    await session.append("de")

    assert session.deltas_sent == 2
    assert session.bytes_sent == 5

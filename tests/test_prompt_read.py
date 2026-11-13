"""Bounded prompt reading tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transport


class FakeRecv:
    def __init__(self, chunks, delay: float = 0.0) -> None:
        self.chunks = list(chunks)
        self.delay = delay
        self.reads = 0

    async def read(self, size_limit: int) -> bytes:
        self.reads += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        assert len(chunk) <= size_limit
        return chunk


async def test_prompt_in_one_read():
    recv = FakeRecv([b'{"type":"prompt","text":"hi"}\n'])

    raw = await transport.read_prompt_line(recv)

    assert raw == b'{"type":"prompt","text":"hi"}'
    assert recv.reads == 1


async def test_prompt_split_across_reads():
    recv = FakeRecv([b'{"type":"prom', b'pt","text":"a', b'b"}\n'])

    raw = await transport.read_prompt_line(recv)

    assert raw == b'{"type":"prompt","text":"ab"}'
    assert recv.reads == 3


async def test_trailing_bytes_after_newline_are_ignored():
    recv = FakeRecv([b'{"type":"prompt","text":"hi"}\nextra bytes'])

    raw = await transport.read_prompt_line(recv)

    assert raw == b'{"type":"prompt","text":"hi"}'
    assert recv.chunks == []


async def test_oversized_line_is_rejected_before_newline():
    recv = FakeRecv([b"x" * transport.PROMPT_READ_CHUNK for _ in range(20)])

    with pytest.raises(transport.PromptError) as excinfo:
        await transport.read_prompt_line(recv, limit=100)

    assert excinfo.value.code == transport.ERR_PROMPT_TOO_LARGE


async def test_size_limit_applies_within_a_single_chunk():
    recv = FakeRecv([b"x" * 200 + b"\n"])

    with pytest.raises(transport.PromptError) as excinfo:
        await transport.read_prompt_line(recv, limit=100)

    assert excinfo.value.code == transport.ERR_PROMPT_TOO_LARGE


async def test_line_exactly_at_limit_is_accepted():
    recv = FakeRecv([b"x" * 100 + b"\n"])

    raw = await transport.read_prompt_line(recv, limit=100)

    assert raw == b"x" * 100


async def test_empty_stream_is_rejected():
    recv = FakeRecv([])

    with pytest.raises(transport.PromptError) as excinfo:
        await transport.read_prompt_line(recv)

    assert excinfo.value.code == transport.ERR_INVALID_PROMPT


async def test_eof_without_newline_returns_buffered_bytes():
    recv = FakeRecv([b'{"type":"prompt","text":"hi"}'])

    raw = await transport.read_prompt_line(recv)

    assert raw == b'{"type":"prompt","text":"hi"}'


async def test_timeout_is_reported():
    recv = FakeRecv([b"{"], delay=1.0)

    with pytest.raises(transport.PromptError) as excinfo:
        await transport.read_prompt_line(recv, timeout=0.01)

    assert excinfo.value.code == transport.ERR_TIMEOUT


async def test_default_timeout_matches_documented_value():
    assert transport.PROMPT_TIMEOUT == 30.0
    assert transport.MAX_PROMPT_BYTES == 64 * 1024

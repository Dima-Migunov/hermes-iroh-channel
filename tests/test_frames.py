"""Frame codec and prompt parsing tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transport


def test_encode_is_compact_single_line_utf8():
    frame = transport.delta_frame("héllo\nworld")
    line = transport.encode_frame(frame)

    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert b'{"type":"delta","content":"h\\u00e9llo\\nworld"}' not in line
    decoded = json.loads(line.decode("utf-8"))
    assert decoded == {"type": "delta", "content": "héllo\nworld"}


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        (transport.start_frame("default"), b'{"type":"start","session_id":"default"}\n'),
        (transport.delta_frame("hi"), b'{"type":"delta","content":"hi"}\n'),
        (transport.done_frame(), b'{"type":"done"}\n'),
        (transport.error_frame("timeout"), b'{"type":"error","message":"timeout"}\n'),
    ],
)
def test_exact_frame_bytes(frame, expected):
    assert transport.encode_frame(frame) == expected


def test_error_constants_are_stable():
    assert transport.ERR_INVALID_PROMPT == "invalid_prompt"
    assert transport.ERR_PROMPT_TOO_LARGE == "prompt_too_large"
    assert transport.ERR_SESSION_BUSY == "session_busy"
    assert transport.ERR_TIMEOUT == "timeout"
    assert transport.ERR_UNAUTHORIZED == "unauthorized"
    assert transport.ERR_INTERNAL == "internal_error"
    assert transport.ERROR_VALUES == frozenset(
        {
            "invalid_prompt",
            "prompt_too_large",
            "session_busy",
            "timeout",
            "unauthorized",
            "internal_error",
            "too_many_streams",
        }
    )


def test_parse_valid_prompt():
    prompt = transport.parse_prompt(
        b'{"type":"prompt","session_id":"client-1","text":"hello"}\n'
    )

    assert prompt.session_id == "client-1"
    assert prompt.text == "hello"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"type":"prompt","text":"hi"}\n',
        b'{"type":"prompt","session_id":null,"text":"hi"}\n',
        b'{"type":"prompt","session_id":"","text":"hi"}\n',
        b'{"type":"prompt","session_id":"   ","text":"hi"}\n',
    ],
)
def test_session_id_defaults(raw):
    assert transport.parse_prompt(raw).session_id == "default"


def test_missing_trailing_newline_is_accepted():
    assert transport.parse_prompt(b'{"type":"prompt","text":"hi"}').text == "hi"


def test_crlf_is_accepted():
    assert transport.parse_prompt(b'{"type":"prompt","text":"hi"}\r\n').text == "hi"


def test_extra_prompt_bytes_after_first_line_are_not_part_of_the_prompt():
    raw = b'{"type":"prompt","text":"hi"}\n{"type":"prompt","text":"ignored"}\n'
    text = raw.split(b"\n")[0]
    assert transport.parse_prompt(text).text == "hi"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"not json\n", transport.ERR_INVALID_PROMPT),
        (b"[]\n", transport.ERR_INVALID_PROMPT),
        (b'"str"\n', transport.ERR_INVALID_PROMPT),
        (b"null\n", transport.ERR_INVALID_PROMPT),
        (b'{"type":"delta","content":"x"}\n', transport.ERR_INVALID_PROMPT),
        (b'{"text":"hi"}\n', transport.ERR_INVALID_PROMPT),
        (b'{"type":"prompt"}\n', transport.ERR_INVALID_PROMPT),
        (b'{"type":"prompt","text":""}\n', transport.ERR_INVALID_PROMPT),
        (b'{"type":"prompt","text":123}\n', transport.ERR_INVALID_PROMPT),
        (b'{"type":"prompt","text":null}\n', transport.ERR_INVALID_PROMPT),
        (b'{"type":"prompt","text":"hi","session_id":7}\n', transport.ERR_INVALID_PROMPT),
        (b'\xff\xfe\n', transport.ERR_INVALID_PROMPT),
    ],
)
def test_invalid_prompts(raw, code):
    with pytest.raises(transport.PromptError) as excinfo:
        transport.parse_prompt(raw)

    assert excinfo.value.code == code


def test_unknown_fields_are_ignored():
    prompt = transport.parse_prompt(
        b'{"type":"prompt","text":"hi","extra":{"x":1},"session_id":"s"}\n'
    )

    assert prompt.session_id == "s"
    assert prompt.text == "hi"


def test_oversized_prompt_is_rejected():
    payload = json.dumps(
        {"type": "prompt", "text": "x" * (transport.MAX_PROMPT_BYTES + 1)}
    ).encode()

    with pytest.raises(transport.PromptError) as excinfo:
        transport.parse_prompt(payload)

    assert excinfo.value.code == transport.ERR_PROMPT_TOO_LARGE


def test_prompt_at_the_exact_limit_is_accepted():
    body = "x" * 100
    payload = json.dumps({"type": "prompt", "text": body}).encode()
    padded = payload + b" " * (transport.MAX_PROMPT_BYTES - len(payload))

    assert transport.parse_prompt(padded).text == body


def test_chunking_splits_by_utf8_bytes_without_breaking_characters():
    text = "é" * (transport.MAX_DELTA_BYTES // 2 + 10)
    chunks = transport.chunk_text(text, transport.MAX_DELTA_BYTES)

    assert "".join(chunks) == text
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= transport.MAX_DELTA_BYTES


def test_chunking_keeps_short_text_whole():
    assert transport.chunk_text("short") == ["short"]
    assert transport.chunk_text("") == []


def test_chunking_handles_four_byte_characters():
    text = "\U0001f600" * 10
    chunks = transport.chunk_text(text, 16)

    assert "".join(chunks) == text
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= 16


def test_chat_id_composition():
    assert transport.make_chat_id("abc", "default") == "abc:default"
    assert transport.make_chat_id("abc", "s1") == "abc:s1"

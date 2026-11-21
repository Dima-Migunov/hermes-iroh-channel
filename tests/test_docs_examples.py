"""Documentation contract tests: README examples must match the codec."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway_adapter
import transport

README = Path(__file__).resolve().parent.parent / "README.md"


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def json_blocks(text: str) -> list[str]:
    return re.findall(r"```json\n(.*?)```", text, re.S)


def frame_lines(text: str) -> list[str]:
    section = text.split("Documented examples")[1].split("```json")[1]
    block = section.split("```", 1)[0]
    return [line for line in block.strip().splitlines() if line.strip()]


def test_readme_exists_and_documents_alpn():
    text = readme_text()

    assert "iroh-channel/1" in text
    assert transport.ALPN.decode() in text


def test_documented_frame_examples_are_single_line_objects():
    lines = frame_lines(readme_text())

    assert lines
    for line in lines:
        payload = json.loads(line)
        assert isinstance(payload, dict)
        assert "type" in payload


def test_documented_prompt_examples_parse_with_the_codec():
    lines = [line for line in frame_lines(readme_text()) if '"prompt"' in line]

    assert lines
    for line in lines:
        prompt = transport.parse_prompt(line.encode("utf-8") + b"\n")
        assert prompt.text
        assert prompt.session_id


def test_documented_response_examples_match_generated_frames():
    lines = frame_lines(readme_text())
    documented = {json.loads(line)["type"]: line for line in lines}

    assert documented["start"] == transport.encode_frame(
        transport.start_frame("default")
    ).decode().strip()
    assert documented["delta"] == transport.encode_frame(
        transport.delta_frame("Hello! How can I help?")
    ).decode().strip()
    assert documented["done"] == transport.encode_frame(
        transport.done_frame()
    ).decode().strip()
    assert documented["error"] == transport.encode_frame(
        transport.error_frame(transport.ERR_SESSION_BUSY)
    ).decode().strip()


def test_every_documented_error_value_is_implemented():
    text = readme_text()
    section = text.split("### Error values")[1].split("###")[0]
    documented = {
        match
        for match in re.findall(r"\| `([a-z_]+)` \|", section)
    }

    assert documented <= transport.ERROR_VALUES
    required = {
        transport.ERR_INVALID_PROMPT,
        transport.ERR_PROMPT_TOO_LARGE,
        transport.ERR_SESSION_BUSY,
        transport.ERR_TIMEOUT,
        transport.ERR_UNAUTHORIZED,
        transport.ERR_INTERNAL,
    }
    assert required <= documented


def test_documented_limits_match_constants():
    text = readme_text()

    assert f"{transport.MAX_PROMPT_BYTES // 1024} KiB" in text
    assert f"{transport.MAX_DELTA_BYTES // 1024} KiB" in text
    assert f"{int(transport.PROMPT_TIMEOUT)} s" in text
    assert f"{int(transport.DEFAULT_TURN_TIMEOUT)} s" in text
    assert f"| Concurrent connections | {transport.DEFAULT_MAX_CONNECTIONS} |" in text
    assert (
        f"| Streams per connection | {transport.DEFAULT_MAX_STREAMS_PER_CONNECTION} |"
        in text
    )
    assert (
        f"| Total concurrent streams | {transport.DEFAULT_MAX_TOTAL_STREAMS} |" in text
    )
    assert (
        f"| Response metadata per `done` frame | {transport.MAX_METADATA_BYTES} bytes |"
        in text
    )


def _metadata_reference_section(text: str) -> str:
    return text.split("### Response metadata")[1].split("###", 1)[0]


def _metadata_reference_names(text: str) -> set[str]:
    section = _metadata_reference_section(text)
    table = "\n".join(
        line for line in section.splitlines() if line.startswith("|")
    )
    return set(re.findall(r"`([a-z][a-z0-9_]*)`", table))


def _nested_keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            keys |= _nested_keys(item)
    return keys


def _maximal_metadata() -> dict:
    agent = SimpleNamespace(
        session_input_tokens=1,
        session_output_tokens=2,
        session_total_tokens=3,
        session_reasoning_tokens=4,
        session_cache_read_tokens=5,
        session_cache_write_tokens=6,
        session_api_calls=7,
        session_estimated_cost_usd=0.5,
        session_cost_status="estimated",
        session_cost_source="catalog",
        context_compressor=SimpleNamespace(
            last_prompt_tokens=8, context_length=100
        ),
        _credits_state={
            "provider": "nous",
            "remaining_micros": 1_000_000,
            "subscription_micros": 2_000_000,
            "subscription_limit_micros": 3_000_000,
            "purchased_micros": 4_000_000,
            "paid_access": True,
            "as_of_ms": 1,
            "plan_gauge": {
                "id": "gauge",
                "label": "Gauge",
                "used_bp": 5,
                "resets_at_ms": 2,
            },
        },
    )
    return gateway_adapter.build_metadata(
        generated_at="2026-11-21T12:00:00.000Z",
        latency_ms=1,
        model_settings={"model": "model", "provider": "provider"},
        turn_usage={
            "input_tokens": 1,
            "output_tokens": 2,
            "total_tokens": 3,
            "reasoning_tokens": 4,
            "cache_read_tokens": 5,
            "cache_write_tokens": 6,
        },
        agent=agent,
    )


def test_documented_metadata_fields_match_the_collector():
    documented = _metadata_reference_names(readme_text())
    collected = _nested_keys(_maximal_metadata())

    assert documented == collected


def test_documented_metadata_omission_behavior():
    section = _metadata_reference_section(readme_text())

    assert "omitted" in section
    assert "null" in section
    assert f"{transport.MAX_METADATA_BYTES} bytes" in section
    assert '{"type":"done"}' in section


def test_documented_environment_variables_exist_in_code():
    text = readme_text()
    documented = set(re.findall(r"HERMES_IROH_[A-Z_]+", text))
    source = (
        (README.parent / "transport.py").read_text(encoding="utf-8")
        + (README.parent / "gateway_adapter.py").read_text(encoding="utf-8")
    )

    assert documented
    for name in documented:
        assert name in source, f"{name} is documented but unused"


def test_documented_slash_commands_match_the_registered_names():
    text = readme_text()
    documented = set(re.findall(r"`/(iroh_[a-z]+)", text))
    source = (README.parent / "gateway_adapter.py").read_text(encoding="utf-8")

    assert documented == {"iroh_info", "iroh_allow", "iroh_revoke"}
    for name in documented:
        assert f'name="{name}"' in source


def test_documented_chat_id_formula_matches_implementation():
    text = readme_text()

    assert 'chat_id = f"{remote_node_id}:{session_id}"' in text
    assert transport.make_chat_id("node", "s") == "node:s"


def test_readme_does_not_promise_attachments():
    text = readme_text().lower()

    assert "no attachments" in text or "attachments are not" in text

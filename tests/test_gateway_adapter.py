"""Gateway adapter tests using fakes; no Hermes installation required."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway_adapter
import transport

NODE_A = "a" * 64
NODE_B = "b" * 64


# --------------------------------------------------------------------------- #
# ResponseRouter
# --------------------------------------------------------------------------- #


def test_prefix_extension_emits_only_the_suffix():
    router = gateway_adapter.ResponseRouter()

    assert router.consume("c", "Hello") == "Hello"
    assert router.consume("c", "Hello world") == " world"
    assert router.consume("c", "Hello world!") == "!"


def test_identical_content_emits_nothing():
    router = gateway_adapter.ResponseRouter()
    router.consume("c", "same")

    assert router.consume("c", "same") == ""


def test_shorter_content_emits_nothing_and_keeps_the_longer_prefix():
    router = gateway_adapter.ResponseRouter()
    router.consume("c", "abcdef")

    assert router.consume("c", "abc") == ""
    assert router.consume("c", "abcdefgh") == "gh"


def test_non_prefix_segment_is_separated_by_a_blank_line():
    router = gateway_adapter.ResponseRouter()
    router.consume("c", "first segment")

    assert router.consume("c", "second segment") == "\n\nsecond segment"
    assert router.consume("c", "second segment!") == "!"


def test_first_content_after_segment_has_no_separator():
    router = gateway_adapter.ResponseRouter()

    assert router.consume("c", "only") == "only"


def test_chats_are_isolated():
    router = gateway_adapter.ResponseRouter()
    router.consume("a", "alpha")

    assert router.consume("b", "beta") == "beta"
    assert router.consume("a", "alphabet") == "bet"


def test_terminal_delivery_is_deduplicated():
    router = gateway_adapter.ResponseRouter()

    assert router.mark_terminal("c") is True
    assert router.mark_terminal("c") is False


def test_consume_after_terminal_emits_nothing():
    router = gateway_adapter.ResponseRouter()
    router.consume("c", "text")
    router.mark_terminal("c")

    assert router.consume("c", "text plus more") == ""


def test_reset_clears_both_state_maps():
    router = gateway_adapter.ResponseRouter()
    router.consume("c", "text")
    router.mark_terminal("c")

    router.reset("c")

    assert router.is_terminal("c") is False
    assert router.consume("c", "fresh") == "fresh"


def test_non_streamed_single_send_emits_full_text():
    router = gateway_adapter.ResponseRouter()
    reply = "one complete reply"

    delta = router.consume("c", reply)

    assert delta == reply
    assert router.mark_terminal("c") is True


# --------------------------------------------------------------------------- #
# Response metadata
# --------------------------------------------------------------------------- #

GENERATED_AT = "2026-11-21T12:00:00.000Z"


def metadata_from(**kwargs) -> dict:
    return gateway_adapter.build_metadata(
        generated_at=GENERATED_AT, latency_ms=812, **kwargs
    )


def assert_done_frame(frame: dict) -> None:
    assert frame["type"] == "done"
    metadata = frame.get("metadata")
    if metadata is not None:
        assert metadata["schema"] == 1
        assert metadata["generated_at"].endswith("Z")
        assert metadata["latency_ms"] >= 0


def test_metadata_without_sources_has_only_the_required_fields():
    assert metadata_from() == {
        "schema": 1,
        "generated_at": GENERATED_AT,
        "latency_ms": 812,
    }


def test_metadata_reads_model_settings_mapping():
    metadata = metadata_from(
        model_settings={"model": "gpt-5.1", "provider": "openai"}
    )

    assert metadata["model"] == "gpt-5.1"
    assert metadata["provider"] == "openai"


def test_metadata_reads_model_settings_attributes_and_provider_objects():
    settings = SimpleNamespace(
        model="claude", provider=SimpleNamespace(name="anthropic")
    )

    metadata = metadata_from(model_settings=settings)

    assert metadata["model"] == "claude"
    assert metadata["provider"] == "anthropic"


def test_metadata_normalizes_turn_usage_and_computes_the_total():
    metadata = metadata_from(
        turn_usage={
            "input_tokens": 1200,
            "output_tokens": 300,
            "reasoning_tokens": 40,
            "cache_read_tokens": 50,
            "cache_write_tokens": 10,
        }
    )

    assert metadata["usage"] == {
        "input_tokens": 1200,
        "output_tokens": 300,
        "total_tokens": 1500,
        "reasoning_tokens": 40,
        "cache_read_tokens": 50,
        "cache_write_tokens": 10,
    }


def test_metadata_accepts_prompt_and_completion_usage_aliases():
    metadata = metadata_from(
        turn_usage={"prompt_tokens": 7, "completion_tokens": 5}
    )

    assert metadata["usage"] == {
        "input_tokens": 7,
        "output_tokens": 5,
        "total_tokens": 12,
    }


def test_metadata_omits_incomplete_turn_usage():
    metadata = metadata_from(turn_usage={"total_tokens": 10})

    assert "usage" not in metadata


def test_metadata_reads_live_agent_counters_cost_and_context():
    agent = SimpleNamespace(
        session_input_tokens=10200,
        session_output_tokens=2400,
        session_total_tokens=12600,
        session_api_calls=8,
        session_estimated_cost_usd=0.42,
        session_cost_status="estimated",
        session_cost_source="model_catalog",
        context_compressor=SimpleNamespace(
            last_prompt_tokens=8400, context_length=128000
        ),
    )

    metadata = metadata_from(agent=agent)

    assert metadata["session_usage"] == {
        "input_tokens": 10200,
        "output_tokens": 2400,
        "total_tokens": 12600,
        "api_calls": 8,
        "estimated_cost_usd": 0.42,
        "cost_status": "estimated",
        "cost_source": "model_catalog",
        "context_tokens": 8400,
        "context_length": 128000,
        "context_percent": 7,
    }


def test_metadata_clamps_context_usage_to_the_window():
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(
            last_prompt_tokens=200000,
            context_length=128000,
        )
    )

    metadata = metadata_from(agent=agent)

    assert metadata["session_usage"] == {
        "context_tokens": 128000,
        "context_length": 128000,
        "context_percent": 100,
    }


def test_metadata_omits_session_usage_without_agent_counters():
    metadata = metadata_from(agent=object())

    assert "session_usage" not in metadata


def test_metadata_reads_balance_from_the_live_credit_state():
    credits = {
        "provider": "nous",
        "remaining_micros": 12_500_000,
        "subscription_micros": 2_000_000,
        "subscription_limit_micros": 10_000_000,
        "purchased_micros": 1_000_000,
        "paid_access": True,
        "as_of_ms": 1763726400000,
        "plan_gauges": [
            {
                "id": "monthly",
                "label": "Monthly",
                "used_bp": 1250,
                "resets_at_ms": 1,
                "binding": False,
            },
            {
                "id": "weekly",
                "label": "Weekly",
                "used_bp": 400,
                "resets_at_ms": 2,
                "binding": True,
            },
        ],
    }
    agent = SimpleNamespace(_credits_state=credits)

    metadata = metadata_from(agent=agent)

    assert metadata["balance"] == {
        "provider": "nous",
        "remaining_micros": 12500000,
        "remaining_usd": 12.5,
        "subscription_micros": 2000000,
        "subscription_usd": 2.0,
        "subscription_limit_usd": 10.0,
        "purchased_micros": 1000000,
        "purchased_usd": 1.0,
        "paid_access": True,
        "as_of_ms": 1763726400000,
        "plan_gauge": {
            "id": "weekly",
            "label": "Weekly",
            "used_bp": 400,
            "resets_at_ms": 2,
        },
    }


def test_metadata_prefers_get_credits_state_over_the_private_attribute():
    agent = SimpleNamespace(
        get_credits_state=lambda: {"remaining_micros": 5_000_000},
        _credits_state={"remaining_micros": 1},
    )

    metadata = metadata_from(agent=agent)

    assert metadata["balance"]["remaining_micros"] == 5_000_000
    assert metadata["balance"]["remaining_usd"] == 5.0


def test_metadata_uses_the_turn_provider_when_credits_omit_it():
    agent = SimpleNamespace(_credits_state={"as_of_ms": 1763726400000})

    metadata = metadata_from(
        model_settings={"provider": "openai"}, agent=agent
    )

    assert metadata["balance"]["provider"] == "openai"
    assert metadata["balance"]["as_of_ms"] == 1763726400000


def test_metadata_reads_a_hermes_credits_state_and_gauge():
    day = SimpleNamespace(
        id="day",
        scope="all",
        label="Daily",
        used_bp=2500,
        resets_at_ms=4102444800000,
    )
    binding = SimpleNamespace(
        id="month",
        scope="all",
        label="Monthly",
        used_bp=9000,
        resets_at_ms=4102444800000,
        full=False,
    )
    credits = SimpleNamespace(
        remaining_micros=12_500_000,
        subscription_micros=2_000_000,
        subscription_limit_micros=10_000_000,
        purchased_micros=1_000_000,
        paid_access=True,
        as_of_ms=1763726400000,
        has_data=True,
        from_header=False,
        gauges=(day, binding),
        binding_gauge=binding,
    )

    metadata = metadata_from(agent=SimpleNamespace(_credits_state=credits))

    assert metadata["balance"]["remaining_usd"] == 12.5
    assert metadata["balance"]["subscription_limit_usd"] == 10.0
    assert metadata["balance"]["plan_gauge"] == {
        "id": "day",
        "label": "Daily",
        "used_bp": 2500,
        "resets_at_ms": 4102444800000,
    }


def test_metadata_prefers_a_full_binding_gauge():
    day = SimpleNamespace(
        id="day", scope="all", label="Daily", used_bp=2500
    )
    binding = SimpleNamespace(
        id="month",
        scope="all",
        label="Monthly",
        used_bp=10000,
        resets_at_ms=4102444800000,
        full=True,
    )
    credits = SimpleNamespace(
        remaining_micros=1_000_000,
        has_data=True,
        gauges=(day, binding),
        binding_gauge=binding,
    )

    metadata = metadata_from(agent=SimpleNamespace(_credits_state=credits))

    assert metadata["balance"]["plan_gauge"] == {
        "id": "month",
        "label": "Monthly",
        "used_bp": 10000,
        "resets_at_ms": 4102444800000,
    }


def test_metadata_omits_balance_before_credits_are_captured():
    credits = SimpleNamespace(
        remaining_micros=0,
        subscription_micros=0,
        purchased_micros=0,
        paid_access=True,
        as_of_ms=0,
        has_data=False,
        from_header=False,
    )

    metadata = metadata_from(agent=SimpleNamespace(_credits_state=credits))

    assert "balance" not in metadata


def test_metadata_omits_unavailable_and_invalid_values():
    metadata = metadata_from(
        model_settings={"model": "", "provider": None},
        turn_usage={"input_tokens": None, "output_tokens": "3"},
        agent=SimpleNamespace(
            session_input_tokens=None,
            session_output_tokens="many",
            session_api_calls=False,
            context_compressor=SimpleNamespace(
                last_prompt_tokens=None, context_length=None
            ),
            _credits_state={
                "remaining_micros": None,
                "paid_access": "yes",
            },
        ),
    )

    assert metadata == {
        "schema": 1,
        "generated_at": GENERATED_AT,
        "latency_ms": 812,
    }

    def assert_no_null_values(value) -> None:
        if isinstance(value, dict):
            for item in value.values():
                assert item is not None
                assert_no_null_values(item)

    assert_no_null_values(metadata)


def test_metadata_drops_long_strings_and_non_finite_numbers():
    metadata = metadata_from(
        model_settings={"model": "m" * 300, "provider": "openai"},
        turn_usage={
            "input_tokens": 1,
            "output_tokens": 2,
            "reasoning_tokens": float("inf"),
        },
        agent=SimpleNamespace(session_estimated_cost_usd=float("nan")),
    )

    assert "model" not in metadata
    assert metadata["provider"] == "openai"
    assert metadata["usage"] == {
        "input_tokens": 1,
        "output_tokens": 2,
        "total_tokens": 3,
    }
    assert "session_usage" not in metadata


def test_metadata_drops_url_like_strings():
    metadata = metadata_from(
        model_settings={
            "model": "https://models.example.com/v1",
            "provider": "openai",
        },
        agent=SimpleNamespace(
            session_cost_source="https://gateway.example.com/usage"
        ),
    )

    assert "model" not in metadata
    assert metadata["provider"] == "openai"
    assert "session_usage" not in metadata


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


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


class FakeSource:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class FakeEvent:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class FakeAdapterBase:
    def __init__(self, config, platform) -> None:
        self.config = config
        self.platform = platform
        self.connected = False
        self.submitted: list[FakeEvent] = []

    def _mark_connected(self) -> None:
        self.connected = True

    def _mark_disconnected(self) -> None:
        self.connected = False

    def build_source(self, **kwargs):
        return FakeSource(**kwargs)

    async def handle_message(self, event) -> None:
        self.submitted.append(event)


class FakeSendResult:
    def __init__(self, success: bool, message_id=None, error=None) -> None:
        self.success = success
        self.message_id = message_id
        self.error = error

    def __repr__(self) -> str:
        return f"SendResult({self.success}, {self.message_id!r}, {self.error!r})"


class FakeAllowlist(transport.Allowlist):
    def __init__(self, path=None) -> None:
        super().__init__(path=path, env_reader=lambda: "")


def make_service(tmp_path) -> gateway_adapter.ChannelService:
    return gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(
            path=tmp_path / "node.key", backend=FakeBackend()
        ),
        allowlist=FakeAllowlist(path=tmp_path / "allowlist.json"),
    )


def make_adapter(tmp_path):
    service = make_service(tmp_path)
    adapter_class = gateway_adapter.build_adapter_class(
        service,
        FakeAdapterBase,
        "iroh",
        FakeSendResult,
        FakeEvent,
    )
    return adapter_class(None), service


def allow_node_allowlist(tmp_path) -> transport.Allowlist:
    return transport.Allowlist(
        path=tmp_path / "allowlist.json", env_reader=lambda: NODE_A
    )


def make_session(tmp_path, node_id: str = NODE_A, session_id: str = "default"):
    send = FakeSend()
    session = transport.StreamSession(
        send,
        node_id=node_id,
        session_id=session_id,
        chat_id=transport.make_chat_id(node_id, session_id),
    )
    return session, send


async def register_session(adapter, service, session) -> None:
    service.router.reset(session.chat_id)
    await session.start()
    server = type(
        "_FakeServer",
        (),
        {"session_for": lambda self, chat_id: session if chat_id == session.chat_id else None},
    )()
    adapter._server = server


class FakeRunner:
    def __init__(self, agents: dict) -> None:
        self.agents = agents
        self.keys: list[str] = []

    def _session_key_for_source(self, source) -> str:
        self.keys.append(source.chat_id)
        return source.chat_id

    def _peek_session_state(self, key: str):
        return {"turn_agent": self.agents.get(key)}


class MultiSessionServer:
    def __init__(self, sessions) -> None:
        self.sessions = {
            session.chat_id: session for session in sessions
        }

    def session_for(self, chat_id):
        return self.sessions.get(chat_id)


class ProbeAgent:
    """Records unknown attribute reads and rejects credential-style names."""

    FORBIDDEN = {"api_key", "credentials", "access_token", "provider_client"}

    def __init__(self) -> None:
        self.accessed: set[str] = set()
        self.requests: list[str] = []
        self.session_input_tokens = 12
        self.session_output_tokens = 3
        self.session_total_tokens = 15
        self.session_api_calls = 1
        self.context_compressor = SimpleNamespace(
            last_prompt_tokens=120, context_length=1000
        )
        self._credits_state = {"remaining_micros": 1_000_000}

    def __getattr__(self, name: str):
        self.accessed.add(name)
        raise AttributeError(name)


# --------------------------------------------------------------------------- #
# Tool-call frames
# --------------------------------------------------------------------------- #


def tool_agent(*calls, session_id="s1"):
    messages = [
        {"role": "user", "content": "find it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }
                for call_id, name, arguments in calls
            ],
        },
    ]
    return SimpleNamespace(_session_messages=messages, session_id=session_id)


QUERY = "heavy lift project cargo vessel voyage analysis"
PROGRESS = '🔍 Searching the web for "heavy lift project cargo vessel voya..."'


async def test_tool_frame_carries_the_full_query_before_the_progress_delta(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    adapter.gateway_runner = FakeRunner(
        {session.chat_id: tool_agent(("call_1", "web_search", {"query": QUERY}))}
    )

    await adapter.send(session.chat_id, PROGRESS)

    frames = send.frames()
    assert [frame["type"] for frame in frames] == ["start", "tool", "delta"]
    assert frames[1] == {
        "type": "tool",
        "id": "call_1",
        "name": "web_search",
        "arguments": {"query": QUERY},
    }
    assert frames[2] == {"type": "delta", "content": PROGRESS}


async def test_missing_history_keeps_the_text_stream_unchanged(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    adapter.gateway_runner = FakeRunner({session.chat_id: SimpleNamespace()})

    await adapter.send(session.chat_id, PROGRESS)

    assert [frame["type"] for frame in send.frames()] == ["start", "delta"]
    assert send.frames()[1] == {"type": "delta", "content": PROGRESS}


async def test_no_live_agent_keeps_the_text_stream_unchanged(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    await adapter.send(session.chat_id, PROGRESS)

    assert [frame["type"] for frame in send.frames()] == ["start", "delta"]


async def test_malformed_tool_arguments_are_ignored(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    agent = SimpleNamespace(
        _session_messages=[
            {"role": "user", "content": "find it"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "web_search",
                            "arguments": "{not valid json",
                        },
                    }
                ],
            },
        ]
    )
    adapter.gateway_runner = FakeRunner({session.chat_id: agent})

    await adapter.send(session.chat_id, PROGRESS)

    assert [frame["type"] for frame in send.frames()] == ["start", "delta"]


async def test_secret_shaped_arguments_are_redacted_and_not_logged(
    tmp_path, caplog
):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    adapter.gateway_runner = FakeRunner(
        {
            session.chat_id: tool_agent(
                (
                    "call_1",
                    "web_search",
                    {"query": QUERY, "api_key": "sk-live-secret-value"},
                )
            )
        }
    )
    caplog.set_level(logging.DEBUG, logger="gateway_adapter")

    await adapter.send(session.chat_id, PROGRESS)

    tool = next(frame for frame in send.frames() if frame["type"] == "tool")
    assert tool["arguments"]["query"] == QUERY
    assert tool["arguments"]["api_key"] == "[redacted]"
    assert "sk-live-secret-value" not in caplog.text


async def test_oversized_arguments_are_bounded_and_marked_truncated(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    adapter.gateway_runner = FakeRunner(
        {
            session.chat_id: tool_agent(
                (
                    "call_1",
                    "execute_code",
                    {"code": "x" * (transport.MAX_TOOL_FRAME_BYTES * 2)},
                )
            )
        }
    )
    line = "🐍 Running code from " + ("x" * 30) + "..."

    await adapter.send(session.chat_id, line)

    tool = next(frame for frame in send.frames() if frame["type"] == "tool")
    assert tool["truncated"] is True
    assert len(transport.encode_frame(tool)) <= transport.MAX_TOOL_FRAME_BYTES


async def test_cumulative_progress_edit_does_not_repeat_a_tool_frame(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    first_query = "heavy lift project cargo vessel voyage analysis"
    second_query = "project cargo hold cleaning costs"
    agent = tool_agent(
        ("call_1", "web_search", {"query": first_query}),
        ("call_2", "web_search", {"query": second_query}),
    )
    adapter.gateway_runner = FakeRunner({session.chat_id: agent})
    first_line = '🔍 Searching the web for "heavy lift project cargo vessel voya..."'
    second_line = '🔍 Searching the web for "project cargo hold cleaning costs"'

    await adapter.send(session.chat_id, first_line)
    await adapter.edit_message(
        session.chat_id, "m1", first_line + "\n" + second_line
    )

    frames = send.frames()
    tool_frames = [frame for frame in frames if frame["type"] == "tool"]
    assert [frame["id"] for frame in tool_frames] == ["call_1", "call_2"]
    deltas = [frame["content"] for frame in frames if frame["type"] == "delta"]
    assert "".join(deltas) == first_line + "\n" + second_line


async def test_duplicate_send_does_not_repeat_a_tool_frame(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    adapter.gateway_runner = FakeRunner(
        {session.chat_id: tool_agent(("call_1", "web_search", {"query": QUERY}))}
    )

    await adapter.send(session.chat_id, PROGRESS)
    await adapter.send(session.chat_id, PROGRESS)

    assert [
        frame["id"] for frame in send.frames() if frame["type"] == "tool"
    ] == ["call_1"]
    assert len([frame for frame in send.frames() if frame["type"] == "delta"]) == 1


async def test_pre_tool_call_capture_covers_stale_agent_history(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    agent = SimpleNamespace(session_id="hermes-s1")
    adapter.gateway_runner = FakeRunner({session.chat_id: agent})
    service.remember_tool_call(
        "hermes-s1", "call_1", "web_search", {"query": QUERY}
    )

    await adapter.send(session.chat_id, PROGRESS)

    tool = next(frame for frame in send.frames() if frame["type"] == "tool")
    assert tool["id"] == "call_1"
    assert tool["arguments"] == {"query": QUERY}
    assert service.pending_tool_calls("hermes-s1") == []


async def test_ambiguous_matches_are_skipped(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    adapter.gateway_runner = FakeRunner(
        {
            session.chat_id: tool_agent(
                ("call_1", "web_search", {"query": QUERY}),
                ("call_2", "web_search", {"query": QUERY + " extended"}),
            )
        }
    )

    await adapter.send(session.chat_id, PROGRESS)

    assert [frame["type"] for frame in send.frames()] == ["start", "delta"]


async def test_final_answer_without_a_progress_line_sends_no_tool_frame(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    adapter.gateway_runner = FakeRunner(
        {session.chat_id: tool_agent(("call_1", "web_search", {"query": QUERY}))}
    )

    await adapter.send(
        session.chat_id, "Here is the answer", metadata={"notify": True}
    )

    assert [frame["type"] for frame in send.frames()] == ["start", "delta", "done"]
    assert "tool" not in [frame["type"] for frame in send.frames()]


async def test_concurrent_sessions_announce_only_their_own_tools(tmp_path):
    adapter, service = make_adapter(tmp_path)
    first, first_send = make_session(tmp_path, session_id="first")
    second, second_send = make_session(tmp_path, session_id="second")
    for session in (first, second):
        service.router.reset(session.chat_id)
        await session.start()
    adapter._server = MultiSessionServer([first, second])
    first_query = "first heavy lift voyage analysis"
    second_query = "second port congestion analysis"
    adapter.gateway_runner = FakeRunner(
        {
            first.chat_id: tool_agent(
                ("call_1", "web_search", {"query": first_query})
            ),
            second.chat_id: tool_agent(
                ("call_2", "web_search", {"query": second_query})
            ),
        }
    )

    await adapter.send(
        first.chat_id, '🔍 Searching the web for "first heavy lift voyage a..."'
    )
    await adapter.send(
        second.chat_id,
        '🔍 Searching the web for "second port congestion analy..."',
    )

    first_tool = next(f for f in first_send.frames() if f["type"] == "tool")
    second_tool = next(f for f in second_send.frames() if f["type"] == "tool")
    assert first_tool["arguments"] == {"query": first_query}
    assert second_tool["arguments"] == {"query": second_query}


async def test_malformed_send_metadata_does_not_break_delivery(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    result = await adapter.send(session.chat_id, "hello", metadata="not-a-dict")

    assert result.success is True
    assert [frame["type"] for frame in send.frames()] == ["start", "delta"]
    assert send.frames()[-1] == {"type": "delta", "content": "hello"}


async def test_tool_frame_collection_failure_keeps_the_text_stream(
    tmp_path, monkeypatch
):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    def fail(*args, **kwargs):
        raise RuntimeError("unexpected history shape")

    monkeypatch.setattr(gateway_adapter, "collect_tool_frames", fail)

    await adapter.send(session.chat_id, PROGRESS)

    assert [frame["type"] for frame in send.frames()] == ["start", "delta"]
    assert send.frames()[-1] == {"type": "delta", "content": PROGRESS}


@pytest.mark.parametrize(
    "arguments",
    [
        {"token": "abc", "nested": {"client_secret": "xyz"}},
        {"password": "p", "query": QUERY},
        {"authorization": "Bearer abc", "command": "ls"},
    ],
)
def test_bounded_arguments_redact_secret_shaped_keys(arguments):
    bounded, _truncated = gateway_adapter._bound_tool_arguments(arguments)

    encoded = json.dumps(bounded)
    assert "[redacted]" in encoded
    for key in ("token", "password", "authorization"):
        if key in arguments:
            assert bounded[key] == "[redacted]"
    nested = bounded.get("nested")
    if isinstance(nested, dict) and "client_secret" in nested:
        assert nested["client_secret"] == "[redacted]"


def test_bounded_arguments_drop_unsupported_values():
    bounded, truncated = gateway_adapter._bound_tool_arguments(
        {"query": QUERY, "handle": object()}
    )

    assert bounded == {"query": QUERY, "handle": "[unsupported]"}
    assert truncated is True


def test_bounded_arguments_omit_payload_shaped_keys():
    bounded, truncated = gateway_adapter._bound_tool_arguments(
        {"path": "notes.txt", "content": "secret file body", "query": QUERY}
    )

    assert bounded["path"] == "notes.txt"
    assert bounded["query"] == QUERY
    assert bounded["content"] == "[omitted]"
    assert truncated is True


def test_bounded_arguments_redact_inline_secret_values():
    bounded, truncated = gateway_adapter._bound_tool_arguments(
        {"query": "look up api_key=sk-live-1234567890 and report"}
    )

    encoded = json.dumps(bounded)
    assert "sk-live-1234567890" not in encoded
    assert "[redacted]" in encoded
    assert truncated is True


# --------------------------------------------------------------------------- #
# Adapter connect / disconnect
# --------------------------------------------------------------------------- #


async def test_connect_starts_the_service_and_marks_connected(
    tmp_path, monkeypatch
):
    adapter, service = make_adapter(tmp_path)

    class StubServer:
        running = True
        ticket = "ticket-value"
        relay = None

        def set_prompt_handler(self, handler):
            self.handler = handler

        async def stop(self):
            self.running = False

    stub = StubServer()

    async def fake_start():
        service.server = stub
        return stub

    monkeypatch.setattr(service, "start", fake_start)

    assert await adapter.connect() is True
    assert adapter.connected is True
    assert hasattr(stub, "handler")


async def test_connect_returns_false_when_start_fails(tmp_path, monkeypatch):
    adapter, service = make_adapter(tmp_path)

    async def failing_start():
        raise transport.TransportError("bind failed")

    monkeypatch.setattr(service, "start", failing_start)

    assert await adapter.connect() is False
    assert adapter.connected is False


async def test_connect_failure_logs_class_and_next_action(
    tmp_path, monkeypatch, caplog
):
    adapter, service = make_adapter(tmp_path)

    class BindFailure(Exception):
        pass

    async def failing_start():
        raise BindFailure("bind failed")

    monkeypatch.setattr(service, "start", failing_start)

    with caplog.at_level(logging.ERROR):
        assert await adapter.connect() is False

    message = caplog.text
    assert "BindFailure" in message
    assert "bind failed" in message
    assert "/iroh_info" in message
    assert "gateway.platforms.iroh.enabled=true" in message
    assert "outside" in message
    assert NODE_A not in message
    assert "allowlist" not in message.lower()


async def test_connect_failure_log_names_invalid_relay_entry_without_the_value(
    tmp_path, monkeypatch, caplog
):
    adapter, service = make_adapter(tmp_path)
    bad_value = "ftp://relay.example.com"

    async def failing_start():
        try:
            transport.parse_relay_setting(bad_value)
        except transport.RelayConfigError as exc:
            raise exc from None

    monkeypatch.setattr(service, "start", failing_start)

    with caplog.at_level(logging.ERROR):
        assert await adapter.connect() is False

    assert "RelayConfigError" in caplog.text
    assert "entry 1 must use the http or https scheme" in caplog.text
    assert bad_value not in caplog.text


async def test_disconnect_is_idempotent(tmp_path, monkeypatch):
    adapter, service = make_adapter(tmp_path)
    stops = {"count": 0}

    class StubServer:
        running = True
        ticket = None
        relay = None

        def set_prompt_handler(self, handler):
            pass

    async def fake_start():
        service.server = StubServer()
        return service.server

    async def fake_stop():
        stops["count"] += 1
        service.server = None

    monkeypatch.setattr(service, "start", fake_start)
    monkeypatch.setattr(service, "stop", fake_stop)

    await adapter.connect()
    await adapter.disconnect()
    await adapter.disconnect()

    assert stops["count"] == 2
    assert adapter.connected is False
    assert adapter._server is None


# --------------------------------------------------------------------------- #
# Prompt submission
# --------------------------------------------------------------------------- #


async def test_prompt_submission_builds_one_event_with_session_keying(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path, session_id="conv-1")
    await register_session(adapter, service, session)

    handler = gateway_adapter.build_prompt_handler(
        service, adapter, FakeEvent
    )
    await handler(session, "hello there")

    assert len(adapter.submitted) == 1
    event = adapter.submitted[0]
    assert event.text == "hello there"
    assert event.source.chat_id == f"{NODE_A}:conv-1"
    assert event.source.user_id == NODE_A
    assert event.source.chat_type == "dm"
    assert event.source.user_name == NODE_A[:12]


async def test_prompt_submission_failure_emits_internal_error(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    async def boom(event):
        raise RuntimeError("gateway down")

    adapter.handle_message = boom
    handler = gateway_adapter.build_prompt_handler(service, adapter, FakeEvent)
    await handler(session, "hello")

    assert send.frames()[-1] == {
        "type": "error",
        "message": transport.ERR_INTERNAL,
    }
    assert "metadata" not in send.frames()[-1]
    assert send.closed is True


async def test_turn_timeout_produces_error_frame(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    invoked = asyncio.Event()

    async def hang(event):
        invoked.set()
        await asyncio.Event().wait()

    adapter.handle_message = hang
    session.chat_id = transport.make_chat_id(NODE_A, "default")
    server = transport.ChannelServer(
        key_store=service.key_store,
        allowlist=allow_node_allowlist(tmp_path),
        turn_timeout=0.05,
        prompt_handler=gateway_adapter.build_prompt_handler(
            service, adapter, FakeEvent
        ),
    )

    class FakeRecv:
        async def read(self, size_limit):
            return b'{"type":"prompt","session_id":"default","text":"hi"}\n'

    class FakeBi:
        def __init__(self, send_stream):
            self._send = send_stream

        def recv(self):
            return FakeRecv()

        def send(self):
            return self._send

    await server._serve_stream(FakeBi(send), NODE_A)

    assert invoked.is_set()
    assert send.frames()[-1] == {
        "type": "error",
        "message": transport.ERR_TIMEOUT,
    }
    assert send.closed is True
    assert server._sessions == {}


async def test_session_end_callback_runs_once_after_timeout(tmp_path):
    adapter, service = make_adapter(tmp_path)
    ended: list[transport.StreamSession] = []

    async def hang(event):
        await asyncio.Event().wait()

    adapter.handle_message = hang
    server = transport.ChannelServer(
        key_store=service.key_store,
        allowlist=allow_node_allowlist(tmp_path),
        turn_timeout=0.05,
        prompt_handler=gateway_adapter.build_prompt_handler(
            service, adapter, FakeEvent
        ),
        on_session_end=ended.append,
    )

    class FakeRecv:
        async def read(self, size_limit):
            return b'{"type":"prompt","session_id":"default","text":"hi"}\n'

    class FakeBi:
        def __init__(self, send_stream):
            self._send = send_stream

        def recv(self):
            return FakeRecv()

        def send(self):
            return self._send

    send = FakeSend()
    await server._serve_stream(FakeBi(send), NODE_A)

    assert send.frames()[-1] == {
        "type": "error",
        "message": transport.ERR_TIMEOUT,
    }
    assert len(ended) == 1
    assert ended[0].chat_id == transport.make_chat_id(NODE_A, "default")
    assert ended[0].node_id == NODE_A
    assert server._sessions == {}


async def test_turn_timeout_releases_router_state(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.allowlist = allow_node_allowlist(tmp_path)
    adapter_class = gateway_adapter.build_adapter_class(
        service, FakeAdapterBase, "iroh", FakeSendResult, FakeEvent
    )
    adapter = adapter_class(None)
    chat_id = transport.make_chat_id(NODE_A, "default")
    invoked = asyncio.Event()

    async def partial_then_hang(event):
        service.router.consume(chat_id, "partial answer")
        invoked.set()
        await asyncio.Event().wait()

    adapter.handle_message = partial_then_hang

    async def fake_start(server_self):
        server_self._endpoint = object()

    monkeypatch.setattr(transport.ChannelServer, "start", fake_start)
    server = await service.start()
    server.set_prompt_handler(
        gateway_adapter.build_prompt_handler(service, adapter, FakeEvent)
    )
    server.turn_timeout = 0.05

    class FakeRecv:
        async def read(self, size_limit):
            return b'{"type":"prompt","session_id":"default","text":"hi"}\n'

    class FakeBi:
        def __init__(self, send_stream):
            self._send = send_stream

        def recv(self):
            return FakeRecv()

        def send(self):
            return self._send

    send = FakeSend()
    await server._serve_stream(FakeBi(send), NODE_A)

    assert invoked.is_set()
    assert service.router._last == {}
    assert server._sessions == {}


async def test_newer_session_keeps_router_state(tmp_path):
    service = make_service(tmp_path)
    ending_session, _ending_send = make_session(tmp_path)
    newer_session, _newer_send = make_session(tmp_path)
    assert ending_session.chat_id == newer_session.chat_id

    class StubServer:
        def session_for(self, chat_id):
            if chat_id == newer_session.chat_id:
                return newer_session
            return None

    service.server = StubServer()
    service.router.consume(ending_session.chat_id, "newer turn text")

    service._release_session_state(ending_session)

    assert service.router._last == {
        ending_session.chat_id: "newer turn text"
    }


async def test_response_is_delivered_after_handler_returns(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session_id = "conv-async"
    chat_id = transport.make_chat_id(NODE_A, session_id)
    send = FakeSend()

    server = transport.ChannelServer(
        key_store=service.key_store,
        allowlist=allow_node_allowlist(tmp_path),
        turn_timeout=5.0,
        prompt_handler=gateway_adapter.build_prompt_handler(
            service, adapter, FakeEvent
        ),
    )
    adapter._server = server

    class FakeRecv:
        async def read(self, size_limit):
            return json.dumps(
                {"type": "prompt", "session_id": session_id, "text": "hi"}
            ).encode() + b"\n"

    class FakeBi:
        def recv(self):
            return FakeRecv()

        def send(self):
            return send

    serving = asyncio.create_task(server._serve_stream(FakeBi(), NODE_A))
    for _ in range(100):
        if adapter.submitted:
            break
        await asyncio.sleep(0.01)

    assert adapter.submitted
    assert server.session_for(chat_id) is not None

    result = await adapter.send(chat_id, "reply", metadata={"notify": True})
    assert result.success is True
    await asyncio.wait_for(serving, timeout=2)

    assert [frame["type"] for frame in send.frames()] == ["start", "delta", "done"]
    assert server._sessions == {}


async def test_notice_send_does_not_finalize_the_stream(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session_id = "conv-notice"
    chat_id = transport.make_chat_id(NODE_A, session_id)
    send = FakeSend()

    server = transport.ChannelServer(
        key_store=service.key_store,
        allowlist=allow_node_allowlist(tmp_path),
        turn_timeout=5.0,
        prompt_handler=gateway_adapter.build_prompt_handler(
            service, adapter, FakeEvent
        ),
    )
    adapter._server = server

    class FakeRecv:
        async def read(self, size_limit):
            return json.dumps(
                {"type": "prompt", "session_id": session_id, "text": "hi"}
            ).encode() + b"\n"

    class FakeBi:
        def recv(self):
            return FakeRecv()

        def send(self):
            return send

    serving = asyncio.create_task(server._serve_stream(FakeBi(), NODE_A))
    for _ in range(100):
        if adapter.submitted:
            break
        await asyncio.sleep(0.01)

    await adapter.send(chat_id, "setup notice", metadata={"_interim_send": True})
    assert server.session_for(chat_id) is not None

    await adapter.send(chat_id, "answer", metadata={"notify": True})
    await asyncio.wait_for(serving, timeout=2)

    assert [frame["type"] for frame in send.frames()] == [
        "start",
        "delta",
        "delta",
        "done",
    ]
    assert send.frames()[1]["content"] == "setup notice"
    assert send.frames()[2]["content"] == "\n\nanswer"
    assert server._sessions == {}


async def test_slash_command_reply_finalizes_without_notify(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session_id = "conv-command"
    chat_id = transport.make_chat_id(NODE_A, session_id)
    send = FakeSend()

    server = transport.ChannelServer(
        key_store=service.key_store,
        allowlist=allow_node_allowlist(tmp_path),
        turn_timeout=5.0,
        prompt_handler=gateway_adapter.build_prompt_handler(
            service, adapter, FakeEvent
        ),
    )
    adapter._server = server

    class FakeRecv:
        async def read(self, size_limit):
            return json.dumps(
                {"type": "prompt", "session_id": session_id, "text": "/help"}
            ).encode() + b"\n"

    class FakeBi:
        def recv(self):
            return FakeRecv()

        def send(self):
            return send

    serving = asyncio.create_task(server._serve_stream(FakeBi(), NODE_A))
    for _ in range(100):
        if adapter.submitted:
            break
        await asyncio.sleep(0.01)

    await adapter.send(chat_id, "command reply")
    await asyncio.wait_for(serving, timeout=2)

    assert [frame["type"] for frame in send.frames()] == ["start", "delta", "done"]
    assert server._sessions == {}


# --------------------------------------------------------------------------- #
# send / edit_message mapping
# --------------------------------------------------------------------------- #


async def test_unmarked_progress_send_keeps_the_stream_open(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    progress = await adapter.send(session.chat_id, "searching the web")
    assert progress.success is True
    assert [frame["type"] for frame in send.frames()] == ["start", "delta"]
    assert session.terminal is False

    final = await adapter.send(
        session.chat_id, "the answer", metadata={"notify": True}
    )
    assert final.success is True
    assert [frame["type"] for frame in send.frames()] == [
        "start",
        "delta",
        "delta",
        "done",
    ]
    assert "".join(
        frame["content"] for frame in send.frames() if frame["type"] == "delta"
    ) == "searching the web\n\nthe answer"
    assert session.terminal is True


async def test_streamed_preview_and_final_edit(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    await adapter.send(
        session.chat_id, "Hel", metadata={"expect_edits": True}
    )
    await adapter.edit_message(session.chat_id, "m1", "Hello")
    result = await adapter.edit_message(
        session.chat_id, "m1", "Hello world", finalize=True
    )

    assert result.success is True
    assert [frame["type"] for frame in send.frames()] == [
        "start",
        "delta",
        "delta",
        "delta",
        "done",
    ]
    assert "".join(
        frame["content"] for frame in send.frames() if frame["type"] == "delta"
    ) == "Hello world"
    assert_done_frame(send.frames()[-1])
    assert send.closed is True


async def test_non_streamed_single_send_is_terminal(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    result = await adapter.send(
        session.chat_id, "single reply", metadata={"notify": True}
    )

    assert result.success is True
    frames = send.frames()
    assert [frame["type"] for frame in frames] == [
        "start",
        "delta",
        "done",
    ]
    assert frames[1] == {"type": "delta", "content": "single reply"}
    assert_done_frame(frames[-1])


async def test_fresh_final_send_finishes_with_separator(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    await adapter.send(session.chat_id, "draft", metadata={"expect_edits": True})
    await adapter.send(session.chat_id, "final answer", metadata={"notify": True})

    deltas = [f for f in send.frames() if f["type"] == "delta"]
    assert "".join(f["content"] for f in deltas) == "draft\n\nfinal answer"
    assert_done_frame(send.frames()[-1])
    assert sum(1 for f in send.frames() if f["type"] == "done") == 1


async def test_duplicate_finalization_writes_nothing_more(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    await adapter.send(session.chat_id, "answer", metadata={"notify": True})
    await adapter.edit_message(
        session.chat_id, "m", "answer", finalize=True
    )
    await adapter.send(session.chat_id, "answer", metadata={"notify": True})

    frames = send.frames()
    assert [frame["type"] for frame in frames] == [
        "start",
        "delta",
        "done",
    ]
    assert frames[1] == {"type": "delta", "content": "answer"}
    assert_done_frame(frames[-1])


async def test_final_delivery_carries_response_metadata(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    seen_sources = []

    def model_settings(source):
        seen_sources.append(source)
        return {"model": "gpt-5.1", "provider": "openai"}

    adapter.get_session_model_settings = model_settings

    result = await adapter.send(
        session.chat_id, "answer", metadata={"notify": True}
    )

    assert result.success is True
    done = send.frames()[-1]
    assert done["type"] == "done"
    assert done["metadata"]["schema"] == 1
    assert done["metadata"]["model"] == "gpt-5.1"
    assert done["metadata"]["provider"] == "openai"
    assert done["metadata"]["latency_ms"] >= 0
    assert done["metadata"]["generated_at"].endswith("Z")
    assert len(seen_sources) == 1
    source = seen_sources[0]
    assert source.chat_id == session.chat_id
    assert source.user_id == NODE_A
    assert source.chat_type == "dm"


async def test_empty_reply_still_carries_metadata(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    await adapter.send(session.chat_id, "", metadata={"notify": True})

    assert [frame["type"] for frame in send.frames()] == ["start", "done"]
    assert send.frames()[-1]["metadata"]["schema"] == 1


async def test_interim_and_edit_deliveries_stay_metadata_free(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    await adapter.send(session.chat_id, "progress")
    await adapter.send(
        session.chat_id, "preview", metadata={"expect_edits": True}
    )
    await adapter.edit_message(session.chat_id, "m", "preview more")

    assert send.frames()
    assert all("metadata" not in frame for frame in send.frames())

    await adapter.send(session.chat_id, "final", metadata={"notify": True})

    frames = send.frames()
    assert all("metadata" not in frame for frame in frames[:-1])
    assert frames[-1]["type"] == "done"
    assert frames[-1]["metadata"]["schema"] == 1


async def test_collector_failure_still_produces_a_normal_done(
    tmp_path, monkeypatch
):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    async def boom(adapter_arg, session_arg):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(gateway_adapter, "collect_response_metadata", boom)

    result = await adapter.send(
        session.chat_id, "answer", metadata={"notify": True}
    )

    assert result.success is True
    assert send.frames()[-1] == {"type": "done"}
    assert send.closed is True
    assert not any(frame["type"] == "error" for frame in send.frames())


async def test_duplicate_finalization_collects_metadata_once(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    calls = []

    def model_settings(source):
        calls.append(source)
        return {"model": "gpt-5.1"}

    adapter.get_session_model_settings = model_settings

    await adapter.send(session.chat_id, "answer", metadata={"notify": True})
    await adapter.edit_message(
        session.chat_id, "m", "answer", finalize=True
    )
    await adapter.send(session.chat_id, "answer", metadata={"notify": True})

    assert len(calls) == 1
    assert sum(1 for frame in send.frames() if frame["type"] == "done") == 1


def test_turn_agent_prefers_the_turn_agent_over_the_cached_agent():
    turn_agent = SimpleNamespace(session_input_tokens=1)
    cached_agent = SimpleNamespace(session_input_tokens=2)
    state = SimpleNamespace(turn=SimpleNamespace(agent=turn_agent))

    assert gateway_adapter._turn_agent(state) is turn_agent
    assert gateway_adapter._turn_agent(
        {"turn_agent": turn_agent, "cached_agent": cached_agent}
    ) is turn_agent
    assert gateway_adapter._turn_agent(
        {"cached_agent": cached_agent}
    ) is cached_agent
    assert gateway_adapter._turn_agent(object()) is None


async def test_metadata_reads_context_from_the_resident_agent(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(
            last_prompt_tokens=8400,
            context_length=128000,
        )
    )

    class Runner:
        def _session_key_for_source(self, source):
            return source.chat_id

        def _resident_agent_for(self, key):
            return agent

    adapter.gateway_runner = Runner()

    await adapter.send(session.chat_id, "answer", metadata={"notify": True})

    session_usage = send.frames()[-1]["metadata"]["session_usage"]
    assert session_usage["context_tokens"] == 8400
    assert session_usage["context_length"] == 128000
    assert session_usage["context_percent"] == 7


async def test_metadata_reads_context_from_a_peeked_session_state(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(
            last_prompt_tokens=3200,
            context_length=12800,
        )
    )

    class Runner:
        def _session_key_for_source(self, source):
            return source.chat_id

        def _peek_session_state(self, key):
            return SimpleNamespace(turn=SimpleNamespace(agent=agent))

    adapter.gateway_runner = Runner()

    await adapter.send(session.chat_id, "answer", metadata={"notify": True})

    session_usage = send.frames()[-1]["metadata"]["session_usage"]
    assert session_usage["context_tokens"] == 3200
    assert session_usage["context_length"] == 12800
    assert session_usage["context_percent"] == 25


async def test_metadata_falls_back_to_the_cached_agent(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(
            last_prompt_tokens=1000,
            context_length=4000,
        )
    )

    class Runner:
        def _session_key_for_source(self, source):
            return source.chat_id

        def _peek_session_state(self, key):
            return None

        def _cached_agent_for(self, key):
            return agent

    adapter.gateway_runner = Runner()

    await adapter.send(session.chat_id, "answer", metadata={"notify": True})

    session_usage = send.frames()[-1]["metadata"]["session_usage"]
    assert session_usage["context_percent"] == 25


async def test_concurrent_sessions_get_isolated_metadata(tmp_path):
    adapter, service = make_adapter(tmp_path)
    first, first_send = make_session(tmp_path, session_id="first")
    second, second_send = make_session(tmp_path, session_id="second")
    for session in (first, second):
        service.router.reset(session.chat_id)
        await session.start()
    adapter._server = MultiSessionServer([first, second])

    adapter.gateway_runner = FakeRunner(
        {
            first.chat_id: SimpleNamespace(
                session_input_tokens=10,
                session_output_tokens=5,
                session_total_tokens=15,
                session_api_calls=1,
            ),
            second.chat_id: SimpleNamespace(
                session_input_tokens=100,
                session_output_tokens=50,
                session_total_tokens=150,
                session_api_calls=2,
            ),
        }
    )
    models = {
        first.chat_id: {"model": "model-first"},
        second.chat_id: {"model": "model-second"},
    }
    adapter.get_session_model_settings = lambda source: models[source.chat_id]

    await asyncio.gather(
        adapter.send(first.chat_id, "a", metadata={"notify": True}),
        adapter.send(second.chat_id, "b", metadata={"notify": True}),
    )

    first_metadata = first_send.frames()[-1]["metadata"]
    second_metadata = second_send.frames()[-1]["metadata"]
    assert first_metadata["model"] == "model-first"
    assert first_metadata["session_usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "api_calls": 1,
    }
    assert second_metadata["model"] == "model-second"
    assert second_metadata["session_usage"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "api_calls": 2,
    }


async def test_consecutive_turns_reflect_the_current_counters(tmp_path):
    adapter, service = make_adapter(tmp_path)
    chat_id = transport.make_chat_id(NODE_A, "default")
    runner = FakeRunner(
        {
            chat_id: SimpleNamespace(
                session_input_tokens=10,
                session_output_tokens=5,
                session_total_tokens=15,
                session_api_calls=1,
            )
        }
    )
    adapter.gateway_runner = runner

    first, first_send = make_session(tmp_path)
    await register_session(adapter, service, first)
    await adapter.send(chat_id, "one", metadata={"notify": True})

    runner.agents[chat_id] = SimpleNamespace(
        session_input_tokens=25,
        session_output_tokens=10,
        session_total_tokens=35,
        session_api_calls=2,
    )
    second, second_send = make_session(tmp_path)
    await register_session(adapter, service, second)
    await adapter.send(chat_id, "two", metadata={"notify": True})

    assert first_send.frames()[-1]["metadata"]["session_usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "api_calls": 1,
    }
    assert second_send.frames()[-1]["metadata"]["session_usage"] == {
        "input_tokens": 25,
        "output_tokens": 10,
        "total_tokens": 35,
        "api_calls": 2,
    }


async def test_metadata_reads_turn_usage_from_the_gateway_execution_result(
    tmp_path, monkeypatch
):
    import contextvars
    import types

    gateway_module = types.ModuleType("gateway")
    session_results = types.ModuleType("gateway.session_results")
    execution_result = contextvars.ContextVar("execution_result")
    execution_result.set(
        {"usage": {"input_tokens": 11, "output_tokens": 4}}
    )
    session_results.execution_result = execution_result
    gateway_module.session_results = session_results
    monkeypatch.setitem(sys.modules, "gateway", gateway_module)
    monkeypatch.setitem(
        sys.modules, "gateway.session_results", session_results
    )

    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    await adapter.send(session.chat_id, "answer", metadata={"notify": True})

    assert send.frames()[-1]["metadata"]["usage"] == {
        "input_tokens": 11,
        "output_tokens": 4,
        "total_tokens": 15,
    }


async def test_metadata_uses_captured_session_stats_when_available(
    tmp_path, monkeypatch
):
    import contextvars
    import types

    gateway_module = types.ModuleType("gateway")
    session_results = types.ModuleType("gateway.session_results")
    execution_result = contextvars.ContextVar("execution_result")
    execution_result.set(
        {
            "usage": {"input_tokens": 20, "output_tokens": 8},
            "session_usage": {
                "input_tokens": 2000,
                "output_tokens": 500,
                "total_tokens": 2500,
                "api_calls": 3,
                "last_prompt_tokens": 2000,
                "context_tokens": 2000,
                "context_length": 8000,
            },
        }
    )
    session_results.execution_result = execution_result
    gateway_module.session_results = session_results
    monkeypatch.setitem(sys.modules, "gateway", gateway_module)
    monkeypatch.setitem(
        sys.modules, "gateway.session_results", session_results
    )

    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    await adapter.send(session.chat_id, "answer", metadata={"notify": True})

    metadata = send.frames()[-1]["metadata"]
    assert metadata["usage"] == {
        "input_tokens": 20,
        "output_tokens": 8,
        "total_tokens": 28,
    }
    assert metadata["session_usage"] == {
        "input_tokens": 2000,
        "output_tokens": 500,
        "total_tokens": 2500,
        "api_calls": 3,
        "context_tokens": 2000,
        "context_length": 8000,
        "context_percent": 25,
    }


async def test_metadata_reads_an_async_model_settings_accessor(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    async def model_settings(source):
        return {"model": "async-model", "provider": "async-provider"}

    adapter.get_session_model_settings = model_settings

    await adapter.send(session.chat_id, "answer", metadata={"notify": True})

    metadata = send.frames()[-1]["metadata"]
    assert metadata["model"] == "async-model"
    assert metadata["provider"] == "async-provider"


async def test_collection_reads_no_credentials_and_makes_no_requests(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)
    agent = ProbeAgent()
    adapter.gateway_runner = FakeRunner({session.chat_id: agent})
    adapter.get_session_model_settings = lambda source: {"model": "model"}

    await adapter.send(session.chat_id, "answer", metadata={"notify": True})

    metadata = send.frames()[-1]["metadata"]
    assert metadata["session_usage"]["input_tokens"] == 12
    assert metadata["balance"]["remaining_micros"] == 1_000_000
    assert agent.requests == []
    assert agent.accessed.isdisjoint(ProbeAgent.FORBIDDEN)


async def test_send_without_active_stream_fails(tmp_path):
    adapter, _service = make_adapter(tmp_path)

    result = await adapter.send("unknown-chat", "text")

    assert result.success is False
    assert result.error == "no active iroh stream"


async def test_edit_message_without_active_stream_fails(tmp_path):
    adapter, _service = make_adapter(tmp_path)

    result = await adapter.edit_message("unknown-chat", "m", "text")

    assert result.success is False
    assert result.error == "no active iroh stream"


async def test_typing_is_a_noop_and_delete_is_unsupported(tmp_path):
    adapter, _service = make_adapter(tmp_path)

    assert await adapter.send_typing(NODE_A) is None
    assert await adapter.delete_message(NODE_A, "m") is False


async def test_get_chat_info_reports_dm(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, _send = make_session(tmp_path)
    await register_session(adapter, service, session)

    info = await adapter.get_chat_info(session.chat_id)

    assert info["type"] == "dm"
    assert info["name"] == NODE_A


async def test_adapter_declares_no_chunking_and_no_async_delivery(tmp_path):
    adapter, _service = make_adapter(tmp_path)

    assert adapter.max_message_length == 0
    assert adapter.supports_async_delivery is False


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def make_command_service(tmp_path) -> gateway_adapter.ChannelService:
    service = make_service(tmp_path)
    return service


async def test_info_reports_node_id_and_unavailable_ticket(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_IROH_STATE_DIR", str(tmp_path))
    service = make_command_service(tmp_path)

    text = await gateway_adapter.handle_info(service)

    assert service.node_id() in text
    assert "ticket: unavailable" in text
    assert transport.ALPN.decode() in text
    assert "allowlist entries: 0" in text
    assert str(tmp_path) in text


async def test_info_reports_ticket_when_running(tmp_path):
    service = make_command_service(tmp_path)

    class StubServer:
        running = True
        ticket = "endpointabc123"
        relay = transport.parse_relay_setting("disabled")

    service.server = StubServer()

    text = await gateway_adapter.handle_info(service)

    assert "ticket: endpointabc123" in text
    assert "relay: disabled" in text


async def test_info_reports_endpoint_stopped_with_next_step_hint(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(transport, "iroh_available", lambda: True)
    service = make_command_service(tmp_path)

    text = await gateway_adapter.handle_info(service)

    assert "endpoint: not running" in text
    assert "ticket: unavailable" in text
    assert "hint:" in text
    assert "gateway.platforms.iroh.enabled=true" in text
    assert "outside" in text
    assert text.splitlines()[-1].startswith("hint: ")


async def test_info_reports_endpoint_running_without_hint(tmp_path):
    service = make_command_service(tmp_path)

    class StubServer:
        running = True
        ticket = "endpointabc123"
        relay = transport.parse_relay_setting("disabled")

    service.server = StubServer()

    text = await gateway_adapter.handle_info(service)

    assert "endpoint: running" in text
    assert "hint:" not in text


async def test_info_sdk_missing_hint_prefers_reinstall(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "iroh_available", lambda: False)
    service = make_command_service(tmp_path)

    text = await gateway_adapter.handle_info(service)

    assert "endpoint: not running" in text
    assert "not importable" in text
    assert "hermes plugins enable iroh-channel" in text
    assert "restart the gateway" not in text


async def test_info_survives_unreadable_key_and_still_reports_facts(
    tmp_path, caplog, monkeypatch
):
    monkeypatch.setattr(transport, "iroh_available", lambda: True)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HERMES_IROH_STATE_DIR", str(state_dir))
    key_path = tmp_path / "node.key"
    key_path.write_bytes(b"short")
    service = gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(path=key_path, backend=FakeBackend()),
        allowlist=FakeAllowlist(path=tmp_path / "allowlist.json"),
    )

    with caplog.at_level(logging.WARNING):
        text = gateway_adapter.format_info(service)

    assert "node id: unavailable" in text
    assert "endpoint: not running" in text
    assert "ticket: unavailable" in text
    assert "alpn: iroh-channel/1" in text
    assert "allowlist entries: 0" in text
    assert f"state dir: {state_dir}" in text.splitlines()
    assert "hint:" in text
    assert "could not be derived" in text
    assert f"state directory {state_dir}" in text
    assert "KeyStoreError" in caplog.text
    assert key_path.read_bytes().hex() not in caplog.text
    assert NODE_A not in caplog.text


async def test_info_reports_relay_from_environment(tmp_path, monkeypatch):
    service = make_command_service(tmp_path)
    monkeypatch.setenv("HERMES_IROH_RELAY", "https://relay.example.com")

    text = await gateway_adapter.handle_info(service)

    assert "relay: https://relay.example.com" in text


async def test_info_redacts_relay_userinfo_and_query(tmp_path, monkeypatch):
    service = make_command_service(tmp_path)
    monkeypatch.setenv(
        "HERMES_IROH_RELAY",
        "https://relay.example.com/path?token=abc",
    )

    text = await gateway_adapter.handle_info(service)

    assert "relay: https://relay.example.com/path" in text
    assert "token" not in text
    assert "?" not in text.split("relay: ", 1)[1].splitlines()[0]


async def test_allow_persists_valid_node_id(tmp_path):
    service = make_command_service(tmp_path)

    text = await gateway_adapter.handle_allow(service, NODE_A)

    assert text == f"allowlisted: {NODE_A}"
    assert service.allowlist.check(NODE_A) is True
    payload = json.loads((tmp_path / "allowlist.json").read_text(encoding="utf-8"))
    assert payload == {"allowed_node_ids": [NODE_A]}


async def test_allow_normalizes_case_and_whitespace(tmp_path):
    service = make_command_service(tmp_path)

    text = await gateway_adapter.handle_allow(service, f"  {NODE_A.upper()}  ")

    assert text == f"allowlisted: {NODE_A}"


async def test_allow_without_argument_returns_usage(tmp_path):
    service = make_command_service(tmp_path)

    text = await gateway_adapter.handle_allow(service, "")

    assert "usage" in text.lower()
    assert NODE_A not in text
    assert not (tmp_path / "allowlist.json").exists()


async def test_allow_with_extra_arguments_returns_usage(tmp_path):
    service = make_command_service(tmp_path)

    text = await gateway_adapter.handle_allow(service, f"{NODE_A} {NODE_B}")

    assert "usage" in text.lower()
    assert not (tmp_path / "allowlist.json").exists()


@pytest.mark.parametrize("value", ["not-a-node-id", "a" * 63, "g" * 64, ""])
async def test_allow_rejects_invalid_node_ids(tmp_path, value):
    service = make_command_service(tmp_path)

    text = await gateway_adapter.handle_allow(service, value)

    assert text.startswith("invalid node id") or "usage" in text.lower()
    assert not (tmp_path / "allowlist.json").exists()


async def test_allow_reports_write_failure_without_raising(tmp_path, monkeypatch):
    service = make_command_service(tmp_path)

    def fail_write(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(transport, "_write_file_atomic", fail_write)

    text = await gateway_adapter.handle_allow(service, NODE_A)

    assert text == "could not update allowlist: PermissionError"
    assert not (tmp_path / "allowlist.json").exists()


async def test_revoke_removes_node_id_and_confirms(tmp_path):
    service = make_command_service(tmp_path)
    await gateway_adapter.handle_allow(service, NODE_A)

    text = await gateway_adapter.handle_revoke(service, NODE_A)

    assert text == f"revoked: {NODE_A}"
    assert service.allowlist.check(NODE_A) is False
    payload = json.loads((tmp_path / "allowlist.json").read_text(encoding="utf-8"))
    assert payload == {"allowed_node_ids": []}


async def test_revoke_normalizes_case_and_whitespace(tmp_path):
    service = make_command_service(tmp_path)
    await gateway_adapter.handle_allow(service, NODE_A)

    text = await gateway_adapter.handle_revoke(service, f"  {NODE_A.upper()}  ")

    assert text == f"revoked: {NODE_A}"
    assert service.allowlist.check(NODE_A) is False


async def test_revoke_without_argument_returns_usage(tmp_path):
    service = make_command_service(tmp_path)

    text = await gateway_adapter.handle_revoke(service, "")

    assert text == "usage: /iroh_revoke <client_node_id>"
    assert not (tmp_path / "allowlist.json").exists()


async def test_revoke_with_extra_arguments_returns_usage(tmp_path):
    service = make_command_service(tmp_path)

    text = await gateway_adapter.handle_revoke(service, f"{NODE_A} {NODE_B}")

    assert text == "usage: /iroh_revoke <client_node_id>"
    assert not (tmp_path / "allowlist.json").exists()


@pytest.mark.parametrize("value", ["not-a-node-id", "a" * 63, "g" * 64])
async def test_revoke_rejects_invalid_node_ids(tmp_path, value):
    service = make_command_service(tmp_path)

    text = await gateway_adapter.handle_revoke(service, value)

    assert text.startswith("invalid node id")
    assert not (tmp_path / "allowlist.json").exists()


async def test_revoke_reports_write_failure_without_raising(
    tmp_path, monkeypatch
):
    service = make_command_service(tmp_path)
    await gateway_adapter.handle_allow(service, NODE_A)

    def fail_write(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(transport, "_write_file_atomic", fail_write)

    text = await gateway_adapter.handle_revoke(service, NODE_A)

    assert text == "could not update allowlist: PermissionError"
    payload = json.loads((tmp_path / "allowlist.json").read_text(encoding="utf-8"))
    assert payload == {"allowed_node_ids": [NODE_A]}


async def test_revoke_of_absent_entry_without_a_file_creates_nothing(tmp_path):
    service = make_command_service(tmp_path)

    text = await gateway_adapter.handle_revoke(service, NODE_A)

    assert text == f"revoked: {NODE_A}"
    assert not (tmp_path / "allowlist.json").exists()


async def test_revoke_reports_environment_authorization(tmp_path):
    service = gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(
            path=tmp_path / "node.key", backend=FakeBackend()
        ),
        allowlist=transport.Allowlist(
            path=tmp_path / "allowlist.json", env_reader=lambda: NODE_A
        ),
    )
    await gateway_adapter.handle_allow(service, NODE_A)

    text = await gateway_adapter.handle_revoke(service, NODE_A)

    assert text == (
        f"still authorized: {NODE_A} (HERMES_IROH_ALLOWLIST)"
    )
    assert service.allowlist.check(NODE_A) is True
    payload = json.loads((tmp_path / "allowlist.json").read_text(encoding="utf-8"))
    assert payload == {"allowed_node_ids": []}


# --------------------------------------------------------------------------- #
# register()
# --------------------------------------------------------------------------- #


# Hermes resolves a user-installed platform name through its platform registry,
# so `Platform("iroh")` raises until the plugin context has registered the name.
# The fakes model that ordering; a permissive Platform would hide the bug.
_FAKE_PLATFORM_NAMES: set[str] = set()


class FakePlatform:
    def __init__(self, name: str) -> None:
        if name not in _FAKE_PLATFORM_NAMES:
            raise ValueError(f"'{name}' is not a valid Platform")
        self.name = name


class FakeContext:
    def __init__(self) -> None:
        self.platforms: list[dict] = []
        self.commands: list[dict] = []
        self.hooks: list[tuple[str, object]] = []

    def register_platform(self, **kwargs) -> None:
        _FAKE_PLATFORM_NAMES.add(kwargs["name"])
        self.platforms.append(kwargs)

    def register_command(self, **kwargs) -> None:
        self.commands.append(kwargs)

    def register_hook(self, name, callback) -> None:
        self.hooks.append((name, callback))


@pytest.fixture
def fake_hermes(monkeypatch):
    """Install minimal fake Hermes modules so register() can be exercised."""
    import dataclasses

    @dataclasses.dataclass
    class PlatformEntry:
        name: str = ""
        label: str = ""
        trusted_inbound: bool = False

    modules = {
        "gateway": type(sys)("gateway"),
        "gateway.config": type(sys)("gateway.config"),
        "gateway.platforms": type(sys)("gateway.platforms"),
        "gateway.platforms.base": type(sys)("gateway.platforms.base"),
        "gateway.platforms.event": type(sys)("gateway.platforms.event"),
        "gateway.platform_registry": type(sys)("gateway.platform_registry"),
    }
    modules["gateway.config"].Platform = FakePlatform
    modules["gateway.platforms.base"].BasePlatformAdapter = FakeAdapterBase
    modules["gateway.platforms.base"].SendResult = FakeSendResult
    modules["gateway.platforms.event"].MessageEvent = FakeEvent
    modules["gateway.platform_registry"].PlatformEntry = PlatformEntry
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    gateway_adapter.ChannelService.reset()
    _FAKE_PLATFORM_NAMES.clear()
    try:
        yield modules
    finally:
        _FAKE_PLATFORM_NAMES.clear()


def test_register_warns_once_when_iroh_sdk_is_missing(
    fake_hermes, monkeypatch, caplog
):
    monkeypatch.setattr(transport, "iroh_available", lambda: False)
    ctx = FakeContext()

    with caplog.at_level(logging.WARNING):
        gateway_adapter.register(ctx)

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == gateway_adapter.__name__
        and "iroh SDK is not importable" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "hermes plugins enable iroh-channel" in warnings[0]
    assert "iroh>=1.2,<2" in warnings[0]
    gateway_adapter.ChannelService.reset()


def test_register_is_quiet_when_iroh_sdk_is_available(
    fake_hermes, monkeypatch, caplog
):
    monkeypatch.setattr(transport, "iroh_available", lambda: True)
    ctx = FakeContext()

    with caplog.at_level(logging.WARNING):
        gateway_adapter.register(ctx)

    assert not any(
        "iroh SDK is not importable" in record.getMessage()
        for record in caplog.records
    )
    gateway_adapter.ChannelService.reset()


def test_register_records_one_platform(fake_hermes):
    ctx = FakeContext()

    gateway_adapter.register(ctx)

    assert len(ctx.platforms) == 1
    registration = ctx.platforms[0]
    assert registration["name"] == "iroh"
    assert registration["label"] == gateway_adapter.PLATFORM_LABEL
    assert callable(registration["adapter_factory"])
    assert callable(registration["check_fn"])
    assert callable(registration["validate_config"])
    assert registration["max_message_length"] == 0
    assert "platform_hint" in registration
    gateway_adapter.ChannelService.reset()


def test_register_captures_full_tool_args_for_later_progress(fake_hermes):
    ctx = FakeContext()
    gateway_adapter.register(ctx)
    hooks = [item for item in ctx.hooks if item[0] == "pre_tool_call"]
    assert len(hooks) == 1

    hooks[0][1](
        tool_name="web_search",
        args={"query": QUERY, "api_key": "sk-secret"},
        session_id="hermes-s1",
        tool_call_id="call_1",
    )

    pending = gateway_adapter.ChannelService.get().pending_tool_calls("hermes-s1")
    assert pending[0]["id"] == "call_1"
    assert pending[0]["name"] == "web_search"
    assert pending[0]["arguments"]["query"] == QUERY
    gateway_adapter.ChannelService.reset()


def test_register_then_adapter_factory_constructs_platform(fake_hermes):
    ctx = FakeContext()

    gateway_adapter.register(ctx)

    factory = ctx.platforms[0]["adapter_factory"]
    adapter = factory(None)

    assert isinstance(adapter, FakeAdapterBase)
    assert adapter.platform.name == "iroh"
    gateway_adapter.ChannelService.reset()


def test_register_feature_detects_trusted_inbound(fake_hermes):
    ctx = FakeContext()

    gateway_adapter.register(ctx)

    assert ctx.platforms[0]["trusted_inbound"] is True
    gateway_adapter.ChannelService.reset()


def test_register_omits_trusted_inbound_when_unsupported(fake_hermes):
    fake_hermes["gateway.platform_registry"].PlatformEntry = type(
        "PlatformEntry",
        (),
        {"__dataclass_fields__": {"name": None, "label": None}},
    )
    ctx = FakeContext()

    gateway_adapter.register(ctx)

    assert "trusted_inbound" not in ctx.platforms[0]
    gateway_adapter.ChannelService.reset()


def test_register_registers_all_commands(fake_hermes):
    ctx = FakeContext()

    gateway_adapter.register(ctx)

    names = {command["name"] for command in ctx.commands}
    assert names == {"iroh_info", "iroh_allow", "iroh_revoke"}
    for command in ctx.commands:
        assert callable(command["handler"])
        assert command["description"]
    gateway_adapter.ChannelService.reset()


def test_register_omits_install_hint_when_unsupported(fake_hermes):
    fake_hermes["gateway.platform_registry"].PlatformEntry = type(
        "PlatformEntry",
        (),
        {"__dataclass_fields__": {"name": None, "label": None}},
    )
    ctx = FakeContext()

    gateway_adapter.register(ctx)

    assert "install_hint" not in ctx.platforms[0]
    gateway_adapter.ChannelService.reset()


async def test_registered_command_handlers_are_awaitable(fake_hermes, tmp_path):
    ctx = FakeContext()
    gateway_adapter.register(ctx)
    service = gateway_adapter.ChannelService.get()
    service.key_store = transport.NodeKeyStore(
        path=tmp_path / "node.key", backend=FakeBackend()
    )
    service.allowlist = FakeAllowlist(path=tmp_path / "allowlist.json")
    handlers = {command["name"]: command["handler"] for command in ctx.commands}

    info = await handlers["iroh_info"]("")
    allowed = await handlers["iroh_allow"](NODE_A)
    allow_usage = await handlers["iroh_allow"]("")
    revoked = await handlers["iroh_revoke"](NODE_A)
    revoke_usage = await handlers["iroh_revoke"]("")

    assert service.node_id() in info
    assert allowed == f"allowlisted: {NODE_A}"
    assert "usage" in allow_usage.lower()
    assert revoked == f"revoked: {NODE_A}"
    assert "usage" in revoke_usage.lower()
    gateway_adapter.ChannelService.reset()


def test_check_requirements_reflects_iroh_availability(monkeypatch):
    monkeypatch.setattr(transport, "iroh_available", lambda: False)
    assert gateway_adapter._check_requirements() is False
    monkeypatch.setattr(transport, "iroh_available", lambda: True)
    assert gateway_adapter._check_requirements() is True


def test_module_imports_without_hermes():
    source = Path(gateway_adapter.__file__).read_text(encoding="utf-8")
    top_level = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "transport" not in line
    ]
    assert all("gateway" not in line for line in top_level)

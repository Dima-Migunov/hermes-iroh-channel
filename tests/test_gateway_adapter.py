"""Gateway adapter tests using fakes; no Hermes installation required."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path

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


async def test_connect_failure_log_names_invalid_relay_value(
    tmp_path, monkeypatch, caplog
):
    adapter, service = make_adapter(tmp_path)
    bad_value = "ftp://relay.example.com"

    async def failing_start():
        raise transport.RelayConfigError(
            f"unsupported relay URL scheme: {bad_value!r}"
        )

    monkeypatch.setattr(service, "start", failing_start)

    with caplog.at_level(logging.ERROR):
        assert await adapter.connect() is False

    assert "RelayConfigError" in caplog.text
    assert bad_value in caplog.text


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
        allowlist=service.allowlist,
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


async def test_response_is_delivered_after_handler_returns(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session_id = "conv-async"
    chat_id = transport.make_chat_id(NODE_A, session_id)
    send = FakeSend()

    server = transport.ChannelServer(
        key_store=service.key_store,
        allowlist=service.allowlist,
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
        allowlist=service.allowlist,
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

    await adapter.send(chat_id, "setup notice")
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
        allowlist=service.allowlist,
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
    assert send.frames()[-1] == {"type": "done"}
    assert send.closed is True


async def test_non_streamed_single_send_is_terminal(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    result = await adapter.send(
        session.chat_id, "single reply", metadata={"notify": True}
    )

    assert result.success is True
    assert send.frames() == [
        {"type": "start", "session_id": "default"},
        {"type": "delta", "content": "single reply"},
        {"type": "done"},
    ]


async def test_fresh_final_send_finishes_with_separator(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    await adapter.send(session.chat_id, "draft", metadata={"expect_edits": True})
    await adapter.send(session.chat_id, "final answer", metadata={"notify": True})

    deltas = [f for f in send.frames() if f["type"] == "delta"]
    assert "".join(f["content"] for f in deltas) == "draft\n\nfinal answer"
    assert send.frames()[-1] == {"type": "done"}
    assert sum(1 for f in send.frames() if f["type"] == "done") == 1


async def test_duplicate_finalization_writes_nothing_more(tmp_path):
    adapter, service = make_adapter(tmp_path)
    session, send = make_session(tmp_path)
    await register_session(adapter, service, session)

    await adapter.send(session.chat_id, "answer")
    await adapter.edit_message(
        session.chat_id, "m", "answer", finalize=True
    )
    await adapter.send(session.chat_id, "answer", metadata={"notify": True})

    assert send.frames() == [
        {"type": "start", "session_id": "default"},
        {"type": "delta", "content": "answer"},
        {"type": "done"},
    ]


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

    def register_platform(self, **kwargs) -> None:
        _FAKE_PLATFORM_NAMES.add(kwargs["name"])
        self.platforms.append(kwargs)

    def register_command(self, **kwargs) -> None:
        self.commands.append(kwargs)


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


def test_register_registers_both_commands(fake_hermes):
    ctx = FakeContext()

    gateway_adapter.register(ctx)

    names = {command["name"] for command in ctx.commands}
    assert names == {"iroh_info", "iroh_allow"}
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
    usage = await handlers["iroh_allow"]("")

    assert service.node_id() in info
    assert allowed == f"allowlisted: {NODE_A}"
    assert "usage" in usage.lower()
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

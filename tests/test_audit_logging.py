"""Audit records and log-hygiene tests."""

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
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def read(self, size_limit: int) -> bytes:
        data, self.payload = self.payload, b""
        return data


class FakeBi:
    def __init__(self, payload: bytes = b"") -> None:
        self._recv = FakeRecv(payload)
        self._send = FakeSend()

    def recv(self) -> FakeRecv:
        return self._recv

    def send(self) -> FakeSend:
        return self._send


class FakeAdapterBase:
    def __init__(self, config, platform) -> None:
        self.config = config
        self.platform = platform

    def _mark_connected(self) -> None:
        pass

    def _mark_disconnected(self) -> None:
        pass


class FakeBackend:
    class _Secret:
        def __init__(self, raw: bytes) -> None:
            self.raw = raw

        def to_bytes(self) -> bytes:
            return self.raw

        def public(self) -> str:
            return hashlib.sha256(self.raw).hexdigest()

    def generate(self):
        return self._Secret(b"\x33" * transport.SECRET_KEY_BYTES)

    def load(self, raw):
        return self._Secret(raw)

    def to_bytes(self, secret):
        return secret.raw

    def node_id(self, secret):
        return secret.public()


def write_allowlist(path: Path, ids, mode: int = 0o600) -> None:
    path.write_text(
        json.dumps({"allowed_node_ids": list(ids)}), encoding="utf-8"
    )
    path.chmod(mode)


def make_allowlist(path: Path) -> transport.Allowlist:
    return transport.Allowlist(path=path, env_reader=lambda: "")


def audit_records(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "iroh audit:" in record.getMessage()
    ]


# --------------------------------------------------------------------------- #
# Allowlist audit records
# --------------------------------------------------------------------------- #


async def test_allowlist_addition_is_audited_at_info(tmp_path, caplog):
    allowlist = make_allowlist(tmp_path / "allowlist.json")

    with caplog.at_level(logging.INFO, logger="transport"):
        await allowlist.add(NODE_A)

    records = audit_records(caplog)
    assert any(
        "allowlist added" in message
        and f"node_id={NODE_A}" in message
        and "entries=1" in message
        for message in records
    )


async def test_allowlist_removal_is_audited_at_info(tmp_path, caplog):
    path = tmp_path / "allowlist.json"
    write_allowlist(path, [NODE_A, NODE_B])
    allowlist = make_allowlist(path)

    with caplog.at_level(logging.INFO, logger="transport"):
        await allowlist.remove(NODE_A)

    records = audit_records(caplog)
    assert any(
        "allowlist removed" in message
        and f"node_id={NODE_A}" in message
        and "entries=1" in message
        for message in records
    )


def test_allowlist_reload_delta_is_audited_at_info(tmp_path, caplog):
    path = tmp_path / "allowlist.json"
    write_allowlist(path, [NODE_A])
    allowlist = make_allowlist(path)
    assert allowlist.entries() == frozenset({NODE_A})

    with caplog.at_level(logging.INFO, logger="transport"):
        write_allowlist(path, [NODE_B])
        assert allowlist.entries() == frozenset({NODE_B})

    records = audit_records(caplog)
    assert any(
        "allowlist reload" in message
        and f"added={NODE_B}" in message
        and f"removed={NODE_A}" in message
        for message in records
    )


def test_raw_allowlist_contents_are_not_logged(tmp_path, caplog):
    path = tmp_path / "allowlist.json"
    write_allowlist(path, [NODE_A, "not-a-node-id", "g" * 64])
    allowlist = make_allowlist(path)

    with caplog.at_level(logging.DEBUG, logger="transport"):
        assert allowlist.entries() == frozenset({NODE_A})

    assert '{"allowed_node_ids"' not in caplog.text
    assert "not-a-node-id" not in caplog.text
    assert "g" * 64 not in caplog.text
    assert NODE_A not in caplog.text


# --------------------------------------------------------------------------- #
# Stream and quota audit records
# --------------------------------------------------------------------------- #


async def test_revoked_stream_is_audited_at_info(tmp_path, caplog):
    path = tmp_path / "allowlist.json"
    write_allowlist(path, [])
    server = transport.ChannelServer(
        key_store=transport.NodeKeyStore(path=tmp_path / "node.key"),
        allowlist=make_allowlist(path),
    )
    bi = FakeBi(
        b'{"type":"prompt","session_id":"default","text":"hi"}\n'
    )

    with caplog.at_level(logging.INFO, logger="transport"):
        await server._serve_stream(bi, NODE_A)

    records = audit_records(caplog)
    assert any(
        "reason=unauthorized" in message and NODE_A in message
        for message in records
    )


async def test_response_cap_rejection_is_audited(tmp_path, caplog):
    send = FakeSend()
    session = transport.StreamSession(
        send,
        node_id=NODE_A,
        session_id="default",
        chat_id=transport.make_chat_id(NODE_A, "default"),
        max_response_bytes=1,
    )

    with caplog.at_level(logging.INFO, logger="transport"):
        await session.start()
        await session.append("too large")

    records = audit_records(caplog)
    assert any(
        "reason=response_too_large" in message and NODE_A in message
        for message in records
    )


# --------------------------------------------------------------------------- #
# Secrets and credentials
# --------------------------------------------------------------------------- #


def test_refused_key_mode_logs_no_key_bytes(tmp_path, caplog):
    path = tmp_path / "node.key"
    raw = b"\x44" * transport.SECRET_KEY_BYTES
    path.write_bytes(raw)
    path.chmod(0o644)
    store = transport.NodeKeyStore(path=path, backend=FakeBackend())

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(transport.KeyStoreError):
            store.load_or_create()

    for representation in (repr(raw), raw.hex(), hashlib.sha256(raw).hexdigest()):
        assert representation not in caplog.text


async def test_relay_credentials_never_appear_in_start_failure_logs(
    tmp_path, monkeypatch, caplog
):
    service = gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(
            path=tmp_path / "node.key", backend=FakeBackend()
        ),
        allowlist=make_allowlist(tmp_path / "allowlist.json"),
    )
    adapter_class = gateway_adapter.build_adapter_class(
        service, FakeAdapterBase, "iroh", object, object
    )
    adapter = adapter_class(None)

    async def failing_start():
        transport.parse_relay_setting(
            "https://user:supersecret@relay.example.com/path?token=abc"
        )

    monkeypatch.setattr(service, "start", failing_start)

    with caplog.at_level(logging.ERROR):
        assert await adapter.connect() is False

    assert "RelayConfigError" in caplog.text
    assert "supersecret" not in caplog.text
    assert "token=abc" not in caplog.text
    assert "relay.example.com" not in caplog.text

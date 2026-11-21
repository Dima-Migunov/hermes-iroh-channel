"""README command documentation must match real handler output."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway_adapter
import transport

README = Path(__file__).resolve().parent.parent / "README.md"
NODE_ID = "3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29"


class FakeBackend:
    class _Secret:
        def __init__(self, raw):
            self.raw = raw

        def to_bytes(self):
            return self.raw

        def public(self):
            return hashlib.sha256(self.raw).hexdigest()

    def generate(self):
        return self._Secret(b"\x07" * transport.SECRET_KEY_BYTES)

    def load(self, raw):
        return self._Secret(raw)

    def to_bytes(self, secret):
        return secret.raw

    def node_id(self, secret):
        return secret.public()


def make_service(tmp_path):
    return gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(
            path=tmp_path / "node.key", backend=FakeBackend()
        ),
        allowlist=transport.Allowlist(
            path=tmp_path / "allowlist.json", env_reader=lambda: ""
        ),
    )


async def test_info_output_labels_match_readme(tmp_path):
    service = make_service(tmp_path)

    class StubServer:
        running = True
        ticket = "endpointabc123..."
        relay = transport.parse_relay_setting("")

    service.server = StubServer()
    text = await gateway_adapter.handle_info(service)
    readme = README.read_text(encoding="utf-8")

    labels = [line.split(":", 1)[0] for line in text.splitlines()]
    assert labels == [
        "node id",
        "ticket",
        "endpoint",
        "alpn",
        "relay",
        "allowlist entries",
        "state dir",
    ]
    for label in labels:
        assert f"{label}:" in readme


async def test_info_hint_is_documented_with_the_other_labels(tmp_path):
    service = make_service(tmp_path)
    text = await gateway_adapter.handle_info(service)
    readme = README.read_text(encoding="utf-8")

    assert "endpoint: not running" in text
    assert text.splitlines()[-1].startswith("hint: ")
    assert "endpoint: not running" in readme
    assert "hint:" in readme


async def test_allow_confirmation_matches_readme(tmp_path):
    service = make_service(tmp_path)
    text = await gateway_adapter.handle_allow(service, NODE_ID)
    readme = README.read_text(encoding="utf-8")

    assert text == f"allowlisted: {NODE_ID}"
    assert f"→ allowlisted: {NODE_ID}" in readme


async def test_allow_usage_line_matches_readme(tmp_path):
    service = make_service(tmp_path)
    text = await gateway_adapter.handle_allow(service, "")
    readme = README.read_text(encoding="utf-8")

    assert text == "usage: /iroh_allow <client_node_id>"
    assert text in readme


async def test_allow_invalid_message_matches_readme(tmp_path):
    service = make_service(tmp_path)
    text = await gateway_adapter.handle_allow(service, "nope")
    readme = README.read_text(encoding="utf-8")

    assert text.startswith("invalid node id: ")
    assert "invalid node id: <reason>" in readme


async def test_allow_write_failure_message_matches_readme(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(transport, "_write_file_atomic", fail_write)
    text = await gateway_adapter.handle_allow(service, NODE_ID)
    readme = README.read_text(encoding="utf-8")

    assert text == "could not update allowlist: OSError"
    assert "could not update allowlist: <error class>" in readme


async def test_revoke_confirmation_matches_readme(tmp_path):
    service = make_service(tmp_path)
    await gateway_adapter.handle_allow(service, NODE_ID)
    text = await gateway_adapter.handle_revoke(service, NODE_ID)
    readme = README.read_text(encoding="utf-8")

    assert text == f"revoked: {NODE_ID}"
    assert f"→ revoked: {NODE_ID}" in readme


async def test_revoke_usage_line_matches_readme(tmp_path):
    service = make_service(tmp_path)
    text = await gateway_adapter.handle_revoke(service, "")
    readme = README.read_text(encoding="utf-8")

    assert text == "usage: /iroh_revoke <client_node_id>"
    assert text in readme


async def test_revoke_invalid_message_matches_readme(tmp_path):
    service = make_service(tmp_path)
    text = await gateway_adapter.handle_revoke(service, "nope")
    readme = README.read_text(encoding="utf-8")

    assert text.startswith("invalid node id: ")
    assert "invalid node id: <reason>" in readme


async def test_revoke_write_failure_message_matches_readme(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)
    await gateway_adapter.handle_allow(service, NODE_ID)

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(transport, "_write_file_atomic", fail_write)
    text = await gateway_adapter.handle_revoke(service, NODE_ID)
    readme = README.read_text(encoding="utf-8")

    assert text == "could not update allowlist: OSError"
    assert "could not update allowlist: <error class>" in readme


async def test_revoke_environment_warning_matches_readme(tmp_path):
    service = gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(
            path=tmp_path / "node.key", backend=FakeBackend()
        ),
        allowlist=transport.Allowlist(
            path=tmp_path / "allowlist.json", env_reader=lambda: NODE_ID
        ),
    )
    await gateway_adapter.handle_allow(service, NODE_ID)
    text = await gateway_adapter.handle_revoke(service, NODE_ID)
    readme = README.read_text(encoding="utf-8")

    assert text == f"still authorized: {NODE_ID} (HERMES_IROH_ALLOWLIST)"
    assert (
        "still authorized: <normalized-id> (HERMES_IROH_ALLOWLIST)" in readme
    )


def test_readme_documents_all_delivery_limitations():
    readme = README.read_text(encoding="utf-8")

    for phrase in (
        "No attachments",
        "No typing indicators",
        "No editing or deletion",
        "No unsolicited messages",
        "No message chunking",
    ):
        assert phrase in readme


def test_readme_documents_session_keying_and_streaming():
    readme = README.read_text(encoding="utf-8")

    assert "chat_id = f\"{remote_node_id}:{session_id}\"" in readme
    assert "append-only deltas" in readme
    assert "session_busy" in readme


def test_documented_send_failure_matches_implementation(tmp_path):
    readme = README.read_text(encoding="utf-8")
    source = (README.parent / "gateway_adapter.py").read_text(encoding="utf-8")

    assert "no active iroh stream" in readme
    assert 'error="no active iroh stream"' in source

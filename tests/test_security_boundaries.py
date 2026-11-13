"""Security boundary tests: default-deny and log hygiene."""

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


class FakeSecret:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def to_bytes(self) -> bytes:
        return self.raw

    def public(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


class FakeBackend:
    def generate(self) -> FakeSecret:
        return FakeSecret(b"\x11" * transport.SECRET_KEY_BYTES)

    def load(self, raw: bytes) -> FakeSecret:
        if len(raw) != transport.SECRET_KEY_BYTES:
            raise ValueError("wrong size")
        return FakeSecret(raw)

    def to_bytes(self, secret: FakeSecret) -> bytes:
        return secret.raw

    def node_id(self, secret: FakeSecret) -> str:
        return secret.public()


def test_empty_allowlist_denies_every_known_id(tmp_path):
    allowlist = transport.Allowlist(
        path=tmp_path / "allowlist.json", env_reader=lambda: ""
    )

    for node_id in (NODE_A, NODE_B, "f" * 64, "0" * 64):
        assert allowlist.check(node_id) is False
    assert allowlist.entries() == frozenset()


def test_empty_allowlist_also_denies_case_variants(tmp_path):
    allowlist = transport.Allowlist(
        path=tmp_path / "allowlist.json", env_reader=lambda: ""
    )

    assert allowlist.check(NODE_A.upper()) is False
    assert allowlist.check(f"  {NODE_A}  ") is False


def test_default_deny_is_not_an_accidental_allow():
    source = (Path(transport.__file__)).read_text(encoding="utf-8")
    assert "def check(self, node_id: str) -> bool:\n        return normalize_node_id(node_id) in self.entries()" in source


def test_key_bytes_never_appear_in_logs(tmp_path, caplog):
    key_path = tmp_path / "node.key"
    store = transport.NodeKeyStore(path=key_path, backend=FakeBackend())

    with caplog.at_level(logging.DEBUG):
        store.node_id()

    raw = key_path.read_bytes()
    for representation in (
        repr(raw),
        raw.hex(),
        hashlib.sha256(raw).hexdigest(),
    ):
        assert representation not in caplog.text


def test_allowlist_contents_never_appear_in_warnings(tmp_path, caplog):
    path = tmp_path / "allowlist.json"
    path.write_text("{not valid json", encoding="utf-8")
    allowlist = transport.Allowlist(
        path=path, env_reader=lambda: f"{NODE_A},{NODE_B}"
    )

    with caplog.at_level(logging.DEBUG):
        assert allowlist.entries() == frozenset({NODE_A, NODE_B})

    assert NODE_A not in caplog.text
    assert NODE_B not in caplog.text


def test_info_output_does_not_leak_key_material(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_IROH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_IROH_ALLOWLIST", NODE_A)
    gateway_adapter.ChannelService.reset()
    service = gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(
            path=tmp_path / "node.key", backend=FakeBackend()
        ),
        allowlist=transport.Allowlist(path=tmp_path / "allowlist.json"),
    )

    info = gateway_adapter.format_info(service)

    raw = (tmp_path / "node.key").read_bytes()
    assert raw.hex() not in info
    assert NODE_A not in info
    assert "node id:" in info
    assert "ticket: unavailable" in info
    gateway_adapter.ChannelService.reset()


def test_allowlist_add_rejects_traversal_shaped_values(tmp_path):
    allowlist = transport.Allowlist(
        path=tmp_path / "allowlist.json", env_reader=lambda: ""
    )

    for value in ("../etc/passwd", "a" * 63, "g" * 64, "", " ", "0x" + "a" * 63):
        with pytest.raises(ValueError):
            asyncio.run(allowlist.add(value))

    assert not (tmp_path / "allowlist.json").exists()

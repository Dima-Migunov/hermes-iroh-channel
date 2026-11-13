"""Node key persistence tests."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transport


class FakeSecret:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def to_bytes(self) -> bytes:
        return self.raw

    def public(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


class FakeBackend:
    def __init__(self) -> None:
        self.generated = 0

    def generate(self) -> FakeSecret:
        self.generated += 1
        return FakeSecret(bytes([self.generated]) * transport.SECRET_KEY_BYTES)

    def load(self, raw: bytes) -> FakeSecret:
        if len(raw) != transport.SECRET_KEY_BYTES:
            raise ValueError("wrong key size")
        return FakeSecret(raw)

    def to_bytes(self, secret: FakeSecret) -> bytes:
        return secret.raw

    def node_id(self, secret: FakeSecret) -> str:
        return secret.public()


def test_creates_key_with_owner_only_permissions(tmp_path):
    path = tmp_path / "state" / "node.key"
    backend = FakeBackend()
    store = transport.NodeKeyStore(path=path, backend=backend)

    node_id = store.node_id()

    assert path.exists()
    assert len(path.read_bytes()) == transport.SECRET_KEY_BYTES
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert node_id == hashlib.sha256(path.read_bytes()).hexdigest()


def test_reload_keeps_node_id_stable(tmp_path):
    path = tmp_path / "node.key"
    first = transport.NodeKeyStore(path=path, backend=FakeBackend())
    first_id = first.node_id()
    raw = path.read_bytes()

    second = transport.NodeKeyStore(path=path, backend=FakeBackend())
    assert second.node_id() == first_id
    assert path.read_bytes() == raw


def test_existing_key_is_not_regenerated(tmp_path):
    path = tmp_path / "node.key"
    path.write_bytes(b"\x07" * transport.SECRET_KEY_BYTES)
    backend = FakeBackend()
    store = transport.NodeKeyStore(path=path, backend=backend)

    store.node_id()

    assert backend.generated == 0
    assert path.read_bytes() == b"\x07" * transport.SECRET_KEY_BYTES


def test_corrupt_key_is_refused_without_overwrite(tmp_path):
    path = tmp_path / "node.key"
    corrupt = b"short"
    path.write_bytes(corrupt)
    store = transport.NodeKeyStore(path=path, backend=FakeBackend())

    with pytest.raises(transport.KeyStoreError) as excinfo:
        store.load_or_create()

    assert str(path) in str(excinfo.value)
    assert path.read_bytes() == corrupt


def test_unreadable_key_is_refused(tmp_path):
    path = tmp_path / "node.key"
    path.write_bytes(b"\x09" * transport.SECRET_KEY_BYTES)
    path.chmod(0o000)
    if os.access(path, os.R_OK):
        pytest.skip("running with privileges that ignore file modes")
    store = transport.NodeKeyStore(path=path, backend=FakeBackend())

    with pytest.raises(transport.KeyStoreError) as excinfo:
        store.load_or_create()

    assert "cannot read" in str(excinfo.value)


def test_default_paths_follow_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_IROH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("HERMES_IROH_KEY_PATH", raising=False)

    assert transport.default_state_dir() == tmp_path
    assert transport.default_key_path() == tmp_path / "node.key"
    assert transport.default_allowlist_path() == tmp_path / "allowlist.json"


def test_key_path_override_wins(tmp_path, monkeypatch):
    override = tmp_path / "custom.key"
    monkeypatch.setenv("HERMES_IROH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERMES_IROH_KEY_PATH", str(override))

    assert transport.default_key_path() == override


def test_default_state_dir_is_the_plugin_directory(monkeypatch):
    monkeypatch.delenv("HERMES_IROH_STATE_DIR", raising=False)

    assert transport.default_state_dir() == Path(transport.__file__).resolve().parent
    assert transport.default_key_path() == (
        Path(transport.__file__).resolve().parent / "node.key"
    )
    assert transport.default_allowlist_path() == (
        Path(transport.__file__).resolve().parent / "allowlist.json"
    )

"""State-file ownership, mode, size, and allowlist safety tests."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway_adapter
import transport

NODE_A = "a" * 64
NODE_B = "b" * 64
NODE_C = "c" * 64


def chmod(path: Path, mode: int) -> Path:
    path.chmod(mode)
    return path


def write_secure(path: Path, payload: str, mode: int = 0o600) -> Path:
    path.write_text(payload, encoding="utf-8")
    path.chmod(mode)
    return path


def write_allowlist(path: Path, ids, mode: int = 0o600) -> Path:
    return write_secure(
        path, json.dumps({"allowed_node_ids": list(ids)}), mode
    )


def make_allowlist(path: Path, env: str = "") -> transport.Allowlist:
    return transport.Allowlist(path=path, env_reader=lambda: env)


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------------- #
# Safe reads
# --------------------------------------------------------------------------- #


def test_safe_read_accepts_an_owner_only_regular_file(tmp_path):
    path = write_secure(tmp_path / "state", "payload")

    assert transport._read_safe_bytes(
        path, max_bytes=32, require_owner_only=True
    ) == b"payload"


def test_safe_read_refuses_a_symlink(tmp_path):
    target = write_secure(tmp_path / "real", "secret")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(transport.FileSafetyError, match="symbolic link"):
        transport._read_safe_bytes(
            link, max_bytes=32, require_owner_only=True
        )


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_safe_read_refuses_group_or_other_permissions(tmp_path, mode):
    path = write_secure(tmp_path / "key", "secret", mode=mode)

    with pytest.raises(transport.FileSafetyError, match="0600"):
        transport._read_safe_bytes(
            path, max_bytes=32, require_owner_only=True
        )


def test_safe_read_refuses_group_writable_when_owner_only_is_not_required(
    tmp_path,
):
    path = write_secure(tmp_path / "allowlist.json", "{}", mode=0o664)

    with pytest.raises(
        transport.FileSafetyError, match="group- or world-writable"
    ):
        transport._read_safe_bytes(
            path, max_bytes=32, require_owner_only=False
        )


def test_safe_read_refuses_an_oversized_file(tmp_path):
    path = write_secure(tmp_path / "allowlist.json", "x" * 11)

    with pytest.raises(
        transport.FileSafetyError, match="10-byte limit"
    ):
        transport._read_safe_bytes(
            path, max_bytes=10, require_owner_only=False
        )


# --------------------------------------------------------------------------- #
# Allowlist input validation
# --------------------------------------------------------------------------- #


def test_invalid_file_entries_are_ignored(tmp_path):
    path = write_allowlist(
        tmp_path / "allowlist.json",
        [NODE_A, "not-a-node-id", "g" * 64, "", " "],
    )
    allowlist = make_allowlist(path)

    assert allowlist.entries() == frozenset({NODE_A})
    assert allowlist.check(NODE_A) is True


def test_invalid_env_entries_are_ignored(tmp_path):
    allowlist = make_allowlist(
        tmp_path / "allowlist.json",
        env=f"{NODE_A} not-a-node-id {'g' * 64} {NODE_B}",
    )

    assert allowlist.entries() == frozenset({NODE_A, NODE_B})


def test_symlinked_allowlist_contributes_no_entries_but_env_applies(
    tmp_path, caplog
):
    target = write_allowlist(tmp_path / "real.json", [NODE_A])
    link = tmp_path / "allowlist.json"
    link.symlink_to(target)
    allowlist = make_allowlist(link, env=NODE_B)

    with caplog.at_level("WARNING"):
        assert allowlist.entries() == frozenset({NODE_B})

    assert allowlist.check(NODE_A) is False
    assert str(link) in caplog.text
    assert NODE_A not in caplog.text


def test_group_writable_allowlist_contributes_no_entries(tmp_path, caplog):
    path = write_allowlist(
        tmp_path / "allowlist.json", [NODE_A], mode=0o664
    )
    allowlist = make_allowlist(path, env=NODE_B)

    with caplog.at_level("WARNING"):
        assert allowlist.entries() == frozenset({NODE_B})

    assert any(
        "group- or world-writable" in record.getMessage()
        for record in caplog.records
    )


def test_oversized_allowlist_contributes_no_entries(tmp_path, caplog):
    path = tmp_path / "allowlist.json"
    path.write_bytes(b" " * (transport.ALLOWLIST_MAX_BYTES + 1))
    path.chmod(0o600)
    allowlist = make_allowlist(path, env=NODE_B)

    with caplog.at_level("WARNING"):
        assert allowlist.entries() == frozenset({NODE_B})

    assert any("limit" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------- #
# State directory checks
# --------------------------------------------------------------------------- #


def test_write_refuses_a_group_writable_state_directory(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    chmod(state, 0o775)

    with pytest.raises(PermissionError, match="chmod 700"):
        transport._write_file_atomic(
            state / "node.key", b"data", prefix=".node.key."
        )

    assert not (state / "node.key").exists()
    assert list(state.iterdir()) == []


def test_write_refuses_a_world_writable_state_directory(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    chmod(state, 0o777)

    with pytest.raises(PermissionError, match="group- or world-writable"):
        transport._write_file_atomic(
            state / "node.key", b"data", prefix=".node.key."
        )

    assert not (state / "node.key").exists()


def test_write_succeeds_in_a_private_state_directory(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    chmod(state, 0o700)

    transport._write_file_atomic(
        state / "node.key", b"data", prefix=".node.key."
    )

    assert (state / "node.key").read_bytes() == b"data"
    assert file_mode(state / "node.key") == 0o600


def test_new_leaf_directories_are_owner_only(tmp_path):
    target = tmp_path / "new" / "deeper" / "node.key"

    transport._write_file_atomic(target, b"data", prefix=".node.key.")

    assert file_mode(target.parent) == 0o700


# --------------------------------------------------------------------------- #
# Allowlist mutation
# --------------------------------------------------------------------------- #


def test_remove_persists_the_remaining_entries(tmp_path):
    path = write_allowlist(tmp_path / "allowlist.json", [NODE_A, NODE_B])
    allowlist = make_allowlist(path)

    removed = asyncio.run(allowlist.remove(NODE_A))

    assert removed == NODE_A
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"allowed_node_ids": [NODE_B]}
    assert allowlist.check(NODE_A) is False
    assert allowlist.check(NODE_B) is True


def test_remove_without_a_file_does_not_create_one(tmp_path):
    path = tmp_path / "allowlist.json"
    allowlist = make_allowlist(path)

    asyncio.run(allowlist.remove(NODE_A))

    assert not path.exists()


def test_add_refuses_a_symlinked_allowlist_and_leaves_it_unchanged(tmp_path):
    target = write_allowlist(tmp_path / "real.json", [NODE_B])
    link = tmp_path / "allowlist.json"
    link.symlink_to(target)
    allowlist = make_allowlist(link)

    with pytest.raises(PermissionError, match="symbolic link"):
        asyncio.run(allowlist.add(NODE_A))

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "allowed_node_ids": [NODE_B]
    }


def test_add_refuses_a_group_writable_allowlist(tmp_path):
    path = write_allowlist(
        tmp_path / "allowlist.json", [NODE_B], mode=0o664
    )
    original = path.read_bytes()
    allowlist = make_allowlist(path)

    with pytest.raises(PermissionError, match="group- or world-writable"):
        asyncio.run(allowlist.add(NODE_A))

    assert path.read_bytes() == original
    assert file_mode(path) == 0o664


def test_remove_refuses_a_group_writable_allowlist(tmp_path):
    path = write_allowlist(
        tmp_path / "allowlist.json", [NODE_A], mode=0o664
    )
    original = path.read_bytes()
    allowlist = make_allowlist(path)

    with pytest.raises(PermissionError):
        asyncio.run(allowlist.remove(NODE_A))

    assert path.read_bytes() == original


def test_add_repairs_a_malformed_but_safe_file(tmp_path):
    path = write_secure(tmp_path / "allowlist.json", "{not json")
    allowlist = make_allowlist(path)

    asyncio.run(allowlist.add(NODE_A))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"allowed_node_ids": [NODE_A]}
    assert allowlist.check(NODE_A) is True


def test_allow_command_reports_an_unsafe_file(tmp_path):
    target = write_allowlist(tmp_path / "real.json", [NODE_B])
    link = tmp_path / "allowlist.json"
    link.symlink_to(target)
    service = gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(path=tmp_path / "node.key"),
        allowlist=make_allowlist(link),
    )

    text = asyncio.run(gateway_adapter.handle_allow(service, NODE_A))

    assert text == "could not update allowlist: PermissionError"
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "allowed_node_ids": [NODE_B]
    }


def test_allow_command_still_repairs_a_malformed_safe_file(tmp_path):
    path = write_secure(tmp_path / "allowlist.json", "{not json")
    service = gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(path=tmp_path / "node.key"),
        allowlist=make_allowlist(path),
    )

    text = asyncio.run(gateway_adapter.handle_allow(service, NODE_C))

    assert text == f"allowlisted: {NODE_C}"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "allowed_node_ids": [NODE_C]
    }


def test_revoke_command_reports_a_symlinked_allowlist(tmp_path):
    target = write_allowlist(tmp_path / "real.json", [NODE_A])
    link = tmp_path / "allowlist.json"
    link.symlink_to(target)
    service = gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(path=tmp_path / "node.key"),
        allowlist=make_allowlist(link),
    )

    text = asyncio.run(gateway_adapter.handle_revoke(service, NODE_A))

    assert text == "could not update allowlist: PermissionError"
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "allowed_node_ids": [NODE_A]
    }


def test_revoke_command_reports_a_group_writable_allowlist(tmp_path):
    path = write_allowlist(
        tmp_path / "allowlist.json", [NODE_A], mode=0o664
    )
    original = path.read_bytes()
    service = gateway_adapter.ChannelService(
        key_store=transport.NodeKeyStore(path=tmp_path / "node.key"),
        allowlist=make_allowlist(path),
    )

    text = asyncio.run(gateway_adapter.handle_revoke(service, NODE_A))

    assert text == "could not update allowlist: PermissionError"
    assert path.read_bytes() == original
    assert file_mode(path) == 0o664

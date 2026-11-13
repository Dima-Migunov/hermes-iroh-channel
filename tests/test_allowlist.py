"""Allowlist behavior tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transport

NODE_A = "a" * 64
NODE_B = "b" * 64
NODE_C = "c" * 64


def make_allowlist(tmp_path, env: str = "") -> transport.Allowlist:
    return transport.Allowlist(
        path=tmp_path / "allowlist.json",
        env_reader=lambda: env,
    )


def write_file(path: Path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_missing_file_and_empty_env_deny_everything(tmp_path):
    allowlist = make_allowlist(tmp_path)

    assert allowlist.entries() == frozenset()
    assert allowlist.check(NODE_A) is False


def test_file_entries_are_accepted(tmp_path):
    write_file(tmp_path / "allowlist.json", {"allowed_node_ids": [NODE_A]})
    allowlist = make_allowlist(tmp_path)

    assert allowlist.check(NODE_A) is True
    assert allowlist.check(NODE_B) is False


def test_env_entries_are_accepted(tmp_path):
    allowlist = make_allowlist(tmp_path, env=NODE_A)

    assert allowlist.check(NODE_A) is True
    assert allowlist.check(NODE_B) is False


def test_file_and_env_are_unioned(tmp_path):
    write_file(tmp_path / "allowlist.json", {"allowed_node_ids": [NODE_A]})
    allowlist = make_allowlist(tmp_path, env=NODE_B)

    assert allowlist.entries() == frozenset({NODE_A, NODE_B})
    assert allowlist.check(NODE_A) is True
    assert allowlist.check(NODE_B) is True
    assert allowlist.check(NODE_C) is False


def test_env_supports_commas_and_whitespace(tmp_path):
    env = f" {NODE_A.upper()} , {NODE_B}\n{NODE_C} "
    allowlist = make_allowlist(tmp_path, env=env)

    assert allowlist.entries() == frozenset({NODE_A, NODE_B, NODE_C})


def test_malformed_file_contributes_no_entries_but_env_still_works(tmp_path):
    (tmp_path / "allowlist.json").write_text("{not json", encoding="utf-8")
    allowlist = make_allowlist(tmp_path, env=NODE_B)

    assert allowlist.entries() == frozenset({NODE_B})
    assert allowlist.check(NODE_A) is False
    assert allowlist.check(NODE_B) is True


def test_wrong_schema_contributes_no_entries(tmp_path):
    write_file(tmp_path / "allowlist.json", {"allowed": [NODE_A]})
    allowlist = make_allowlist(tmp_path)

    assert allowlist.entries() == frozenset()


@pytest.mark.parametrize(
    "payload",
    ["[]", '"string"', "null", "123"],
)
def test_non_object_payload_denies(tmp_path, payload):
    (tmp_path / "allowlist.json").write_text(payload, encoding="utf-8")
    allowlist = make_allowlist(tmp_path)

    assert allowlist.entries() == frozenset()


def test_entries_are_trimmed_and_lowercased(tmp_path):
    write_file(
        tmp_path / "allowlist.json",
        {"allowed_node_ids": [f"  {NODE_A.upper()}  ", NODE_B]},
    )
    allowlist = make_allowlist(tmp_path)

    assert allowlist.entries() == frozenset({NODE_A, NODE_B})
    assert allowlist.check(f" {NODE_A.upper()} ") is True


def test_duplicate_file_entries_are_deduplicated(tmp_path):
    write_file(
        tmp_path / "allowlist.json",
        {"allowed_node_ids": [NODE_A, NODE_A, NODE_A.upper()]},
    )
    allowlist = make_allowlist(tmp_path)

    assert allowlist.entries() == frozenset({NODE_A})


def test_adding_an_entry_persists_it(tmp_path):
    path = tmp_path / "allowlist.json"
    allowlist = make_allowlist(tmp_path)

    async def run() -> str:
        return await allowlist.add(NODE_A)

    import asyncio

    added = asyncio.run(run())

    assert added == NODE_A
    assert allowlist.check(NODE_A) is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"allowed_node_ids": [NODE_A]}
    assert stat_mode(path) == 0o600


def stat_mode(path: Path) -> int:
    import stat

    return stat.S_IMODE(path.stat().st_mode)


def test_adding_a_duplicate_keeps_one_copy(tmp_path):
    path = tmp_path / "allowlist.json"
    write_file(path, {"allowed_node_ids": [NODE_A]})
    allowlist = make_allowlist(tmp_path)

    import asyncio

    asyncio.run(allowlist.add(NODE_A))
    asyncio.run(allowlist.add(f" {NODE_A.upper()} "))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["allowed_node_ids"] == [NODE_A]


def test_adding_an_invalid_id_is_refused(tmp_path):
    path = tmp_path / "allowlist.json"
    allowlist = make_allowlist(tmp_path)

    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(allowlist.add("not-a-node-id"))

    assert not path.exists()
    assert allowlist.entries() == frozenset()


def test_adding_preserves_env_entries_but_writes_only_file(tmp_path):
    path = tmp_path / "allowlist.json"
    allowlist = make_allowlist(tmp_path, env=NODE_B)

    import asyncio

    asyncio.run(allowlist.add(NODE_A))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["allowed_node_ids"] == [NODE_A]
    assert allowlist.check(NODE_B) is True


def test_reload_applies_external_edits(tmp_path):
    path = tmp_path / "allowlist.json"
    write_file(path, {"allowed_node_ids": [NODE_A]})
    allowlist = make_allowlist(tmp_path)
    assert allowlist.check(NODE_A) is True

    write_file(path, {"allowed_node_ids": [NODE_B]})
    os.utime(path, (1, 1))

    assert allowlist.check(NODE_B) is True
    assert allowlist.check(NODE_A) is False


def test_malformed_file_is_logged_without_entries(tmp_path, caplog):
    (tmp_path / "allowlist.json").write_text("{oops", encoding="utf-8")
    allowlist = make_allowlist(tmp_path)

    with caplog.at_level("WARNING"):
        assert allowlist.entries() == frozenset()

    assert any("allowlist" in record.message for record in caplog.records)


def test_env_var_is_read_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(transport.Allowlist.ENV_VAR, NODE_C)
    allowlist = transport.Allowlist(path=tmp_path / "allowlist.json")

    assert allowlist.check(NODE_C) is True

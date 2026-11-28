"""Manifest and packaging checks for the Hermes plugin directory."""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "plugin.yaml"
PLUGIN_NAME = "iroh-channel"
PLATFORM_NAME = "iroh"


def declared_env_names() -> list[str]:
    """Env var names declared in the manifest's optional_env block."""
    text = MANIFEST.read_text(encoding="utf-8")
    block = text.split("optional_env:", 1)
    if len(block) != 2:
        return []
    names = re.findall(r"^\s*-\s*name:\s*([A-Z0-9_]+)\s*$", block[1], re.MULTILINE)
    return names


def test_manifest_declares_required_fields():
    text = MANIFEST.read_text(encoding="utf-8")

    assert MANIFEST.is_file()
    assert f"name: {PLUGIN_NAME}" in text
    assert "kind: platform" in text
    assert "label: " in text
    assert "version: " in text
    assert "description: " in text


def test_manifest_declares_present_tool():
    text = MANIFEST.read_text(encoding="utf-8")

    assert "provides_tools:" in text
    assert "- present" in text


def test_manifest_declares_optional_env():
    names = declared_env_names()

    assert "HERMES_IROH_ALLOWLIST" in names
    assert "HERMES_IROH_RELAY" in names
    assert "HERMES_IROH_TURN_TIMEOUT" in names


def test_state_dir_is_not_declared_because_default_needs_no_config():
    """The plugin runs wherever it is installed; state dir is an override only."""
    assert "HERMES_IROH_STATE_DIR" not in declared_env_names()


def test_default_state_dir_follows_the_installed_plugin(monkeypatch):
    """No configuration needed: state lands next to the plugin's own files."""
    monkeypatch.delenv("HERMES_IROH_STATE_DIR", raising=False)
    monkeypatch.delenv("HERMES_IROH_KEY_PATH", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/nonexistent/hermes-home")

    import transport

    plugin_dir = ROOT.resolve()

    assert transport.default_state_dir() == plugin_dir
    assert transport.default_key_path() == plugin_dir / "node.key"
    assert transport.default_allowlist_path() == plugin_dir / "allowlist.json"


def test_declared_env_vars_are_read_by_transport():
    transport_source = (ROOT / "transport.py").read_text(encoding="utf-8")

    for name in declared_env_names():
        assert name in transport_source, f"{name} is declared but never read"


def test_declared_env_vars_are_documented():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for name in declared_env_names():
        assert name in readme, f"{name} is declared but undocumented"


def test_pyproject_declares_iroh_range():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = [dep.replace(" ", "") for dep in data["project"]["dependencies"]]

    assert any(dep.startswith("iroh>=1.2") and "<2" in dep for dep in dependencies)


def test_entry_point_imports_without_hermes_or_iroh():
    for name in ("gateway", "gateway.config", "iroh"):
        sys.modules.pop(name, None)

    spec = importlib.util.spec_from_file_location(
        "_iroh_channel_entry", ROOT / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.register)
    assert "iroh" not in sys.modules


def test_after_install_covers_switches_and_authorization():
    text = (ROOT / "after-install.md").read_text(encoding="utf-8")

    assert f"hermes plugins enable {PLUGIN_NAME}" in text
    assert f"platforms.{PLATFORM_NAME}.enabled" in text
    assert "/iroh_info" in text
    assert "/iroh_allow" in text


def test_install_docs_name_the_terminal_helper():
    for name in ("README.md", "after-install.md"):
        text = (ROOT / name).read_text(encoding="utf-8")

        assert "scripts/enable.sh" in text, f"{name} omits the helper"
        assert "--restart" in text, f"{name} omits the restart opt-in"
        assert "does not restart" in text.lower(), f"{name} omits the default"


@pytest.mark.parametrize("name", ["README.md", "after-install.md"])
def test_install_docs_document_interactive_fallback(name):
    text = (ROOT / name).read_text(encoding="utf-8").lower()

    assert "register_tool" in text
    assert "registration succeeds" in text
    assert "present" in text
    assert "tool progress" in text
    assert "busy" in text
    assert "plain-text" in text or "plain text" in text
    assert "hint" in text


def test_install_docs_share_the_restart_handoff():
    for name in ("README.md", "after-install.md"):
        text = (ROOT / name).read_text(encoding="utf-8")

        assert "outside the gateway" in text, f"{name} omits the restart boundary"
        assert "gateway_lifecycle_block" in text, f"{name} omits the guard"
        assert (
            "human or host restart step" in text
        ), f"{name} does not hand the guard refusal to a human or host"
        assert (
            "Quit and reopen" in text
        ), f"{name} omits the no-terminal Desktop path"
        assert (
            "Remote Desktop" in text and "another host" in text
        ), f"{name} omits the remote-host limitation"

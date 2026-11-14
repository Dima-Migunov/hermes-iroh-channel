"""Contract tests for scripts/enable.sh using fake Hermes and systemctl CLIs."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "enable.sh"
BASH = shutil.which("bash") or "/bin/bash"

FAKE_HERMES = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_HERMES_LOG"

case "${1:-}" in
  plugins)
    case "${2:-}" in
      enable)
        if ! grep -qxF "${3:-}" "$FAKE_HERMES_ENABLED" 2>/dev/null; then
          printf '%s\\n' "${3:-}" >> "$FAKE_HERMES_ENABLED"
        fi
        echo "enabled ${3:-}"
        ;;
      list)
        if [ -f "$FAKE_HERMES_ENABLED" ]; then
          sed 's/^/  /' "$FAKE_HERMES_ENABLED"
        fi
        ;;
      *)
        echo "unknown plugins subcommand: ${2:-}" >&2
        exit 1
        ;;
    esac
    ;;
  config)
    if [ "${2:-}" != "set" ]; then
      echo "unknown config subcommand: ${2:-}" >&2
      exit 1
    fi
    entry="${3:-}=${4:-}"
    if ! grep -qxF "$entry" "$FAKE_HERMES_CONFIG" 2>/dev/null; then
      printf '%s\\n' "$entry" >> "$FAKE_HERMES_CONFIG"
    fi
    echo "set $entry"
    ;;
  gateway)
    if [ "${2:-}" != "restart" ]; then
      echo "unknown gateway subcommand: ${2:-}" >&2
      exit 1
    fi
    printf 'gateway restart\\n' >> "$FAKE_HERMES_RESTARTS"
    echo "gateway restarted"
    ;;
  *)
    echo "unknown command: $*" >&2
    exit 1
    ;;
esac
"""

FAKE_PIP = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_PIP_LOG"
"""

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"

case "$*" in
  "--user cat hermes-gateway.service")
    [ "${FAKE_SYSTEMCTL_HAS_UNIT:-0}" = "1" ]
    ;;
  "--user restart hermes-gateway.service")
    printf 'systemctl restart\\n' >> "$FAKE_SYSTEMCTL_RESTARTS"
    echo "gateway restarted"
    ;;
  *)
    exit 1
    ;;
esac
"""


def make_env(tmp_path: Path, *, has_unit: bool = False) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    hermes = bin_dir / "hermes"
    hermes.write_text(FAKE_HERMES, encoding="utf-8")
    hermes.chmod(0o755)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(FAKE_SYSTEMCTL, encoding="utf-8")
    systemctl.chmod(0o755)
    pip = bin_dir / "pip"
    pip.write_text(FAKE_PIP, encoding="utf-8")
    pip.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "FAKE_HERMES_LOG": str(tmp_path / "hermes.log"),
            "FAKE_HERMES_ENABLED": str(tmp_path / "enabled.txt"),
            "FAKE_HERMES_CONFIG": str(tmp_path / "config.txt"),
            "FAKE_HERMES_RESTARTS": str(tmp_path / "hermes-restarts.txt"),
            "FAKE_SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
            "FAKE_SYSTEMCTL_RESTARTS": str(tmp_path / "systemctl-restarts.txt"),
            "FAKE_PIP_LOG": str(tmp_path / "pip.log"),
            "FAKE_SYSTEMCTL_HAS_UNIT": "1" if has_unit else "0",
        }
    )
    return env


def run_helper(tmp_path: Path, *args: str, **env_kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=make_env(tmp_path, **env_kwargs),
        timeout=15,
    )


def lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_helper_is_executable():
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


def test_help_documents_the_handoff_and_restart_warning(tmp_path):
    result = run_helper(tmp_path, "--help")

    assert result.returncode == 0
    assert "scripts/enable.sh" in result.stdout
    assert "does not restart the gateway unless --restart" in result.stdout
    assert "--restart" in result.stdout
    assert "outside the gateway" in result.stdout


def test_first_run_enables_without_restarting(tmp_path):
    result = run_helper(tmp_path)

    assert result.returncode == 0
    calls = lines(tmp_path / "hermes.log")
    assert "plugins enable iroh-channel" in calls
    assert "config set gateway.platforms.iroh.enabled true" in calls
    assert "plugins list" in calls
    assert "Restart command for this host:" in result.stdout
    assert "hermes gateway restart" in result.stdout
    assert "No restart was performed." in result.stdout
    assert "pip install" not in result.stdout
    assert lines(tmp_path / "pip.log") == []
    assert lines(tmp_path / "hermes-restarts.txt") == []
    assert lines(tmp_path / "systemctl-restarts.txt") == []


def test_repeated_run_makes_no_duplicate_changes(tmp_path):
    first = run_helper(tmp_path)
    second = run_helper(tmp_path)

    assert first.returncode == 0
    assert second.returncode == 0
    assert lines(tmp_path / "enabled.txt") == ["iroh-channel"]
    assert lines(tmp_path / "config.txt") == [
        "gateway.platforms.iroh.enabled=true"
    ]
    assert "hermes gateway restart" in first.stdout
    assert "hermes gateway restart" in second.stdout
    assert lines(tmp_path / "pip.log") == []
    assert lines(tmp_path / "hermes-restarts.txt") == []


def test_missing_hermes_cli_exits_non_zero(tmp_path):
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()

    result = subprocess.run(
        [BASH, str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": str(empty_bin), "HOME": str(tmp_path)},
        timeout=15,
    )

    assert result.returncode != 0
    assert "Hermes CLI (hermes) is required" in result.stderr
    assert not (tmp_path / "hermes.log").exists()


def test_restart_uses_systemctl_when_the_unit_exists(tmp_path):
    result = run_helper(tmp_path, "--restart", has_unit=True)

    assert result.returncode == 0
    assert "systemctl --user restart hermes-gateway.service" in result.stdout
    assert lines(tmp_path / "systemctl-restarts.txt") == ["systemctl restart"]
    assert lines(tmp_path / "hermes-restarts.txt") == []
    assert "Gateway restart requested." in result.stdout


def test_restart_falls_back_to_the_hermes_cli(tmp_path):
    result = run_helper(tmp_path, "--restart")

    assert result.returncode == 0
    assert "hermes gateway restart" in result.stdout
    assert lines(tmp_path / "hermes-restarts.txt") == ["gateway restart"]
    assert lines(tmp_path / "systemctl-restarts.txt") == []
    assert "Gateway restart requested." in result.stdout

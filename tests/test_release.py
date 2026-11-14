"""Release tooling: metadata stays consistent and version bumps are correct."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "release_script", ROOT / "scripts" / "release.py"
)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def test_metadata_versions_match():
    version = release.ensure_metadata_in_sync()

    assert release.SEMVER.match(version)


@pytest.mark.parametrize(
    ("current", "part", "expected"),
    [
        ("1.0.0", "patch", "1.0.1"),
        ("1.0.0", "minor", "1.1.0"),
        ("1.0.0", "major", "2.0.0"),
        ("1.2.3", "patch", "1.2.4"),
        ("1.2.3", "minor", "1.3.0"),
        ("1.2.3", "major", "2.0.0"),
    ],
)
def test_bump_version(current, part, expected):
    assert release.bump_version(current, part) == expected


@pytest.mark.parametrize("value", ["1.0", "1.0.0.0", "v1.0.0", "", "one.two.three"])
def test_bump_version_rejects_invalid_versions(value):
    with pytest.raises(release.ReleaseError):
        release.bump_version(value, "patch")


def test_bump_version_rejects_unknown_part():
    with pytest.raises(release.ReleaseError):
        release.bump_version("1.0.0", "banana")


def test_update_versions_rewrites_both_files(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "iroh-channel"\nversion = "1.2.3"\n'
        'dependencies = ["iroh>=1.2,<2"]\n',
        encoding="utf-8",
    )
    (tmp_path / "plugin.yaml").write_text(
        "name: iroh-channel\nversion: 1.2.3\nlabel: Iroh Channel\n",
        encoding="utf-8",
    )

    release.update_versions(tmp_path, "2.0.0")

    assert release.read_versions(tmp_path) == ("2.0.0", "2.0.0")
    assert 'dependencies = ["iroh>=1.2,<2"]' in (
        tmp_path / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert "label: Iroh Channel" in (tmp_path / "plugin.yaml").read_text(
        encoding="utf-8"
    )


def test_update_versions_refuses_missing_version(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "iroh-channel"\n', encoding="utf-8"
    )
    (tmp_path / "plugin.yaml").write_text(
        "name: iroh-channel\nversion: 1.2.3\n", encoding="utf-8"
    )

    with pytest.raises(release.ReleaseError):
        release.update_versions(tmp_path, "1.2.4")

    assert "version" not in (tmp_path / "pyproject.toml").read_text(
        encoding="utf-8"
    )


def test_ensure_metadata_reports_mismatch(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        'version = "1.0.0"\n', encoding="utf-8"
    )
    (tmp_path / "plugin.yaml").write_text(
        "version: 1.0.1\n", encoding="utf-8"
    )
    monkeypatch.setattr(release, "ROOT", tmp_path)

    with pytest.raises(release.ReleaseError, match="version mismatch"):
        release.ensure_metadata_in_sync()


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:Dima-Migunov/iroh-channel.git", "Dima-Migunov/iroh-channel"),
        ("https://github.com/Dima-Migunov/iroh-channel.git", "Dima-Migunov/iroh-channel"),
        ("https://github.com/Dima-Migunov/iroh-channel", "Dima-Migunov/iroh-channel"),
    ],
)
def test_parse_repo_slug(remote, expected):
    assert release.parse_repo_slug(remote) == expected


def test_parse_repo_slug_rejects_other_hosts():
    with pytest.raises(release.ReleaseError):
        release.parse_repo_slug("git@gitlab.com:owner/repo.git")

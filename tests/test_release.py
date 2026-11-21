"""Release tooling: metadata stays consistent and version bumps are correct."""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("fix: keep the stream open\x1f", "patch"),
        ("chore: tidy the tests\x1f", "patch"),
        ("plain subject without a type\x1f", "patch"),
        ("feat: add auto releases\x1f", "minor"),
        ("feat(release): add auto releases\x1f", "minor"),
        ("feat!: change the wire format\x1f", "major"),
        ("fix: adjust\x1f\nBREAKING CHANGE: new ALPN", "major"),
        ("chore: release v1.0.1\x1f", None),
        ("\x1f", None),
    ],
)
def test_parse_commit_part(entry, expected):
    assert release.parse_commit_part(entry) == expected


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        (["fix: a\x1f", "docs: b\x1f"], "patch"),
        (["fix: a\x1f", "feat: b\x1f"], "minor"),
        (["feat: a\x1f", "fix!: b\x1f"], "major"),
    ],
)
def test_release_part_for_takes_the_highest_part(entries, expected):
    assert release.release_part_for(entries) == expected


def test_release_part_for_rejects_empty_history():
    with pytest.raises(release.ReleaseError):
        release.release_part_for(["chore: release v1.0.1\x1f"])


def test_commits_since_uses_the_tag_range(monkeypatch):
    calls = []

    def fake_git(*args):
        calls.append(args)
        return "fix: a\x1f\x1e"

    monkeypatch.setattr(release, "git", fake_git)

    entries = release.commits_since("v1.0.0")

    assert entries == ["fix: a\x1f"]
    assert calls[0][-1] == "v1.0.0..HEAD"


def test_last_release_tag_returns_none_without_tags(monkeypatch):
    def fake_git(*args):
        raise release.ReleaseError("no tags")

    monkeypatch.setattr(release, "git", fake_git)

    assert release.last_release_tag() is None


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
        ("git@github.com:Dima-Migunov/hermes-iroh-channel.git", "Dima-Migunov/hermes-iroh-channel"),
        ("https://github.com/Dima-Migunov/hermes-iroh-channel.git", "Dima-Migunov/hermes-iroh-channel"),
        ("https://github.com/Dima-Migunov/hermes-iroh-channel", "Dima-Migunov/hermes-iroh-channel"),
    ],
)
def test_parse_repo_slug(remote, expected):
    assert release.parse_repo_slug(remote) == expected


def test_parse_repo_slug_rejects_other_hosts():
    with pytest.raises(release.ReleaseError):
        release.parse_repo_slug("git@gitlab.com:owner/repo.git")


class _RedirectHandler(BaseHTTPRequestHandler):
    target_hits: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path.endswith("/releases"):
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{self.server.server_port}/target",
            )
            self.end_headers()
            return
        self._record_and_reply()

    def do_GET(self):
        self._record_and_reply()

    def _record_and_reply(self):
        type(self).target_hits.append(dict(self.headers))
        body = json.dumps({"html_url": "http://example.invalid"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def redirect_server():
    _RedirectHandler.target_hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_release_refuses_redirects_without_forwarding_authorization(
    redirect_server, monkeypatch
):
    base = f"http://127.0.0.1:{redirect_server.server_port}"
    monkeypatch.setattr(release, "GITHUB_API", base)

    with pytest.raises(release.ReleaseError) as excinfo:
        release.create_release("owner/repo", "v9.9.9", "notes", "secret-token")

    assert "302" in str(excinfo.value)
    assert _RedirectHandler.target_hits == []

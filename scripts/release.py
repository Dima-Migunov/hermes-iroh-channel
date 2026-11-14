#!/usr/bin/env python3
"""Cut a release: bump both version files, test, commit, tag, push, publish.

The release is refused unless the working tree is clean, the branch is
``main``, local ``main`` matches ``origin/main``, and the two metadata files
already agree on the current version.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
PYPROJECT_VERSION = re.compile(r'(?m)^version\s*=\s*"([^"]+)"\s*$')
PLUGIN_VERSION = re.compile(r"(?m)^version\s*:\s*([^\s#]+)\s*$")
GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")


class ReleaseError(Exception):
    """A release precondition failed or a release step could not complete."""


def git(*args: str) -> str:
    result = subprocess.run(
        ("git", *args), cwd=ROOT, text=True, capture_output=True
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        message = f"git {' '.join(args)} failed"
        raise ReleaseError(f"{message}: {detail}" if detail else message)
    return result.stdout.strip()


def parse_version(value: str) -> tuple[int, int, int]:
    match = SEMVER.match(value)
    if not match:
        raise ReleaseError(f"invalid version: {value!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def bump_version(current: str, part: str) -> str:
    major, minor, patch = parse_version(current)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"unknown release part: {part}")


def read_versions(root: Path | None = None) -> tuple[str, str]:
    root = root or ROOT
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    plugin = (root / "plugin.yaml").read_text(encoding="utf-8")
    pyproject_match = PYPROJECT_VERSION.search(pyproject)
    plugin_match = PLUGIN_VERSION.search(plugin)
    if not pyproject_match or not plugin_match:
        raise ReleaseError(
            "could not find the version in pyproject.toml or plugin.yaml"
        )
    return pyproject_match.group(1), plugin_match.group(1)


def ensure_metadata_in_sync() -> str:
    pyproject_version, plugin_version = read_versions()
    if pyproject_version != plugin_version:
        raise ReleaseError(
            "version mismatch: "
            f"pyproject.toml has {pyproject_version}, "
            f"plugin.yaml has {plugin_version}"
        )
    parse_version(pyproject_version)
    return pyproject_version


def update_versions(root: Path, new_version: str) -> None:
    pyproject_path = root / "pyproject.toml"
    plugin_path = root / "plugin.yaml"
    pyproject = pyproject_path.read_text(encoding="utf-8")
    plugin = plugin_path.read_text(encoding="utf-8")
    pyproject_new, pyproject_count = PYPROJECT_VERSION.subn(
        f'version = "{new_version}"', pyproject
    )
    plugin_new, plugin_count = PLUGIN_VERSION.subn(
        f"version: {new_version}", plugin
    )
    if pyproject_count != 1 or plugin_count != 1:
        raise ReleaseError(
            "expected exactly one version field in pyproject.toml and "
            "plugin.yaml; nothing was written"
        )
    pyproject_path.write_text(pyproject_new, encoding="utf-8")
    plugin_path.write_text(plugin_new, encoding="utf-8")


def parse_repo_slug(remote: str) -> str:
    match = GITHUB_REMOTE.search(remote)
    if not match:
        raise ReleaseError(f"origin is not a GitHub remote: {remote}")
    return f"{match.group(1)}/{match.group(2)}"


def repo_slug() -> str:
    return parse_repo_slug(git("remote", "get-url", "origin"))


def load_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.removeprefix("export ").strip()
            if key != "GITHUB_TOKEN":
                continue
            value = value.strip().strip("'\"").strip()
            if value:
                return value
    raise ReleaseError("GITHUB_TOKEN is not set and was not found in .env")


def require_clean_main() -> None:
    try:
        branch = git("symbolic-ref", "--short", "HEAD")
    except ReleaseError:
        raise ReleaseError(
            "releases must run from the main branch; HEAD is detached"
        ) from None
    if branch != "main":
        raise ReleaseError(f"releases must run from main, currently on {branch}")
    status = git("status", "--porcelain")
    if status:
        raise ReleaseError(f"working tree is not clean:\n{status}")
    git("fetch", "--quiet", "origin", "main")
    local = git("rev-parse", "HEAD")
    remote = git("rev-parse", "origin/main")
    if local != remote:
        raise ReleaseError(
            "local main and origin/main differ; pull or push before releasing"
        )


def tag_exists(tag: str) -> bool:
    if git("tag", "--list", tag):
        return True
    return bool(git("ls-remote", "--tags", "origin", f"refs/tags/{tag}"))


def default_notes(current_version: str) -> str:
    tag = f"v{current_version}"
    if git("tag", "--list", tag):
        revision_range = f"{tag}..HEAD"
        heading = f"Changes since {tag}"
    else:
        revision_range = "HEAD"
        heading = "Changes"
    subjects = git("log", "--pretty=format:%s", revision_range).splitlines()
    subjects = [
        subject
        for subject in subjects
        if subject and not subject.startswith("chore: release v")
    ]
    bullets = "\n".join(f"- {subject}" for subject in subjects)
    return f"## {heading}\n\n{bullets or '- No user-visible changes.'}\n"


def build_notes(args: argparse.Namespace, current_version: str) -> str:
    if args.notes and args.notes_file:
        raise ReleaseError("use either --notes or --notes-file, not both")
    if args.notes:
        return args.notes.rstrip() + "\n"
    if args.notes_file:
        path = Path(args.notes_file)
        if not path.is_file():
            raise ReleaseError(f"notes file not found: {path}")
        return path.read_text(encoding="utf-8")
    return default_notes(current_version)


def run_tests() -> None:
    command = (sys.executable or "python3", "-m", "pytest", "-q")
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        raise ReleaseError("tests failed; nothing was changed")


def create_release(
    slug: str, tag: str, body: str, token: str
) -> str:
    payload = json.dumps(
        {
            "tag_name": tag,
            "target_commitish": "main",
            "name": tag,
            "body": body,
            "draft": False,
            "prerelease": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{slug}/releases",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "iroh-channel-release",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ReleaseError(
            f"GitHub release failed ({exc.code}): {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ReleaseError(f"GitHub release failed: {exc.reason}") from exc
    return data.get("html_url", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bump the version, run tests, commit, tag, push, and publish a "
            "GitHub release."
        )
    )
    parser.add_argument(
        "part",
        choices=("patch", "minor", "major"),
        help="which semantic version component to increment",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run preconditions and tests, print the plan, change nothing",
    )
    parser.add_argument("--notes", help="release notes text")
    parser.add_argument(
        "--notes-file", help="read release notes from this file"
    )
    parser.add_argument(
        "--repo",
        help="GitHub repository as owner/name (default: parsed from origin)",
    )
    args = parser.parse_args(argv)

    require_clean_main()
    current_version = ensure_metadata_in_sync()
    new_version = bump_version(current_version, args.part)
    tag = f"v{new_version}"
    if tag_exists(tag):
        raise ReleaseError(f"tag {tag} already exists")
    notes = build_notes(args, current_version)
    slug = args.repo or repo_slug()

    print(f"current version: {current_version}")
    print(f"next version:    {new_version}")
    print(f"tag:             {tag}")
    print(f"release:         {slug} {tag}")
    print("plan:")
    print("  1. bump pyproject.toml and plugin.yaml")
    print(f"  2. commit 'chore: release {tag}'")
    print(f"  3. create annotated tag {tag}")
    print(f"  4. push main and {tag} atomically")
    print(f"  5. publish the GitHub release for {tag}")
    print("notes:")
    print(notes)

    run_tests()

    if args.dry_run:
        print("dry run: no files, commits, tags, or releases were changed.")
        return 0

    token = load_token()
    update_versions(ROOT, new_version)
    git("add", "pyproject.toml", "plugin.yaml")
    git("commit", "-m", f"chore: release {tag}")
    git("tag", "-a", tag, "-m", tag)
    print(f"pushing main and {tag}...")
    git("push", "--atomic", "origin", "main", f"refs/tags/{tag}")

    url = create_release(slug, tag, notes, token)
    print(f"released: {url}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

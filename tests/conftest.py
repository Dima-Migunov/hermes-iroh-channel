"""Shared pytest fixtures for the Iroh channel plugin test suite."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HAS_IROH = importlib.util.find_spec("iroh") is not None

requires_iroh = pytest.mark.skipif(
    not HAS_IROH, reason="the iroh Python package is not installed"
)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "requires_iroh: skipped when the iroh package is absent"
    )


def pytest_collection_modifyitems(config, items):
    del config
    if HAS_IROH:
        return
    skip = pytest.mark.skip(reason="the iroh Python package is not installed")
    for item in items:
        if "requires_iroh" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _secure_umask():
    """Create fixtures with owner-only modes, like a hardened install."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture(autouse=True)
async def _register_uniffi_event_loop():
    if HAS_IROH:
        import iroh

        iroh.iroh_ffi.uniffi_set_event_loop(asyncio.get_running_loop())
    yield


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_IROH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("HERMES_IROH_KEY_PATH", raising=False)
    return tmp_path


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        "HERMES_IROH_ALLOWLIST",
        "HERMES_IROH_RELAY",
        "HERMES_IROH_KEY_PATH",
        "HERMES_IROH_STATE_DIR",
        "HERMES_IROH_TURN_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch

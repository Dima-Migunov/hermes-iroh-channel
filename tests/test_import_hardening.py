"""Sibling-module fallback tests for directory-plugin loading."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    return spec, module


def test_gateway_adapter_refuses_a_shadowing_transport(
    tmp_path, monkeypatch
):
    foreign_path = tmp_path / "transport.py"
    foreign_path.write_text("SHADOW = True\n", encoding="utf-8")
    foreign_spec, foreign = load_module("foreign_transport", foreign_path)
    foreign_spec.loader.exec_module(foreign)
    monkeypatch.setitem(sys.modules, "transport", foreign)

    spec, module = load_module(
        "shadowed_gateway_adapter", ROOT / "gateway_adapter.py"
    )

    with pytest.raises(ImportError, match="refusing to load transport"):
        spec.loader.exec_module(module)


def test_gateway_adapter_loads_the_plugin_sibling(monkeypatch):
    monkeypatch.delitem(sys.modules, "gateway_adapter_shadow_ok", raising=False)
    spec, module = load_module(
        "gateway_adapter_shadow_ok", ROOT / "gateway_adapter.py"
    )

    spec.loader.exec_module(module)

    assert Path(module.transport.__file__).resolve().parent == ROOT


def test_entry_point_refuses_a_shadowing_gateway_adapter(
    tmp_path, monkeypatch
):
    foreign_path = tmp_path / "gateway_adapter.py"
    foreign_path.write_text(
        "def register(ctx):\n    raise AssertionError('foreign module ran')\n",
        encoding="utf-8",
    )
    foreign_spec, foreign = load_module(
        "foreign_gateway_adapter", foreign_path
    )
    foreign_spec.loader.exec_module(foreign)
    monkeypatch.setitem(sys.modules, "gateway_adapter", foreign)

    spec, entry = load_module("_iroh_entry_shadowed", ROOT / "__init__.py")
    spec.loader.exec_module(entry)

    with pytest.raises(ImportError, match="refusing to load gateway_adapter"):
        entry.register(None)


def test_package_relative_import_uses_the_plugin_sibling():
    package_name = "_iroh_plugin_package_test"
    spec = importlib.util.spec_from_file_location(
        package_name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    try:
        spec.loader.exec_module(package)
        submodule = importlib.import_module(
            f"{package_name}.gateway_adapter"
        )
        assert Path(submodule.__file__).resolve().parent == ROOT

        calls: list[object] = []
        submodule.register = calls.append
        package.register("ctx")

        assert calls == ["ctx"]
    finally:
        sys.modules.pop(f"{package_name}.gateway_adapter", None)
        sys.modules.pop(f"{package_name}.transport", None)
        sys.modules.pop(package_name, None)

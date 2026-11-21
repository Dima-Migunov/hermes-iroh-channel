"""Hermes Agent Iroh channel plugin.

The package stays import-light: the Hermes gateway stack is imported only from
``gateway_adapter.register`` so plugin discovery stays cheap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _is_plugin_sibling(module: Any) -> bool:
    module_path = getattr(module, "__file__", None)
    if module_path is None:
        return False
    return (
        Path(module_path).resolve().parent
        == Path(__file__).resolve().parent
    )


def register(ctx: Any) -> None:
    try:
        from . import gateway_adapter
    except ImportError:
        import gateway_adapter

        if not _is_plugin_sibling(gateway_adapter):
            raise ImportError(
                "refusing to load gateway_adapter from outside the plugin "
                "directory"
            ) from None

    gateway_adapter.register(ctx)

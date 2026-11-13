"""Hermes Agent Iroh channel plugin.

The package stays import-light: the Hermes gateway stack is imported only from
``gateway_adapter.register`` so plugin discovery stays cheap.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    try:
        from . import gateway_adapter
    except ImportError:
        import gateway_adapter

    gateway_adapter.register(ctx)

"""Client tools exposed by the Iroh platform plugin."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PRESENT_TOOL_SCHEMA = {
    "name": "present",
    "description": (
        "Render a compact interactive UI block for an Iroh user. Components may "
        "include card, text, table, chart, form controls, and button elements; "
        "optional content provides surrounding text. Keep all data compact and "
        "within 15 KiB."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "components": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["components"],
    },
}

# Hermes reuses this module but creates a new context when the adapter loads.
_registered_manifests: dict[int, object] = {}


def _present(args: dict, **kwargs: Any) -> str:
    del args, kwargs
    return '{"ok":true,"status":"presented"}'


def register_tools(ctx: Any) -> bool:
    """Register ``present`` once per plugin manifest and report availability."""
    register_tool = getattr(ctx, "register_tool", None)
    if not callable(register_tool):
        logger.info(
            "iroh interactive blocks unavailable; using the plain-text platform hint"
        )
        return False

    manifest = getattr(ctx, "manifest", None)
    manifest_id = id(manifest)
    if manifest is not None and _registered_manifests.get(manifest_id) is manifest:
        return True

    try:
        registration = register_tool(
            name=PRESENT_TOOL_SCHEMA["name"],
            toolset="iroh",
            schema=PRESENT_TOOL_SCHEMA,
            handler=_present,
        )
    except Exception as exc:
        logger.debug(
            "iroh present tool registration skipped: %s", type(exc).__name__
        )
        return False

    if registration is None:
        return False
    if manifest is not None:
        _registered_manifests[manifest_id] = manifest
    return True

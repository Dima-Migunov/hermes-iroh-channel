"""Client tools exposed by the Iroh platform plugin."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SHARED_NODE_PROPERTIES = {
    "_key": {"anyOf": [{"type": "string"}, {"type": "number"}]},
    "_action": {"$ref": "#/$defs/action"},
    "children": {"$ref": "#/$defs/children"},
}

PRESENT_TOOL_SCHEMA = {
    "name": "present",
    "description": (
        "Render a compact interactive UI block for an Iroh user using Text, "
        "Card, Table, Chart, Form, Input, Select, RadioGroup, CheckboxGroup, "
        "DatePicker, Slider, or Button components. Each component uses the "
        "canonical _type discriminator; optional content provides surrounding "
        "text. Keep all data compact and within 15 KiB."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "components": {
                "type": "array",
                "minItems": 1,
                "items": {"$ref": "#/$defs/node"},
            },
        },
        "required": ["components"],
        "additionalProperties": False,
        "$defs": {
            "stringOrNumber": {
                "anyOf": [{"type": "string"}, {"type": "number"}]
            },
            "action": {
                "type": "object",
                "properties": {
                    "type": {"const": "answer"},
                    "value": {},
                },
                "required": ["type"],
                "additionalProperties": True,
            },
            "children": {
                "anyOf": [
                    {"type": "string"},
                    {"$ref": "#/$defs/node"},
                    {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"type": "string"},
                                {"$ref": "#/$defs/node"},
                            ]
                        },
                    },
                ]
            },
            "option": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["label", "value"],
                "additionalProperties": False,
            },
            "describedOption": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["label", "value"],
                "additionalProperties": False,
            },
            "point": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "number"},
                },
                "required": ["value"],
                "additionalProperties": False,
            },
            "series": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "color": {"type": "string"},
                    "data": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/point"},
                    },
                },
                "required": ["data"],
                "additionalProperties": False,
            },
            "column": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "align": {"enum": ["start", "end"]},
                    "format": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "enum": ["number", "currency", "percent", "date"]
                            },
                            "currency": {"type": "string"},
                            "decimals": {"type": "number"},
                        },
                        "required": ["kind"],
                        "additionalProperties": False,
                    },
                },
                "required": ["label"],
                "additionalProperties": False,
            },
            "cardFooterButton": {
                "type": "object",
                "properties": {
                    "label": {"$ref": "#/$defs/stringOrNumber"},
                    "_action": {"$ref": "#/$defs/action"},
                },
                "required": ["label"],
                "additionalProperties": False,
            },
            "node": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            **_SHARED_NODE_PROPERTIES,
                            "_type": {"const": "Text"},
                            "value": {"$ref": "#/$defs/stringOrNumber"},
                            "size": {
                                "enum": ["sm", "md", "lg", "xl", "2xl", "3xl"]
                            },
                            "weight": {
                                "enum": ["normal", "medium", "semibold", "bold"]
                            },
                            "color": {
                                "enum": [
                                    "emphasis",
                                    "secondary",
                                    "alpha-70",
                                    "white",
                                    "white-70",
                                    "white-50",
                                ]
                            },
                        },
                        "required": ["_type", "value"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            **_SHARED_NODE_PROPERTIES,
                            "_type": {"const": "Card"},
                            "title": {"$ref": "#/$defs/stringOrNumber"},
                            "padding": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 8,
                            },
                            "background": {"type": "string"},
                            "asForm": {"type": "boolean"},
                            "confirm": {"$ref": "#/$defs/cardFooterButton"},
                            "cancel": {"$ref": "#/$defs/cardFooterButton"},
                        },
                        "required": ["_type"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            **_SHARED_NODE_PROPERTIES,
                            "_type": {"const": "Table"},
                            "columns": {
                                "type": "array",
                                "items": {"$ref": "#/$defs/column"},
                            },
                            "rows": {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "items": {
                                        "anyOf": [
                                            {"type": "string"},
                                            {"type": "number"},
                                            {"type": "boolean"},
                                        ]
                                    },
                                },
                            },
                            "sortable": {"type": "boolean"},
                        },
                        "required": ["_type"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            **_SHARED_NODE_PROPERTIES,
                            "_type": {"const": "Chart"},
                            "variant": {
                                "enum": ["bar", "line", "sparkline", "area"]
                            },
                            "data": {
                                "type": "array",
                                "items": {"$ref": "#/$defs/point"},
                            },
                            "series": {
                                "type": "array",
                                "items": {"$ref": "#/$defs/series"},
                            },
                            "stacked": {"type": "boolean"},
                            "showAxis": {"type": "boolean"},
                            "showLegend": {"type": "boolean"},
                            "color": {"type": "string"},
                        },
                        "required": ["_type", "variant"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            **_SHARED_NODE_PROPERTIES,
                            "_type": {"const": "Form"},
                            "gap": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 8,
                            },
                        },
                        "required": ["_type", "_action"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            **_SHARED_NODE_PROPERTIES,
                            "_type": {"const": "Input"},
                            "inputType": {
                                "enum": ["text", "password", "number"]
                            },
                            "placeholder": {"type": "string"},
                            "multiline": {"type": "boolean"},
                            "label": {"type": "string"},
                            "name": {"type": "string"},
                            "defaultValue": {"type": "string"},
                            "required": {"type": "boolean"},
                            "minLength": {
                                "type": "integer",
                                "minimum": 0,
                            },
                            "maxLength": {
                                "type": "integer",
                                "minimum": 0,
                            },
                            "min": {"type": "number"},
                            "max": {"type": "number"},
                        },
                        "required": ["_type"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            **_SHARED_NODE_PROPERTIES,
                            "_type": {"const": "Select"},
                            "options": {
                                "type": "array",
                                "items": {"$ref": "#/$defs/option"},
                            },
                            "placeholder": {"$ref": "#/$defs/stringOrNumber"},
                            "label": {"type": "string"},
                            "name": {"type": "string"},
                            "defaultValue": {"type": "string"},
                            "required": {"type": "boolean"},
                        },
                        "required": ["_type", "options"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            **_SHARED_NODE_PROPERTIES,
                            "_type": {"const": "RadioGroup"},
                            "options": {
                                "type": "array",
                                "items": {"$ref": "#/$defs/describedOption"},
                            },
                            "name": {"type": "string"},
                            "label": {"type": "string"},
                            "defaultValue": {"type": "string"},
                            "required": {"type": "boolean"},
                        },
                        "required": ["_type", "options"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            **_SHARED_NODE_PROPERTIES,
                            "_type": {"const": "CheckboxGroup"},
                            "options": {
                                "type": "array",
                                "items": {"$ref": "#/$defs/describedOption"},
                            },
                            "name": {"type": "string"},
                            "label": {"type": "string"},
                            "defaultValue": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "progress": {"type": "boolean"},
                            "required": {"type": "boolean"},
                        },
                        "required": ["_type", "options"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            **_SHARED_NODE_PROPERTIES,
                            "_type": {"const": "DatePicker"},
                            "inputType": {
                                "enum": ["date", "datetime", "time"]
                            },
                            "value": {"type": "string"},
                            "min": {"type": "string"},
                            "max": {"type": "string"},
                            "label": {"type": "string"},
                            "name": {"type": "string"},
                            "required": {"type": "boolean"},
                        },
                        "required": ["_type"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            **_SHARED_NODE_PROPERTIES,
                            "_type": {"const": "Slider"},
                            "name": {"type": "string"},
                            "label": {"type": "string"},
                            "min": {"type": "number"},
                            "max": {"type": "number"},
                            "step": {"type": "number"},
                            "defaultValue": {"type": "number"},
                            "unit": {"type": "string"},
                        },
                        "required": ["_type", "min", "max"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            **_SHARED_NODE_PROPERTIES,
                            "_type": {"const": "Button"},
                            "label": {"$ref": "#/$defs/stringOrNumber"},
                            "buttonStyle": {
                                "enum": [
                                    "primary",
                                    "secondary",
                                    "outline",
                                    "ghost",
                                    "danger",
                                ]
                            },
                            "block": {"type": "boolean"},
                            "submit": {"type": "boolean"},
                            "undoable": {"type": "boolean"},
                        },
                        "required": ["_type", "label"],
                        "anyOf": [
                            {"required": ["_action"]},
                            {
                                "required": ["submit"],
                                "properties": {"submit": {"const": True}},
                            },
                        ],
                        "additionalProperties": False,
                    },
                ]
            },
        },
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

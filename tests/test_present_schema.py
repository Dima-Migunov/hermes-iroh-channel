"""Contract tests for the model-facing ``present`` JSON Schema."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from tools import PRESENT_TOOL_SCHEMA


COMPONENT_NAMES = (
    "Text",
    "Card",
    "Table",
    "Chart",
    "Form",
    "Input",
    "Select",
    "RadioGroup",
    "CheckboxGroup",
    "DatePicker",
    "Slider",
    "Button",
)

VALID_COMPONENTS = (
    ({"_type": "Text", "value": "Hello"}),
    ({"_type": "Card", "title": "Summary"}),
    (
        {
            "_type": "Table",
            "columns": [{"label": "Name"}],
            "rows": [["Ada"]],
        }
    ),
    ({"_type": "Chart", "variant": "bar", "data": [{"value": 3}]}),
    (
        {
            "_type": "Form",
            "_action": {"type": "answer"},
            "children": [{"_type": "Input", "name": "city"}],
        }
    ),
    ({"_type": "Input", "name": "city"}),
    ({"_type": "Select", "options": [{"label": "A", "value": "a"}]}),
    ({"_type": "RadioGroup", "options": [{"label": "A", "value": "a"}]}),
    ({"_type": "CheckboxGroup", "options": [{"label": "A", "value": "a"}]}),
    ({"_type": "DatePicker", "inputType": "date"}),
    ({"_type": "Slider", "min": 0, "max": 10}),
    (
        {
            "_type": "Button",
            "label": "Continue",
            "_action": {"type": "answer", "value": "continue"},
        }
    ),
)


def _accepts(payload: object) -> bool:
    return Draft202012Validator(PRESENT_TOOL_SCHEMA["parameters"]).is_valid(payload)


def test_present_parameters_are_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(PRESENT_TOOL_SCHEMA["parameters"])


def test_present_description_names_the_component_vocabulary():
    description = PRESENT_TOOL_SCHEMA["description"].lower()

    for name in COMPONENT_NAMES:
        assert name.lower() in description
    assert "15 kib" in description


@pytest.mark.parametrize("component", VALID_COMPONENTS, ids=COMPONENT_NAMES)
def test_present_accepts_each_supported_component(component):
    assert _accepts({"components": [component]}) is True


@pytest.mark.parametrize(
    "payload",
    (
        None,
        [],
        "present",
        {},
        {"components": []},
        {"components": {}},
        {"components": [{"_type": "Text", "value": "Hello"}], "extra": True},
        {"components": [{"_type": "Text", "value": "Hello"}], "content": 1},
    ),
)
def test_present_rejects_invalid_root_shapes(payload):
    assert _accepts(payload) is False


@pytest.mark.parametrize(
    "component",
    (
        {"value": "Hello"},
        {"_type": "Unknown", "value": "Hello"},
        {"_type": "text", "value": "Hello"},
        {"type": "Text", "value": "Hello"},
        {"$type": "Text", "value": "Hello"},
        {"component": "Text", "value": "Hello"},
    ),
)
def test_present_rejects_noncanonical_component_discriminators(component):
    assert _accepts({"components": [component]}) is False


@pytest.mark.parametrize(
    "component",
    (
        {"_type": "Text"},
        {"_type": "Chart"},
        {"_type": "Select"},
        {"_type": "RadioGroup"},
        {"_type": "CheckboxGroup"},
        {"_type": "Slider", "min": 0},
        {"_type": "Slider", "max": 10},
        {"_type": "Button", "_action": {"type": "answer"}},
        {"_type": "Form"},
        {"_type": "Button", "label": "Continue"},
        {"_type": "Button", "label": "Continue", "submit": False},
    ),
)
def test_present_rejects_components_missing_required_fields(component):
    assert _accepts({"components": [component]}) is False


@pytest.mark.parametrize(
    "component",
    (
        {"_type": "Text", "value": "Hello", "rows": []},
        {"_type": "Text", "value": "Hello", "variant": "bar"},
        {"_type": "Button", "label": "Continue", "options": []},
    ),
)
def test_present_rejects_unknown_or_other_type_properties(component):
    assert _accepts({"components": [component]}) is False


@pytest.mark.parametrize("field", ("minLength", "maxLength"))
def test_present_rejects_fractional_input_length_limits(field):
    component = {"_type": "Input", field: 1.5}

    assert _accepts({"components": [component]}) is False


@pytest.mark.parametrize(
    "component",
    (
        {"_type": "Form"},
        {
            "_type": "Button",
            "label": "Continue",
            "_action": {"type": "submit"},
        },
        {
            "_type": "Button",
            "label": "Continue",
            "_action": {"value": "continue"},
        },
    ),
)
def test_present_rejects_invalid_actions(component):
    assert _accepts({"components": [component]}) is False


@pytest.mark.parametrize(
    "component",
    (
        {
            "_type": "Button",
            "label": "Continue",
            "_action": {"type": "answer", "value": "continue"},
        },
        {"_type": "Button", "label": "Submit", "submit": True},
        {
            "_type": "Button",
            "label": "Submit",
            "submit": True,
            "_action": {
                "type": "answer",
                "value": {"nested": [1, True, None]},
                "client_extension": "kept",
            },
        },
    ),
)
def test_present_accepts_button_action_or_submission(component):
    assert _accepts({"components": [component]}) is True


def test_present_accepts_shared_keys_and_nested_components():
    payload = {
        "content": "Choose an option",
        "components": [
            {
                "_type": "Card",
                "_key": "card-1",
                "children": [
                    {"_type": "Text", "value": "Ready?", "_key": 1},
                    {
                        "_type": "Button",
                        "label": "Continue",
                        "_action": {"type": "answer", "value": "continue"},
                    },
                ],
            }
        ],
    }

    assert _accepts(payload) is True


def test_present_rejects_invalid_nested_components():
    payload = {
        "components": [
            {
                "_type": "Card",
                "children": [{"_type": "Button", "label": "Continue"}],
            }
        ]
    }

    assert _accepts(payload) is False

"""Tests for deriving a tool's output schema from its return annotation."""

from __future__ import annotations

import dataclasses
import sys
from collections import Counter, OrderedDict
from collections.abc import (
    Collection,
    Iterable,
    Mapping,
    MutableMapping,
    Sequence,
)
from dataclasses import dataclass
from enum import Enum
from typing import Literal, cast

import pytest

if sys.version_info >= (3, 11):
    from typing import NotRequired, Required, TypedDict
else:  # pragma: no cover - Python 3.10
    from typing_extensions import NotRequired, Required, TypedDict

from toolregistry import Tool, ToolMetadata, ToolRegistry
from toolregistry import _output_schema as output_schema
from toolregistry._output_schema import _inline_refs, derive_output_schema


class Priority(Enum):
    LOW = "low"
    HIGH = "high"


class Address(TypedDict):
    city: str
    zip_code: NotRequired[str]


class User(TypedDict):
    name: str
    address: Address
    aliases: list[Address]


class Partial(TypedDict, total=False):
    note: str
    value: int


class Node(TypedDict):
    value: int
    child: Node | None


class OptionalBase(TypedDict, total=False):
    opt: int


class RequiredChild(OptionalBase):
    req: str


class MustBase(TypedDict):
    must: str


class RelaxedChild(MustBase, total=False):
    extra: int


class MixedBase(TypedDict):
    p: int
    q: NotRequired[int]


class MixedChild(MixedBase, total=False):
    r: str


class ExplicitRequired(TypedDict, total=False):
    must: Required[str]
    note: str


class StrictBase(TypedDict):
    p: int


class RelaxedRedeclare(StrictBase, total=False):
    p: NotRequired[int]
    q: int


class LooseBase(TypedDict, total=False):
    a: int


class TightRedeclare(LooseBase):
    a: Required[int]
    b: int


class Adder:
    """Callable instance used to check ``type(func).__call__`` derivation."""

    def __call__(self, a: int, b: int) -> int:
        return a + b


@dataclass
class Point:
    x: int
    y: float = 0.0
    label: str | None = None


@dataclass
class Envelope:
    points: list[Point]
    owner: str


def returns_user() -> User:
    """Return a user."""
    return cast(
        User,
        {"name": "", "address": {"city": ""}, "aliases": []},
    )


def returns_point() -> Point:
    """Return a point."""
    return Point(1)


def returns_envelope() -> Envelope:
    """Return an envelope."""
    return Envelope([Point(1)], "owner")


def returns_str() -> str:
    """Return a string."""
    return ""


def returns_int() -> int:
    """Return an integer."""
    return 0


def returns_bool() -> bool:
    """Return a boolean."""
    return True


def returns_list() -> list[str]:
    """Return a list."""
    return []


def returns_bare_list() -> list:
    """Return an unparameterized list."""
    return []


def returns_bare_dict() -> dict:
    """Return an unparameterized dictionary."""
    return {}


def returns_bare_tuple() -> tuple:
    """Return an unparameterized tuple."""
    return ()


def returns_dict() -> dict[str, int]:
    """Return a mapping."""
    return {}


def returns_tuple() -> tuple[int, str]:
    """Return a fixed tuple."""
    return (1, "")


def returns_literal() -> Literal["a", "b"]:
    """Return one of two literals."""
    return "a"


def returns_enum() -> Priority:
    """Return an enum member."""
    return Priority.LOW


def returns_optional() -> str | None:
    """Return an optional string."""
    return None


def returns_none() -> None:
    """Return nothing."""
    return None


def returns_required_child() -> RequiredChild:
    """Return a child that adds a required key to an optional base."""
    return {"req": ""}


def returns_relaxed_child() -> RelaxedChild:
    """Return a child that adds optional keys to a required base."""
    return {"must": ""}


def returns_mixed_child() -> MixedChild:
    """Return a child of a base with mixed required and optional keys."""
    return {"p": 0}


def returns_explicit_required() -> ExplicitRequired:
    """Return a total=False model whose Required marker is a string."""
    return {"must": ""}


def returns_relaxed_redeclare() -> RelaxedRedeclare:
    """Return a required key redeclared as optional in the child."""
    return {"q": 0}


def returns_tight_redeclare() -> TightRedeclare:
    """Return an optional key redeclared as required in the child."""
    return {"a": 0, "b": 0}


def returns_mapping() -> Mapping[str, int]:
    """Return an abstract mapping."""
    return {}


def returns_mutable_mapping() -> MutableMapping[str, bool]:
    """Return a mutable abstract mapping."""
    return {}


class MyDict(dict):
    """A bare concrete mapping subclass."""


def returns_ordered_dict() -> OrderedDict[str, int]:
    """Return a parameterized mapping subclass."""
    return OrderedDict()


def returns_counter() -> Counter[str]:
    """Return a mapping subclass parameterized with only a key type."""
    return Counter()


def returns_custom_dict() -> MyDict:
    """Return a bare concrete mapping subclass."""
    return MyDict()


def returns_sequence() -> Sequence[int]:
    """Return an abstract sequence."""
    return []


def returns_iterable() -> Iterable[str]:
    """Return an abstract iterable."""
    return []


def returns_collection() -> Collection[float]:
    """Return an abstract collection."""
    return []


def returns_bytes() -> bytes:
    """Return bytes, which encode as a base64 string."""
    return b""


def returns_set() -> set[str]:
    """Return a set, which the JSON encoder stringifies."""
    return set()


def returns_frozenset() -> frozenset[int]:
    """Return a frozenset, which the JSON encoder stringifies."""
    return frozenset()


def returns_missing():
    """Return an annotation that cannot resolve."""
    raise NotImplementedError


# A forward reference is the supported way to describe a type that is not
# importable here; ``get_type_hints`` then raises and derivation must decline.
returns_missing.__annotations__ = {"return": "MissingType"}


def returns_node() -> Node:
    """Return a recursive model."""
    return {"value": 1, "child": None}


def returns_unannotated():
    """Return without an annotation."""
    return 1


def test_typeddict_required_and_optional_keys() -> None:
    """Only keys declared required appear in ``required``."""
    schema = derive_output_schema(returns_user)

    assert schema is not None
    assert schema["type"] == "object"
    assert schema["required"] == ["address", "aliases", "name"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["address"] == {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "zip_code": {"type": "string"},
        },
        "additionalProperties": False,
        "required": ["city"],
    }


def test_typeddict_total_false_has_no_required_keys() -> None:
    """``total=False`` marks every key optional."""

    def partial() -> Partial:
        return {}

    schema = derive_output_schema(partial)

    assert schema is not None
    assert "required" not in schema
    assert set(schema["properties"]) == {"note", "value"}


def test_typeddict_child_of_optional_base_keeps_its_required_key() -> None:
    """The leaf ``total`` flag does not erase a child's required key."""
    schema = derive_output_schema(returns_required_child)

    assert schema is not None
    assert schema["required"] == ["req"]
    assert set(schema["properties"]) == {"opt", "req"}


def test_typeddict_optional_child_of_required_base_keeps_inherited_required() -> None:
    """A ``total=False`` child still inherits required keys from its base."""
    schema = derive_output_schema(returns_relaxed_child)

    assert schema is not None
    assert schema["required"] == ["must"]
    assert set(schema["properties"]) == {"extra", "must"}


def test_typeddict_mixed_base_keys_keep_per_class_total_flags() -> None:
    """A required and an optional key from one base keep their own flags."""
    schema = derive_output_schema(returns_mixed_child)

    assert schema is not None
    assert schema["required"] == ["p"]
    assert set(schema["properties"]) == {"p", "q", "r"}


def test_typeddict_resolved_required_marker_beats_leaf_total() -> None:
    """``Required[T]`` stays required even when ``total=False`` and deferred."""
    schema = derive_output_schema(returns_explicit_required)

    assert schema is not None
    assert schema["required"] == ["must"]
    assert set(schema["properties"]) == {"must", "note"}


def test_typeddict_child_can_redeclare_a_key_as_optional() -> None:
    """A ``NotRequired`` redeclaration overrides the base's required key."""
    schema = derive_output_schema(returns_relaxed_redeclare)

    assert schema is not None
    assert "required" not in schema
    assert set(schema["properties"]) == {"p", "q"}


def test_typeddict_child_can_redeclare_a_key_as_required() -> None:
    """A ``Required`` redeclaration overrides the base's optional key."""
    schema = derive_output_schema(returns_tight_redeclare)

    assert schema is not None
    assert schema["required"] == ["a", "b"]
    assert set(schema["properties"]) == {"a", "b"}


def test_nested_typeddict_list_is_inlined() -> None:
    """A list of nested models carries the object schema in ``items``."""
    schema = derive_output_schema(returns_user)

    assert schema is not None
    aliases = schema["properties"]["aliases"]
    assert aliases["type"] == "array"
    assert aliases["items"]["type"] == "object"
    assert aliases["items"]["additionalProperties"] is False
    assert "$defs" not in schema
    assert "$ref" not in str(schema)


def test_dataclass_with_defaults_and_optional() -> None:
    """Defaulted fields are optional; the annotation still publishes their type."""
    schema = derive_output_schema(returns_point)

    assert schema is not None
    assert schema["required"] == ["x"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["y"] == {"type": "number"}
    assert schema["properties"]["label"] == {"type": ["string", "null"]}


def test_nested_dataclass_list() -> None:
    """A dataclass field that is a list of dataclasses is inlined."""
    schema = derive_output_schema(returns_envelope)

    assert schema is not None
    assert schema["required"] == ["owner", "points"]
    points = schema["properties"]["points"]
    assert points["type"] == "array"
    assert points["items"]["required"] == ["x"]
    assert points["items"]["properties"]["x"] == {"type": "integer"}


@pytest.mark.parametrize(
    ("func", "expected"),
    [
        (returns_str, {"type": "string"}),
        (returns_int, {"type": "integer"}),
        (returns_bool, {"type": "boolean"}),
        (returns_list, {"type": "array", "items": {"type": "string"}}),
        (returns_bare_list, {"type": "array"}),
        (returns_bare_dict, {"type": "object"}),
        (returns_bare_tuple, {"type": "array"}),
        (
            returns_dict,
            {"type": "object", "additionalProperties": {"type": "integer"}},
        ),
        (
            returns_tuple,
            {
                "type": "array",
                "prefixItems": [{"type": "integer"}, {"type": "string"}],
                "minItems": 2,
                "maxItems": 2,
            },
        ),
        (returns_literal, {"enum": ["a", "b"], "type": "string"}),
        (returns_enum, {"enum": ["low", "high"], "type": "string"}),
        (returns_optional, {"type": ["string", "null"]}),
        (
            returns_mapping,
            {"type": "object", "additionalProperties": {"type": "integer"}},
        ),
        (
            returns_mutable_mapping,
            {"type": "object", "additionalProperties": {"type": "boolean"}},
        ),
        (
            returns_ordered_dict,
            {"type": "object", "additionalProperties": {"type": "integer"}},
        ),
        (returns_counter, {"type": "object"}),
        (returns_custom_dict, {"type": "object"}),
        (returns_sequence, {"type": "array", "items": {"type": "integer"}}),
        (returns_iterable, {"type": "array", "items": {"type": "string"}}),
        (returns_collection, {"type": "array", "items": {"type": "number"}}),
    ],
)
def test_plain_return_annotations(func, expected) -> None:
    """Primitive, container, literal, enum and nullable returns map directly."""
    assert derive_output_schema(func) == expected


def test_none_return_annotation_adds_nothing() -> None:
    """A tool that returns nothing publishes no null-only schema."""
    assert derive_output_schema(returns_none) is None


@pytest.mark.parametrize("func", [returns_bytes, returns_set, returns_frozenset])
def test_concrete_non_mapping_iterables_add_nothing(func) -> None:
    """A concrete iterable does not imply the JSON shape the encoder produces."""
    assert derive_output_schema(func) is None


def test_pydantic_model_is_inlined() -> None:
    """A pydantic model keeps its own schema, with references resolved."""
    pydantic = pytest.importorskip("pydantic")

    class Item(pydantic.BaseModel):
        name: str
        count: int = 0

    def returns_item() -> Item:
        return Item(name="")

    schema = derive_output_schema(returns_item, localns=locals())

    assert schema is not None
    assert schema["type"] == "object"
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["count"]["type"] == "integer"
    assert schema["required"] == ["name"]
    assert "$defs" not in schema
    assert "$ref" not in str(schema)


def test_unresolvable_annotation_adds_nothing() -> None:
    """A return annotation that cannot resolve yields no schema, not ``{}``."""
    assert derive_output_schema(returns_missing) is None


def test_missing_annotation_adds_nothing() -> None:
    """An unannotated function yields no schema."""
    assert derive_output_schema(returns_unannotated) is None


def test_recursive_annotation_adds_nothing(monkeypatch) -> None:
    """The cycle check stops the descent instead of hitting the recursion limit."""
    calls = 0
    original = output_schema._type_hints

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(output_schema, "_type_hints", counting)

    assert derive_output_schema(returns_node) is None
    assert calls <= 3


def test_tool_from_function_is_off_by_default() -> None:
    """The per-construction switch defaults to off."""
    tool = Tool.from_function(returns_user)

    assert "output_schema" not in tool.metadata.extra


def test_tool_from_function_derives_when_enabled() -> None:
    """The per-construction switch stores the derived schema in ``extra``."""
    tool = Tool.from_function(returns_user, derive_output_schema=True)

    schema = tool.metadata.extra["output_schema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["address", "aliases", "name"]


def test_explicit_output_schema_is_never_overridden() -> None:
    """A declared contract wins even when derivation is enabled."""
    declared = {"type": "string"}
    tool = Tool.from_function(
        returns_user,
        metadata=ToolMetadata(extra={"output_schema": declared}),
        derive_output_schema=True,
    )

    assert tool.metadata.extra["output_schema"] == declared


def test_callable_instance_derives_from_dunder_call() -> None:
    """A callable instance uses its ``__call__`` return annotation."""
    assert derive_output_schema(Adder()) == {"type": "integer"}


def test_registry_switch_derives_for_callable_instance() -> None:
    """The registry switch reaches a registered callable instance."""
    registry = ToolRegistry(derive_output_schemas=True)
    registry.register(Adder(), name="adder")

    tool = registry.get_tool("adder")
    assert tool is not None
    assert tool.metadata.extra["output_schema"] == {"type": "integer"}


def test_registry_switch_off_keeps_no_schema() -> None:
    """The registry default leaves every tool without an output schema."""
    registry = ToolRegistry()
    registry.register(returns_user, name="user_tool")

    tool = registry.get_tool("user_tool")
    assert tool is not None
    assert "output_schema" not in tool.metadata.extra


def test_registry_switch_on_derives_for_registered_functions() -> None:
    """The registry switch is forwarded to ``Tool.from_function``."""
    registry = ToolRegistry(derive_output_schemas=True)
    registry.register(returns_user, name="user_tool")

    tool = registry.get_tool("user_tool")
    assert tool is not None
    assert tool.metadata.extra["output_schema"]["required"] == [
        "address",
        "aliases",
        "name",
    ]


def test_registry_switch_derives_for_override() -> None:
    """An override callable goes through the same switch."""
    registry = ToolRegistry(derive_output_schemas=True)
    registry.register(returns_str, name="value_tool")
    registry.override("value_tool", returns_user)

    tool = registry.get_tool("value_tool")
    assert tool is not None
    assert tool.metadata.extra["output_schema"]["type"] == "object"


def test_registry_switch_on_leaves_harmless_annotation_alone() -> None:
    """An unresolvable return annotation keeps today's no-schema behaviour."""
    registry = ToolRegistry(derive_output_schemas=True)
    registry.register(returns_missing, name="missing_tool")

    tool = registry.get_tool("missing_tool")
    assert tool is not None
    assert "output_schema" not in tool.metadata.extra


def test_dataclass_fields_are_still_dataclass_fields() -> None:
    """Derivation does not mutate the annotated dataclass."""
    assert [field.name for field in dataclasses.fields(Point)] == ["x", "y", "label"]


def test_inline_refs_resolves_local_definitions() -> None:
    """A pydantic-style ``$defs`` reference is replaced by its definition."""
    schema = {
        "type": "object",
        "properties": {"child": {"$ref": "#/$defs/Child"}},
        "$defs": {
            "Child": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        },
    }

    assert _inline_refs(schema) == {
        "type": "object",
        "properties": {
            "child": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        },
    }


def test_inline_refs_rejects_a_reference_cycle() -> None:
    """A recursive reference yields no schema instead of looping."""
    schema = {
        "type": "object",
        "properties": {"self": {"$ref": "#/$defs/Node"}},
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"self": {"$ref": "#/$defs/Node"}},
            }
        },
    }

    assert _inline_refs(schema) is None


def test_pydantic_model_config_controls_additional_properties() -> None:
    """An open pydantic model stays open; derivation does not close it."""
    pydantic = pytest.importorskip("pydantic")

    class OpenItem(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="allow")

        name: str

    def returns_open_item() -> OpenItem:
        return OpenItem(name="")

    schema = derive_output_schema(returns_open_item, localns=locals())

    assert schema is not None
    assert schema["additionalProperties"] is True
    assert schema == _inline_refs(OpenItem.model_json_schema())

"""Derive a JSON Schema from a tool's return annotation.

The derivation is intentionally conservative: an annotation that cannot be
resolved to a complete schema produces ``None`` instead of a partial or empty
schema.  Callers publish the result only when it is a mapping, so a tool
without a usable return annotation keeps yesterday's behaviour (no
``outputSchema``).

Supported annotations are primitives,
``list``/``dict``/``tuple`` containers and their ``collections.abc``
counterparts (``Mapping``, ``Sequence``, ``Iterable``, ``Collection``),
``Literal``, ``Enum``, ``TypedDict`` (including ``Required`` and
``NotRequired``), dataclasses, and pydantic models.  Nested ``TypedDict`` and
dataclass definitions are inlined.  References emitted by a pydantic model
``$defs`` are resolved before the schema is returned, because some MCP
clients do not follow ``$ref``.  A recursive shape yields no schema rather
than a looping conversion.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import types
import typing
from collections.abc import (
    Collection,
    Iterable,
    Mapping,
    MutableMapping,
    Sequence,
)
from enum import Enum
from typing import Any, get_args, get_origin, get_type_hints

_NULL_SCHEMA: dict[str, Any] = {"type": "null"}

_PRIMITIVE_TYPES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}

#: Origins that carry a type argument the schema does not change.
_TRANSPARENT_ORIGINS: tuple[Any, ...] = tuple(
    origin
    for origin in (
        getattr(typing, "Annotated", None),
        getattr(typing, "Final", None),
        getattr(typing, "ClassVar", None),
        getattr(typing, "Required", None),
        getattr(typing, "NotRequired", None),
    )
    if origin is not None
)


def _origin_name(origin: Any) -> str | None:
    """Return the ``_name`` of a typing construct such as ``Required``."""
    name = getattr(origin, "_name", None)
    return name if isinstance(name, str) else None


def _unwrap(hint: Any) -> Any:
    """Strip ``Annotated``, ``Required``, ``NotRequired``, ``Final`` wrappers."""
    origin = get_origin(hint)
    while origin is not None:
        if origin in _TRANSPARENT_ORIGINS or _origin_name(origin) in {
            "Annotated",
            "Required",
            "NotRequired",
            "Final",
            "ClassVar",
        }:
            args = get_args(hint)
            if not args:
                break
            hint = args[0]
            origin = get_origin(hint)
            continue
        break
    return hint


def _type_hints(
    target: Any, localns: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Resolve annotations on *target*, or return ``None`` when they do not resolve."""
    try:
        return get_type_hints(target, localns=dict(localns or {}), include_extras=True)
    except Exception:
        return None


def _is_typeddict(tp: Any) -> bool:
    """Return whether *tp* is a ``TypedDict``, including the backport."""
    checker = getattr(typing, "is_typeddict", None)
    if callable(checker) and checker(tp):
        return True
    try:
        from typing_extensions import is_typeddict as extension_checker
    except ImportError:
        return False
    try:
        return bool(extension_checker(tp))
    except TypeError:
        return False


def _is_pydantic_model(tp: Any) -> bool:
    """Return whether *tp* looks like a pydantic model, without importing it."""
    if not isinstance(tp, type):
        return False
    if callable(getattr(tp, "model_json_schema", None)) and hasattr(tp, "model_fields"):
        return True
    return callable(getattr(tp, "schema", None)) and any(
        getattr(base, "__name__", "") == "BaseModel"
        for base in getattr(tp, "__mro__", ())
    )


def _primitive_type(value: Any) -> str | None:
    """Return the JSON type of one primitive Python value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return None


def _literal_schema(values: tuple[Any, ...]) -> dict[str, Any] | None:
    """Build an ``enum`` schema for ``Literal`` values or enum members."""
    normalized: list[Any] = []
    for value in values:
        if isinstance(value, Enum):
            value = value.value
        if value is not None and _primitive_type(value) is None:
            return None
        normalized.append(value)

    schema: dict[str, Any] = {"enum": normalized}
    types_seen = {_primitive_type(value) for value in normalized if value is not None}
    if len(types_seen) == 1 and None not in normalized:
        json_type = types_seen.pop()
        if json_type is not None:
            schema["type"] = json_type
    return schema


def _make_nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """Add ``null`` to a schema, preferring a type list when that is enough."""
    if set(schema) == {"type"}:
        json_type = schema["type"]
        if isinstance(json_type, str):
            return {"type": [json_type, "null"]}
        if isinstance(json_type, list) and "null" not in json_type:
            return {"type": [*json_type, "null"]}
    return {"anyOf": [schema, dict(_NULL_SCHEMA)]}


def _union_schema(
    args: tuple[Any, ...],
    localns: Mapping[str, Any] | None,
    stack: frozenset[Any],
) -> dict[str, Any] | None:
    """Build an ``anyOf`` schema for a union, collapsing simple nullables."""
    branches: list[dict[str, Any]] = []
    nullable = False
    for arg in args:
        branch = _convert(_unwrap(arg), localns, stack)
        if branch is None:
            return None
        if branch == _NULL_SCHEMA:
            nullable = True
            continue
        if set(branch) == {"anyOf"} and isinstance(branch["anyOf"], list):
            branches.extend(branch["anyOf"])
        else:
            branches.append(branch)

    if nullable:
        if len(branches) == 1:
            return _make_nullable(branches[0])
        branches.append(dict(_NULL_SCHEMA))
    if not branches:
        return None
    if len(branches) == 1:
        return branches[0]
    return {"anyOf": branches}


def _required_marker(hint: Any) -> bool | None:
    """Return whether a TypedDict hint is explicitly required or optional.

    ``__required_keys__`` is computed when the class is created from the raw
    annotation text, so ``from __future__ import annotations`` hides
    ``NotRequired[T]`` from it.  Resolving the hints first and inspecting the
    wrapper here gives the same answer for string and object annotations.
    """
    origin = get_origin(hint)
    while origin is not None and (
        origin is getattr(typing, "Annotated", None)
        or _origin_name(origin) == "Annotated"
    ):
        args = get_args(hint)
        if not args:
            return None
        hint = args[0]
        origin = get_origin(hint)
    if origin is None:
        return None
    name = _origin_name(origin)
    if name == "Required":
        return True
    if name == "NotRequired":
        return False
    return None


def _typeddict_bases(typed_dict: Any) -> list[Any]:
    """Return the ``TypedDict`` bases of *typed_dict*.

    A ``TypedDict`` class reports ``dict`` in ``__bases__``; its real bases
    live in ``__orig_bases__``.
    """
    candidates = getattr(typed_dict, "__orig_bases__", None) or getattr(
        typed_dict, "__bases__", ()
    )
    return [base for base in candidates if _is_typeddict(base)]


def _typeddict_declared_keys(typed_dict: Any) -> set[str]:
    """Return every key a ``TypedDict`` declares, including inherited keys."""
    keys = set(getattr(typed_dict, "__annotations__", {}) or {})
    for base in _typeddict_bases(typed_dict):
        keys |= _typeddict_declared_keys(base)
    return keys


def _required_keys(
    typed_dict: Any,
    hints: Mapping[str, Any],
    localns: Mapping[str, Any] | None,
) -> set[str]:
    """Return the keys a ``TypedDict`` declares as required.

    Each class in the inheritance chain keeps its own ``total`` flag.  Walking
    the bases applies that flag to the keys the base declares, which the leaf
    ``__total__`` cannot express.  A resolved ``Required``/``NotRequired``
    marker still wins over the flag, because ``__required_keys__`` itself is
    unreliable when ``from __future__ import annotations`` hides the wrapper.
    """
    required: set[str] = set()
    inherited: set[str] = set()
    for base in _typeddict_bases(typed_dict):
        inherited |= _typeddict_declared_keys(base)
        base_hints = _type_hints(base, localns)
        if base_hints is None:
            required |= set(getattr(base, "__required_keys__", ()) or ())
        else:
            required |= _required_keys(base, base_hints, localns)

    total = bool(getattr(typed_dict, "__total__", True))
    for name, hint in hints.items():
        if name in inherited:
            continue
        marker = _required_marker(hint)
        is_required = total if marker is None else marker
        if is_required:
            required.add(name)
    return required


def _typeddict_schema(
    typed_dict: Any,
    localns: Mapping[str, Any] | None,
    stack: frozenset[Any],
) -> dict[str, Any] | None:
    """Build a closed object schema for a ``TypedDict``."""
    if typed_dict in stack:
        return None
    hints = _type_hints(typed_dict, localns)
    if hints is None:
        return None

    required = _required_keys(typed_dict, hints, localns)

    next_stack = stack | {typed_dict}
    properties: dict[str, Any] = {}
    for name, hint in hints.items():
        schema = _convert(_unwrap(hint), localns, next_stack)
        if schema is None:
            return None
        properties[name] = schema

    schema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    required_names = sorted(set(required) & set(properties))
    if required_names:
        schema["required"] = required_names
    return schema


def _dataclass_schema(
    cls: Any,
    localns: Mapping[str, Any] | None,
    stack: frozenset[Any],
) -> dict[str, Any] | None:
    """Build a closed object schema for a dataclass."""
    if cls in stack:
        return None
    hints = _type_hints(cls, localns)
    if hints is None:
        return None

    next_stack = stack | {cls}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in dataclasses.fields(cls):
        hint = hints.get(field.name)
        if hint is None:
            continue
        schema = _convert(_unwrap(hint), localns, next_stack)
        if schema is None:
            return None
        properties[field.name] = schema
        if (
            field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING
        ):
            required.append(field.name)

    schema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = sorted(required)
    return schema


def _resolve_ref(ref: str) -> str | None:
    """Return the definition name a local JSON pointer refers to."""
    if not ref.startswith("#/"):
        return None
    return ref.rsplit("/", 1)[-1]


def _inline_sequence(
    node: list[Any],
    definitions: Mapping[str, Any],
    resolving: frozenset[str],
) -> list[Any] | None:
    """Inline every item in a JSON Schema array."""
    result: list[Any] = []
    for item in node:
        converted = _inline_node(item, definitions, resolving)
        if converted is None:
            return None
        result.append(converted)
    return result


def _inline_ref_node(
    node: dict[str, Any],
    definitions: Mapping[str, Any],
    resolving: frozenset[str],
) -> dict[str, Any] | None:
    """Replace one ``$ref`` node with its inlined definition."""
    name = _resolve_ref(node["$ref"])
    if name is None or name not in definitions or name in resolving:
        return None
    resolved = _inline_node(
        copy.deepcopy(definitions[name]), definitions, resolving | {name}
    )
    if resolved is None:
        return None
    siblings = {key: value for key, value in node.items() if key != "$ref"}
    if not siblings:
        return resolved if isinstance(resolved, dict) else None
    if not isinstance(resolved, dict):
        return None
    converted = _inline_node(siblings, definitions, resolving)
    if converted is None:
        return None
    return {**resolved, **converted}


def _inline_mapping(
    node: dict[str, Any],
    definitions: Mapping[str, Any],
    resolving: frozenset[str],
) -> dict[str, Any] | None:
    """Inline every value in a JSON Schema object node."""
    result: dict[str, Any] = {}
    for key, value in node.items():
        converted = _inline_node(value, definitions, resolving)
        if converted is None:
            return None
        result[key] = converted
    return result


def _inline_node(
    node: Any,
    definitions: Mapping[str, Any],
    resolving: frozenset[str],
) -> Any:
    """Inline references inside one JSON Schema node."""
    if isinstance(node, list):
        return _inline_sequence(node, definitions, resolving)
    if not isinstance(node, dict):
        return node
    if isinstance(node.get("$ref"), str):
        return _inline_ref_node(node, definitions, resolving)
    return _inline_mapping(node, definitions, resolving)


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Inline ``$defs``/``definitions`` references into one self-contained schema."""
    definitions: dict[str, Any] = {}
    for key in ("$defs", "definitions"):
        value = schema.get(key)
        if isinstance(value, dict):
            definitions.update(value)

    root = {
        key: value
        for key, value in schema.items()
        if key not in ("$defs", "definitions")
    }
    inlined = _inline_node(root, definitions, frozenset())
    return inlined if isinstance(inlined, dict) else None


def _pydantic_schema(cls: Any) -> dict[str, Any] | None:
    """Return a pydantic model's own JSON Schema with references inlined."""
    try:
        if callable(getattr(cls, "model_json_schema", None)):
            schema = cls.model_json_schema()
        else:
            schema = cls.schema()
    except Exception:
        return None
    if not isinstance(schema, dict):
        return None
    return _inline_refs(schema)


def _class_schema(
    cls: Any,
    localns: Mapping[str, Any] | None,
    stack: frozenset[Any],
) -> dict[str, Any] | None:
    """Convert a class-like annotation into a JSON Schema node."""
    if cls in (list, tuple):
        return {"type": "array"}
    if cls is dict:
        return {"type": "object"}
    if cls in _PRIMITIVE_TYPES:
        return {"type": _PRIMITIVE_TYPES[cls]}
    if issubclass(cls, Enum):
        return _literal_schema(tuple(cls))
    if _is_typeddict(cls):
        return _typeddict_schema(cls, localns, stack)
    if dataclasses.is_dataclass(cls):
        return _dataclass_schema(cls, localns, stack)
    if _is_pydantic_model(cls):
        return _pydantic_schema(cls)
    if issubclass(cls, Mapping):
        return {"type": "object"}
    if issubclass(cls, Iterable):
        return {"type": "array"}
    return None


def _list_schema(
    args: tuple[Any, ...],
    localns: Mapping[str, Any] | None,
    stack: frozenset[Any],
) -> dict[str, Any] | None:
    """Convert ``list[T]`` into an array schema."""
    if not args:
        return {"type": "array"}
    items = _convert(_unwrap(args[0]), localns, stack)
    if items is None:
        return None
    return {"type": "array", "items": items}


def _dict_schema(
    args: tuple[Any, ...],
    localns: Mapping[str, Any] | None,
    stack: frozenset[Any],
) -> dict[str, Any] | None:
    """Convert ``dict[str, T]`` into an open object schema."""
    if not args:
        return {"type": "object"}
    if len(args) != 2:
        return None
    key_type, value_type = args
    if _unwrap(key_type) is not str:
        return None
    value_schema = _convert(_unwrap(value_type), localns, stack)
    if value_schema is None:
        return None
    if not value_schema:
        return {"type": "object"}
    return {"type": "object", "additionalProperties": value_schema}


def _tuple_schema(
    args: tuple[Any, ...],
    localns: Mapping[str, Any] | None,
    stack: frozenset[Any],
) -> dict[str, Any] | None:
    """Convert a fixed or variadic tuple into an array schema."""
    if not args:
        return {"type": "array"}
    if len(args) == 2 and args[1] is Ellipsis:
        items = _convert(_unwrap(args[0]), localns, stack)
        if items is None:
            return None
        return {"type": "array", "items": items}
    prefix_items: list[dict[str, Any]] = []
    for arg in args:
        item = _convert(_unwrap(arg), localns, stack)
        if item is None:
            return None
        prefix_items.append(item)
    return {
        "type": "array",
        "prefixItems": prefix_items,
        "minItems": len(prefix_items),
        "maxItems": len(prefix_items),
    }


def _container_schema(
    origin: Any,
    args: tuple[Any, ...],
    localns: Mapping[str, Any] | None,
    stack: frozenset[Any],
) -> dict[str, Any] | None:
    """Convert a container annotation into a JSON Schema node."""
    if origin is list or origin in (Sequence, Iterable, Collection):
        return _list_schema(args, localns, stack)
    if origin is dict or origin in (Mapping, MutableMapping):
        return _dict_schema(args, localns, stack)
    if origin is tuple:
        return _tuple_schema(args, localns, stack)
    return None


def _convert(
    hint: Any,
    localns: Mapping[str, Any] | None,
    stack: frozenset[Any],
) -> dict[str, Any] | None:
    """Convert one annotation into a JSON Schema node, or ``None`` if unsupported."""
    hint = _unwrap(hint)

    if hint is None or hint is type(None):
        return dict(_NULL_SCHEMA)
    if hint is Any or hint is object:
        return {}

    origin = get_origin(hint)
    args = get_args(hint)

    if origin is typing.Literal:
        return _literal_schema(args)
    if origin is typing.Union or origin is types.UnionType:
        return _union_schema(args, localns, stack)
    if origin is not None:
        return _container_schema(origin, args, localns, stack)
    if isinstance(hint, type):
        return _class_schema(hint, localns, stack)
    return None


def derive_output_schema(
    func: Any,
    *,
    localns: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Derive a JSON Schema for *func*'s return annotation.

    Args:
        func: A callable whose return annotation describes the value the tool
            produces.
        localns: Names visible only to the function that defined the
            annotation, typically ``locals()`` from that function.  Forward
            references that cannot be resolved through the function's globals
            need this mapping.

    Returns:
        A JSON Schema mapping for the return annotation, or ``None`` when the
        annotation is missing, unresolvable, unsupported, recursive, empty,
        or a bare ``None``.  A tool that returns nothing publishes no schema:
        a null-only contract adds no information, and MCP servers that
        declare one are expected to emit ``structuredContent`` that a null
        result does not have.  An explicit ``extra["output_schema"]`` still
        opts in.
    """
    if not callable(func):
        return None
    hints = _type_hints(func, localns)
    if hints is None:
        return None
    annotation = hints.get("return", inspect.Signature.empty)
    if annotation is inspect.Signature.empty:
        return None
    annotation = _unwrap(annotation)
    if annotation is None or annotation is type(None):
        return None
    schema = _convert(annotation, localns, frozenset())
    if not schema:
        return None
    return schema

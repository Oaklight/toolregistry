# Output Schemas

A tool can publish the shape of the value it returns. The schema is stored in
`ToolMetadata.extra["output_schema"]`; LLM discovery and MCP transports read it
from there and publish it as `outputSchema`. Core can derive that schema from
the function's return annotation, so a typed function does not need a
hand-written JSON Schema.

Derivation is opt-in. Publishing an `outputSchema` changes what MCP clients
see, so a deployment can enable it without changing a tool definition.

## Enable derivation

Use the registry switch for most deployments:

```python
from toolregistry import ToolRegistry

registry = ToolRegistry(derive_output_schemas=True)
```

Every function registered through `registry.register()` is converted with
`Tool.from_function(..., derive_output_schema=True)`. Class registration and
callable overrides use the same path. A callable instance derives from the
return annotation on `type(obj).__call__`.

Enable it for one tool when you build the `Tool` directly:

```python
from toolregistry import Tool

tool = Tool.from_function(my_search, derive_output_schema=True)
```

Both switches default to `False`, which preserves the contract from earlier
releases: no tool gains an `outputSchema` unless it declares one.

## Priority and fallback

The order is fixed:

1. An explicit `ToolMetadata.extra["output_schema"]` wins. Presence of the key
   is enough; it is never replaced by a derived schema.
2. Otherwise, a derived schema is used when the switch is on.
3. Otherwise, the tool publishes no output schema.

An annotation that cannot be resolved to a complete schema adds nothing. Core
does not publish an empty `{}` schema, because that would claim a contract that
was never described.

```python
from toolregistry import Tool, ToolMetadata

declared = {"type": "object", "properties": {"path": {"type": "string"}}}

tool = Tool.from_function(
    find_file,
    metadata=ToolMetadata(extra={"output_schema": declared}),
    derive_output_schema=True,  # ignored: the declaration wins
)
```

## Supported return annotations

- Primitives: `str`, `int`, `float`, `bool`
- Containers: `list[T]`, `dict[str, T]`, `tuple[...]`, including a fixed tuple
- Abstract containers: `Mapping[str, T]`, `MutableMapping[str, T]`,
  `Sequence[T]`, `Iterable[T]`, and `Collection[T]`
- `Literal[...]` and `Enum` subclasses
- `Optional[...]` / `T | None` as nullable; a bare `None` return publishes no
  schema
- `TypedDict`, including `total=False` and `NotRequired`
- Dataclasses, including fields with defaults
- Pydantic `BaseModel` subclasses

Nested `TypedDict` and dataclass definitions are inlined. A pydantic model's
`$defs` references are resolved before the schema is returned, so a client that
does not follow `$ref` still receives a complete schema. A recursive shape
produces no schema instead of looping.

Only a `Mapping` subclass derives an object schema. A concrete iterable class
such as `class MyList(list)` publishes no schema, because the annotation alone
does not fix how the tool encodes it; declare `extra["output_schema"]` when the
shape is known. A parameterized `Mapping` subclass such as
`OrderedDict[str, int]` derives the same open object as `dict[str, int]`.

## Semantic decisions

### Closed and open objects

A `TypedDict` and a dataclass are closed shapes in the type system, so their
derived schema sets `additionalProperties: false`. A plain `dict[str, T]`
remains open and carries `additionalProperties` as the value schema. A
pydantic model follows its own configuration: a model with `extra="allow"`
stays open.

If a tool needs the opposite contract, declare `extra["output_schema"]`.
Derivation never silently flips an existing published contract.

### Required keys

Only keys declared required appear in `required`.

- A `TypedDict` key wrapped in `NotRequired`, or any key of a `total=False`
  model, is omitted from `required`.
- A dataclass field with a default is omitted from `required`.
- The type of an optional key is still published; only its required status
  changes.

### Nullable values

A simple nullable shape uses a type list, for example
`{"type": ["string", "null"]}`. A shape with more keywords (an enum or a nested
object) uses `anyOf` with a null branch.

### Inlined definitions

Nested named shapes are emitted inline. The derived top-level schema contains
no `$defs` or `$ref`, which keeps it usable with clients that resolve neither.

### Recursion

A type that refers to itself yields no schema. The conversion detects the type
already being visited and stops; it never loops and never emits a partial
recursive contract.

## Example

```python
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from toolregistry import ToolRegistry


class Address(TypedDict):
    city: str
    postal_code: NotRequired[str]


@dataclass
class Customer:
    name: str
    address: Address
    note: str | None = None


registry = ToolRegistry(derive_output_schemas=True)


@registry.register
def load_customer(customer_id: int) -> Customer:
    """Load one customer."""
    ...
```

The published schema describes a closed object with required `name` and
`address`, an optional `note`, and an inlined `address` object whose only
required key is `city`.

"""First-class Agent Skills support.

A skill is a directory with a ``SKILL.md`` file: YAML frontmatter holds the
name, description, and optional requirements; the Markdown body holds the
instructions.  Skills use progressive disclosure:

1. **Catalog** — names and descriptions, loaded up front.
2. **Activation** — the full instructions, loaded when the skill is chosen.
3. **Resources** — files under ``scripts/``, ``references/``, and ``assets/``,
   read on demand.

This module re-exports the vendored zerodep skill model and adds a store with
standard-root discovery, filtering, safe resource reads, and tools that expose
skills to a model.

Example::

    from toolregistry.skills import SkillStore, default_skill_roots

    store = SkillStore()
    store.discover()
    print(store.catalog())
    print(store.activate("pdf-processing"))

    # Or through ToolRegistry:
    from toolregistry import ToolRegistry

    registry = ToolRegistry()
    registry.discover_skills()
    registry.register_skill_tools()
"""

from __future__ import annotations

from .._vendor.skills import (
    BM25Selector,
    KeywordSelector,
    ParseError,
    SelectionResult,
    Selector,
    Skill,
    SkillError,
    SkillProperties,
    SkillRegistry,
    ValidationError,
    compose,
    discover,
    filter_compatible,
    load,
    loads,
    to_catalog,
    validate,
)
from ._store import (
    DEFAULT_MAX_INLINE_BYTES,
    DEFAULT_MAX_RESOURCE_BYTES,
    MAX_RESOURCE_BYTES,
    SkillNotFoundError,
    SkillResource,
    SkillResourceError,
    SkillRoot,
    SkillStore,
    default_skill_roots,
    skill_to_dict,
)
from ._tools import (
    CATALOG_TOOL_METHODS,
    SkillToolMode,
    build_catalog_tools,
    build_per_skill_tools,
    build_skill_tools,
)

__all__ = [
    "BM25Selector",
    "CATALOG_TOOL_METHODS",
    "DEFAULT_MAX_INLINE_BYTES",
    "DEFAULT_MAX_RESOURCE_BYTES",
    "KeywordSelector",
    "MAX_RESOURCE_BYTES",
    "ParseError",
    "SelectionResult",
    "Selector",
    "Skill",
    "SkillError",
    "SkillNotFoundError",
    "SkillProperties",
    "SkillRegistry",
    "SkillResource",
    "SkillResourceError",
    "SkillRoot",
    "SkillStore",
    "SkillToolMode",
    "ValidationError",
    "build_catalog_tools",
    "build_per_skill_tools",
    "build_skill_tools",
    "compose",
    "default_skill_roots",
    "discover",
    "filter_compatible",
    "load",
    "loads",
    "skill_to_dict",
    "to_catalog",
    "validate",
]

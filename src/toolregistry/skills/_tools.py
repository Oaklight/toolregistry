"""Tools that expose Agent Skills to a model.

Two usage modes are supported:

* ``catalog`` — three stable tools for listing, activating, and reading a
  skill resource.  The model chooses from the Level-1 catalog and loads a
  skill on demand.
* ``per-skill`` — one no-argument tool per skill.  Activating the tool returns
  the skill's instructions, so the model can select a skill from the normal
  tool list.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
import warnings
from collections.abc import Iterable
from typing import Literal

from .._names import normalize_tool_name
from ..tool import Tool, ToolMetadata, ToolTag
from ._store import MAX_RESOURCE_BYTES, Skill, SkillResourceError, SkillStore

__all__ = [
    "CATALOG_TOOL_METHODS",
    "SkillResourceError",
    "SkillToolMode",
    "build_catalog_tools",
    "build_per_skill_tools",
    "build_skill_tools",
]

SkillToolMode = Literal["catalog", "per-skill", "both"]

#: Method names used by the catalog tool set.
CATALOG_TOOL_METHODS = ("list", "get", "read_resource")

#: Method names used when the catalog tool set has no namespace.
_UNSCOPED_CATALOG_METHODS = ("list_skills", "get_skill", "read_skill_resource")

#: Characters accepted by common function-calling APIs.
_INVALID_TOOL_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")

#: Maximum length of a tool name accepted by OpenAI-compatible APIs.
_MAX_TOOL_NAME_LENGTH = 64

#: Length of the stable hash suffix used to disambiguate names.
_HASH_LENGTH = 8


def _sanitize_tool_name(value: str) -> str:
    """Fold accents and replace characters outside the API-safe set."""
    folded = unicodedata.normalize("NFKD", value)
    folded = "".join(char for char in folded if unicodedata.category(char) != "Mn")
    safe = _INVALID_TOOL_NAME_CHARS.sub("_", folded)
    return re.sub(r"_+", "_", safe).strip("_")


def _method_name_for_skill(name: str, *, fallback: str | None = None) -> str:
    """Return a function-call-safe method name for a skill.

    ``normalize_tool_name`` handles the usual separators and CamelCase, but a
    skill registered directly through the store can still carry characters
    such as ``/``, ``:``, or non-ASCII text.  Accents are folded, anything
    else is replaced with an underscore, and an unreadable name falls back to
    the skill directory slug before the generic ``skill``.

    Args:
        name: Raw skill name.
        fallback: Directory slug used when *name* has no usable characters.

    Returns:
        A name matching ``[A-Za-z0-9_-]+``.
    """
    method = normalize_tool_name(name)
    safe = _sanitize_tool_name(method)
    if not safe and fallback:
        safe = _sanitize_tool_name(normalize_tool_name(fallback))
    if safe and not re.match(r"[A-Za-z_]", safe):
        safe = f"skill_{safe}"
    if not safe:
        safe = "skill"
    if safe != method:
        warnings.warn(
            f"Skill name {name!r} is not a valid tool name; using {safe!r}.",
            UserWarning,
            stacklevel=3,
        )
    return safe


def _method_budget(namespace: str | None) -> int:
    """Return the method-name length available under *namespace*."""
    if not namespace:
        return _MAX_TOOL_NAME_LENGTH
    prefix = len(normalize_tool_name(namespace)) + 1
    return max(_HASH_LENGTH + 1, _MAX_TOOL_NAME_LENGTH - prefix)


def _stable_suffix(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    return f"_{digest}"


def _fit_method(method: str, suffix: str, budget: int) -> str:
    """Truncate *method* so that it plus *suffix* fits within *budget*."""
    if len(suffix) >= budget:
        return suffix.lstrip("_")[:budget] or "skill"
    head = method[: budget - len(suffix)].rstrip("_")
    return f"{head}{suffix}" if head else suffix.lstrip("_")


def _unique_method(
    method: str,
    raw_name: str,
    used: set[str],
    budget: int,
    *,
    prefix: str = "",
) -> str:
    """Return a unique, prefixed method name that fits within *budget*."""
    budget = max(_HASH_LENGTH + 1, budget - len(prefix))
    changed = False
    if len(method) > budget:
        method = _fit_method(method, _stable_suffix(raw_name), budget)
        changed = True
    candidate = f"{prefix}{method}"
    if candidate in used:
        suffix = _stable_suffix(raw_name)
        method = _fit_method(method, suffix, budget)
        counter = 2
        while f"{prefix}{method}" in used:
            method = _fit_method(method, f"{suffix}_{counter}", budget)
            counter += 1
        changed = True
    candidate = f"{prefix}{method}"
    if changed:
        warnings.warn(
            f"Skill {raw_name!r} was exposed as {candidate!r} to keep the tool "
            f"name unique and at most {_MAX_TOOL_NAME_LENGTH} characters.",
            UserWarning,
            stacklevel=3,
        )
    used.add(candidate)
    return candidate


def build_catalog_tools(
    store: SkillStore,
    *,
    namespace: str | None = "skills",
) -> list[Tool]:
    """Build the catalog/list/get/read tool set.

    Args:
        store: Skill store the tools call.
        namespace: Namespace applied by the registry.  When ``None``, the
            method names carry the ``_skills`` suffix themselves.

    Returns:
        Unregistered tools.  Pass them to :meth:`ToolRegistry.register`.
    """
    list_name, get_name, read_name = (
        CATALOG_TOOL_METHODS if namespace else _UNSCOPED_CATALOG_METHODS
    )

    def list_skills(query: str = "", limit: int = 20) -> str:
        """List available Agent Skills, optionally ranked by a query.

        Returns an ``<available_skills>`` catalog.  Each entry has a name and
        description.  Use ``get_skill`` to load one skill's full instructions.

        Args:
            query: Optional natural-language task description.  When given,
                skills are ranked by relevance and unrelated skills are
                omitted.
            limit: Maximum number of skills to return (1-200).
        """
        limit = max(1, min(int(limit), 200))
        if query.strip():
            matches = store.select(query, top_k=limit)
            names = [match.skill.name for match in matches]
            if not names:
                return (
                    "No skills matched that query. "
                    "Call list_skills with an empty query to see every skill."
                )
        else:
            names = [skill.name for skill in store.list()[:limit]]
        return store.catalog(names)

    def get_skill(name: str, inline_resources: bool = False) -> str:
        """Load a skill's full instructions (progressive disclosure level 2).

        Args:
            name: Exact skill name from ``list_skills``.
            inline_resources: Include the content of small bundled resource
                files.  When false, only their paths are listed.
        """
        return store.activate(name, inline_resources=inline_resources)

    def read_skill_resource(
        name: str,
        path: str,
        max_bytes: int = 64 * 1024,
    ) -> str:
        """Read one file bundled with a skill (level 3).

        Args:
            name: Exact skill name from ``list_skills``.
            path: Skill-relative path, for example ``references/guide.md``.
            max_bytes: Maximum accepted file size.  Capped at 10 MiB.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        capped = min(max_bytes, MAX_RESOURCE_BYTES)
        content = store.read_resource(name, path, max_bytes=capped)
        escaped_name = html.escape(name)
        escaped_path = html.escape(path)
        return (
            f'<skill_resource skill="{escaped_name}" path="{escaped_path}">\n'
            f"{content}\n"
            "</skill_resource>"
        )

    source = "skills"
    metadata = ToolMetadata(tags={ToolTag.READ_ONLY}, source=source)
    return [
        Tool.from_function(
            list_skills,
            name=list_name,
            description=(
                "List available Agent Skills and their descriptions. "
                "Pass a task description as the query to rank skills by "
                "relevance. Use get_skill to load a skill after choosing one."
            ),
            metadata=metadata,
        ),
        Tool.from_function(
            get_skill,
            name=get_name,
            description=(
                "Load an Agent Skill's full instructions by name. Use the "
                "exact name returned by list_skills. This is the activation "
                "step before following the skill."
            ),
            metadata=metadata,
        ),
        Tool.from_function(
            read_skill_resource,
            name=read_name,
            description=(
                "Read a reference, script, or asset bundled with an Agent "
                "Skill. Paths are relative to the skill directory and are "
                "listed by get_skill."
            ),
            metadata=ToolMetadata(
                tags={ToolTag.READ_ONLY, ToolTag.FILE_SYSTEM},
                source=source,
            ),
        ),
    ]


def build_per_skill_tools(
    store: SkillStore,
    *,
    namespace: str | None = "skills",
    include: str | Iterable[str] | None = None,
    exclude: str | Iterable[str] | None = None,
    limit: int | None = None,
) -> list[Tool]:
    """Build one activation tool per registered skill.

    Args:
        store: Skill store the tools call.
        namespace: Namespace applied by the registry.  When ``None``, names
            are prefixed with ``skill_`` to avoid collisions.
        include: Only build tools for names matching these glob patterns.
        exclude: Skip names matching these glob patterns.
        limit: Maximum number of tools to build.

    Returns:
        Unregistered tools.  Pass them to :meth:`ToolRegistry.register`.
    """
    skills = store.list(include=include, exclude=exclude)
    if limit is not None:
        skills = skills[: max(0, limit)]
    tools: list[Tool] = []
    budget = _method_budget(namespace)
    used: set[str] = set()
    for skill in skills:
        fallback = skill.path.name if skill.path is not None else None
        base = _method_name_for_skill(skill.name, fallback=fallback)
        prefix = "skill_" if (not namespace or base in CATALOG_TOOL_METHODS) else ""
        method = _unique_method(base, skill.name, used, budget, prefix=prefix)
        tools.append(_make_skill_tool(store, skill, method))
    return tools


def build_skill_tools(
    store: SkillStore,
    *,
    mode: SkillToolMode = "catalog",
    namespace: str | None = "skills",
    include: str | Iterable[str] | None = None,
    exclude: str | Iterable[str] | None = None,
    limit: int | None = None,
) -> list[Tool]:
    """Build skill tools for one of the supported usage modes.

    Args:
        store: Skill store the tools call.
        mode: ``"catalog"`` for list/get/read, ``"per-skill"`` for one
            activation tool per skill, or ``"both"``.
        namespace: Namespace applied by the registry.
        include: Per-skill mode only; only build matching names.
        exclude: Per-skill mode only; skip matching names.
        limit: Per-skill mode only; maximum number of tools.

    Returns:
        Unregistered tools.

    Raises:
        ValueError: If *mode* is unknown.
    """
    if mode == "catalog":
        return build_catalog_tools(store, namespace=namespace)
    if mode == "per-skill":
        return build_per_skill_tools(
            store,
            namespace=namespace,
            include=include,
            exclude=exclude,
            limit=limit,
        )
    if mode == "both":
        return [
            *build_catalog_tools(store, namespace=namespace),
            *build_per_skill_tools(
                store,
                namespace=namespace,
                include=include,
                exclude=exclude,
                limit=limit,
            ),
        ]
    raise ValueError(
        f"Unknown skill tool mode {mode!r}; expected 'catalog', 'per-skill', or 'both'."
    )


def _make_skill_tool(store: SkillStore, skill: Skill, method: str) -> Tool:
    """Build one no-argument activation tool for *skill*."""
    skill_name = skill.name
    description = skill.description

    def activate() -> str:
        """Activate this skill and return its instructions."""
        return store.activate(skill_name)

    doc = (
        f"Activate the '{skill_name}' Agent Skill.\n\n"
        f"{description}\n\n"
        "Returns the skill's instructions and a list of bundled resources."
    )
    return Tool.from_function(
        activate,
        name=method,
        description=doc,
        metadata=ToolMetadata(
            tags={ToolTag.READ_ONLY},
            source="skills",
            source_detail=str(skill.path or ""),
        ),
    )

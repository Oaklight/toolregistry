"""Skills mixin for ToolRegistry."""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .._names import NameSeparator, qualified_tool_name
from ..skills import (
    Skill,
    SkillResource,
    SkillStore,
    SkillToolMode,
    build_skill_tools,
)

if TYPE_CHECKING:
    from ..config import SkillSource, ToolConfig
    from ..tool import Tool


class SkillsMixin:
    """Mixin exposing Agent Skills through a ``ToolRegistry``.

    The store is created lazily, so constructing a registry does not touch the
    filesystem.  The first skill call resolves the standard roots and loads
    nothing until :meth:`discover_skills` or :meth:`reload_skills` runs.
    """

    if TYPE_CHECKING:
        _tools: dict[str, Tool]
        name_sep: NameSeparator

        def register(
            self,
            target: Any,
            *,
            namespace: bool | str | None = None,
            **kwargs: Any,
        ) -> Any: ...

        def _unregister(self, name: str) -> bool: ...

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._skill_store: SkillStore | None = None
        self._skill_tool_names: set[str] = set()

    # -- state --------------------------------------------------------------

    @property
    def skills(self) -> SkillStore:
        """The lazily created skill store."""
        if self._skill_store is None:
            self._skill_store = SkillStore()
        return self._skill_store

    @property
    def skill_tools(self) -> list[str]:
        """Names of the tools currently registered by :meth:`register_skill_tools`."""
        return sorted(self._skill_tool_names)

    # -- loading ------------------------------------------------------------

    def discover_skills(
        self,
        *paths: str | Path,
        recursive: bool | None = None,
        override: bool = True,
        include: str | Iterable[str] | None = None,
        exclude: str | Iterable[str] | None = None,
    ) -> list[Skill]:
        """Discover skills from explicit paths or the standard roots.

        Args:
            *paths: Skill directories, ``SKILL.md`` files, or roots containing
                skill directories.  Omit to scan the standard roots.
            recursive: Walk nested directories.  ``None`` uses each root's
                setting, or the store default for explicit paths.
            override: Let a later skill replace an earlier one with the same
                name.
            include: Only register names matching these glob patterns.
            exclude: Skip names matching these glob patterns.

        Returns:
            Newly registered skills.
        """
        return self.skills.discover(
            *paths,
            recursive=recursive,
            override=override,
            include=include,
            exclude=exclude,
        )

    def reload_skills(self) -> list[Skill]:
        """Re-run the last skill discovery call from disk."""
        return self.skills.reload()

    def add_skill(
        self,
        skill: Skill | str | Path,
        *,
        override: bool = False,
    ) -> Skill:
        """Register a parsed skill or load one from a path."""
        return self.skills.add(skill, override=override)

    def remove_skill(self, name: str) -> None:
        """Remove a registered skill by name."""
        self.skills.remove(name)

    def clear_skills(self) -> None:
        """Remove every registered skill."""
        self.skills.clear()

    # -- lookup -------------------------------------------------------------

    def get_skill(self, name: str) -> Skill | None:
        """Return a skill by name, or ``None`` when it is not registered."""
        return self.skills.find(name)

    def list_skills(
        self,
        *,
        include: str | Iterable[str] | None = None,
        exclude: str | Iterable[str] | None = None,
    ) -> list[Skill]:
        """Return registered skills sorted by name."""
        return self.skills.list(include=include, exclude=exclude)

    def select_skills(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        available_tools: Iterable[str] | None = None,
        include: str | Iterable[str] | None = None,
        exclude: str | Iterable[str] | None = None,
    ):
        """Rank registered skills by relevance to *query*.

        Returns:
            ``SelectionResult`` objects sorted by descending score.
        """
        return self.skills.select(
            query,
            top_k=top_k,
            min_score=min_score,
            available_tools=available_tools,
            include=include,
            exclude=exclude,
        )

    # -- prompt and resource access ----------------------------------------

    def skills_catalog(self, names: Iterable[str] | None = None) -> str:
        """Return the Level-1 ``<available_skills>`` catalog."""
        return self.skills.catalog(names)

    def activate_skill(
        self,
        name: str,
        *,
        inline_resources: bool = False,
        max_inline_bytes: int = 64 * 1024,
    ) -> str:
        """Return a skill's Level-2 activation prompt."""
        return self.skills.activate(
            name,
            inline_resources=inline_resources,
            max_inline_bytes=max_inline_bytes,
        )

    def read_skill_resource(
        self,
        name: str,
        path: str,
        *,
        max_bytes: int = 64 * 1024,
        encoding: str = "utf-8",
    ) -> str:
        """Read a text resource bundled with a skill, safely."""
        return self.skills.read_resource(
            name,
            path,
            max_bytes=max_bytes,
            encoding=encoding,
        )

    def skill_resources(self, name: str) -> list[SkillResource]:
        """List files bundled with a skill."""
        return self.skills.resources(name)

    def skill_status(self) -> list[dict[str, Any]]:
        """Return JSON-serializable skill records for the admin panel."""
        return self.skills.status()

    def validate_skills(self, target: str | Path | None = None) -> dict[str, list[str]]:
        """Return Agent Skills spec problems keyed by skill name or path."""
        return self.skills.validate(target)

    # -- tool exposure ------------------------------------------------------

    def register_skill_tools(
        self,
        *,
        mode: SkillToolMode = "catalog",
        namespace: str | None = "skills",
        include: str | Iterable[str] | None = None,
        exclude: str | Iterable[str] | None = None,
        limit: int | None = None,
        replace: bool = True,
    ) -> list[str]:
        """Register tools that expose skills to a model.

        Args:
            mode: ``"catalog"`` for list/get/read tools, ``"per-skill"`` for
                one activation tool per skill, or ``"both"``.
            namespace: Namespace applied to the tools.  ``None`` keeps the
                unscoped method names.
            include: Per-skill mode only; only build matching names.
            exclude: Per-skill mode only; skip matching names.
            limit: Per-skill mode only; maximum number of tools.
            replace: Remove the tools from a previous call first.  Pass
                ``False`` to add another source's tools to the existing set.

        Returns:
            Registered tool names.
        """
        if replace:
            self.unregister_skill_tools()
        tools = build_skill_tools(
            self.skills,
            mode=mode,
            namespace=namespace,
            include=include,
            exclude=exclude,
            limit=limit,
        )
        names: list[str] = []
        seen: set[str] = set()
        for tool in tools:
            name = qualified_tool_name(tool.name, namespace, self.name_sep)
            if name in seen:
                warnings.warn(
                    f"Skill tool name {name!r} collides with another skill; "
                    "rename one skill to expose both.",
                    UserWarning,
                    stacklevel=2,
                )
                continue
            seen.add(name)
            self.register(tool, namespace=namespace, allow_replace=not replace)
            self._skill_tool_names.add(name)
            names.append(name)
        return names

    def unregister_skill_tools(self) -> list[str]:
        """Remove the tools registered by :meth:`register_skill_tools`.

        A tool that another registration replaced is left alone: only tools
        whose metadata still names ``skills`` as their source are removed.
        """
        removed: list[str] = []
        for name in sorted(self._skill_tool_names):
            tool = self._tools.get(name)
            if tool is not None and getattr(tool.metadata, "source", "") != "skills":
                continue
            if self._unregister(name):
                removed.append(name)
        self._skill_tool_names.clear()
        return removed

    # -- declarative configuration -----------------------------------------

    def apply_skill_config(
        self,
        config: ToolConfig | Iterable[SkillSource],
        *,
        register_tools: bool = True,
    ) -> list[Skill]:
        """Apply skill sources from a loaded ``ToolConfig`` or source iterable.

        Relative paths are resolved against the config file's directory, so a
        config committed beside its skills behaves the same from any working
        directory.

        Args:
            config: A ``ToolConfig`` or an iterable of ``SkillSource``.
            register_tools: Register the tool set declared by each source's
                ``expose`` field.  Pass ``False`` to load skills only.

        Returns:
            All skills discovered across the enabled sources.
        """
        if hasattr(config, "skills"):
            sources = cast("Iterable[SkillSource]", getattr(config, "skills"))
            source_path = cast(str, getattr(config, "source", ""))
        else:
            sources = cast("Iterable[SkillSource]", config)
            source_path = ""
        base = Path(source_path).expanduser().resolve().parent if source_path else None

        discovered: list[Skill] = []
        first_tool_source = True
        for source in sources:
            if not source.enabled:
                continue
            paths = _resolve_skill_paths(source.paths, base)
            discovered.extend(
                self.discover_skills(
                    *paths,
                    recursive=source.recursive,
                    override=source.override,
                    include=source.include or None,
                    exclude=source.exclude or None,
                )
            )
            if register_tools and source.expose != "none":
                self.register_skill_tools(
                    mode=source.expose,
                    namespace=source.namespace,
                    include=source.include or None,
                    exclude=source.exclude or None,
                    limit=source.limit,
                    replace=first_tool_source,
                )
                first_tool_source = False
        return discovered


def _resolve_skill_paths(paths: Iterable[str], base: Path | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if base is not None and not path.is_absolute():
            path = base / path
        resolved.append(path)
    return resolved

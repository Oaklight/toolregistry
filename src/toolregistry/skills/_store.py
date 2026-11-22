"""Skill discovery, storage, and progressive-disclosure helpers.

The parsing and ranking primitives come from the vendored zerodep ``skills``
module.  This layer adds the pieces a tool registry needs:

* standard user/project/system roots with deterministic precedence;
* include/exclude filters;
* safe access to a skill's bundled resources;
* catalog and activation prompt generation;
* status records for the admin panel.
"""

from __future__ import annotations

import builtins
import fnmatch
import html
import os
import warnings
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from .._vendor.skills import (
    BM25Selector,
    KeywordSelector,
    ParseError,
    SelectionResult,
    Selector,
    Skill,
    SkillError,
    SkillRegistry,
    ValidationError,
    compose as _compose_skills,
    filter_compatible,
    validate as _validate_path,
)

__all__ = [
    "DEFAULT_MAX_INLINE_BYTES",
    "DEFAULT_MAX_RESOURCE_BYTES",
    "SkillNotFoundError",
    "SkillResource",
    "SkillResourceError",
    "SkillRoot",
    "SkillStore",
    "default_skill_roots",
    "skill_to_dict",
]

#: Default cap for a single resource read, in bytes.
DEFAULT_MAX_RESOURCE_BYTES = 64 * 1024

#: Default cap for inlining resource content into an activation prompt.
DEFAULT_MAX_INLINE_BYTES = 64 * 1024

#: Hard cap on a caller-supplied resource read, so a skill cannot make the
#: process load an arbitrarily large file in one tool call.
MAX_RESOURCE_BYTES = 10 * 1024 * 1024

_RESOURCE_DIRS = {"scripts": "script", "references": "reference", "assets": "asset"}

Scope = Literal["system", "user", "project", "custom"]
ResourceKind = Literal["script", "reference", "asset", "other"]


class SkillNotFoundError(SkillError):
    """Raised when a skill name is not registered."""


class SkillResourceError(SkillError):
    """Raised when a skill resource is missing, unsafe, or too large."""


@dataclass(frozen=True)
class SkillRoot:
    """A directory that may contain skills.

    Attributes:
        path: Root directory.  Expanded and resolved on construction.
        scope: Precedence bucket used for reporting.  Lower-precedence
            buckets are discovered first.
        recursive: Whether to walk nested directories under *path*.
    """

    path: Path
    scope: Scope = "custom"
    recursive: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).expanduser().resolve())


@dataclass(frozen=True)
class SkillResource:
    """A file bundled with a skill.

    Attributes:
        path: Path relative to the skill directory, using ``/`` separators.
        kind: ``"script"``, ``"reference"``, ``"asset"``, or ``"other"``.
        size: File size in bytes.
    """

    path: str
    kind: ResourceKind
    size: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize the resource description."""
        return {"path": self.path, "kind": self.kind, "size": self.size}


@dataclass
class _DiscoveryCall:
    """One ``discover()`` call, retained so ``reload()`` can replay it."""

    specs: tuple[tuple[Path, bool], ...]
    override: bool
    include: tuple[str, ...] | None
    exclude: tuple[str, ...] | None
    errors: dict[str, str] = field(default_factory=dict)


def default_skill_roots(
    project_dir: str | Path | None = None,
) -> builtins.list[SkillRoot]:
    """Return the conventional Agent Skills roots, lowest precedence first.

    The order is deliberate: later roots replace earlier skills with the same
    name when discovery uses ``override=True``, so a project skill beats a
    user skill and a user skill beats a system skill.

    Supported defaults:

    * ``/etc/agents/skills`` and ``/usr/local/share/agents/skills`` (system)
    * ``~/.codex/skills/.system`` (system)
    * ``~/.agents/skills``, ``~/.codex/skills``, ``~/.claude/skills`` (user)
    * ``<project>/.claude/skills``, ``<project>/.codex/skills``,
      ``<project>/.agents/skills`` (project)

    ``TOOLREGISTRY_SKILLS_PATH`` (or ``AGENT_SKILLS_PATH``) adds
    ``os.pathsep``-separated custom roots with the highest precedence.

    Args:
        project_dir: Project root.  Defaults to the current working directory.

    Returns:
        Existing and conventional roots, deduplicated by resolved path.
    """
    home = Path.home()
    project = Path(project_dir or Path.cwd()).expanduser().resolve()

    candidates: builtins.list[tuple[Path, Scope, bool]] = [
        (Path("/etc/agents/skills"), "system", True),
        (Path("/usr/local/share/agents/skills"), "system", True),
        (home / ".codex" / "skills" / ".system", "system", True),
        (home / ".agents" / "skills", "user", True),
        (home / ".codex" / "skills", "user", True),
        (home / ".claude" / "skills", "user", True),
        (project / ".claude" / "skills", "project", True),
        (project / ".codex" / "skills", "project", True),
        (project / ".agents" / "skills", "project", True),
    ]

    extra = os.environ.get("TOOLREGISTRY_SKILLS_PATH") or os.environ.get(
        "AGENT_SKILLS_PATH"
    )
    if extra:
        candidates.extend(
            (Path(entry), "custom", True)
            for entry in extra.split(os.pathsep)
            if entry.strip()
        )

    roots: builtins.list[SkillRoot] = []
    seen: set[Path] = set()
    for path, scope, recursive in candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(SkillRoot(resolved, scope=scope, recursive=recursive))
    return roots


class SkillStore:
    """A mutable store of parsed skills with discovery and prompt helpers.

    The store is lazy: constructing it does not touch the filesystem.  Call
    :meth:`discover` (or :meth:`reload`) to populate it.

    Args:
        roots: Roots used when :meth:`discover` is called without paths.
            Defaults to :func:`default_skill_roots`.
        recursive: Default recursive flag for explicit paths.
        selector: Default selector for :meth:`select`.  When omitted, BM25 is
            used if available, otherwise keyword overlap.
    """

    def __init__(
        self,
        *,
        roots: Iterable[SkillRoot | str | Path] | None = None,
        recursive: bool = True,
        selector: Selector | None = None,
    ) -> None:
        self._registry = SkillRegistry()
        self._recursive = recursive
        self._selector = selector
        raw_roots: Iterable[SkillRoot | str | Path]
        if roots is None:
            raw_roots = default_skill_roots()
        else:
            raw_roots = roots
        self._roots: builtins.list[SkillRoot] = [
            root if isinstance(root, SkillRoot) else SkillRoot(Path(root))
            for root in raw_roots
        ]
        self._last_calls: builtins.list[_DiscoveryCall] | None = None
        self._last_errors: dict[str, str] = {}

    # -- registry surface ---------------------------------------------------

    @property
    def registry(self) -> SkillRegistry:
        """The underlying zerodep registry."""
        return self._registry

    @property
    def roots(self) -> builtins.list[SkillRoot]:
        """Configured discovery roots."""
        return list(self._roots)

    def __len__(self) -> int:
        return len(self._registry)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.find(name) is not None

    def __iter__(self) -> Iterator[Skill]:
        return iter(sorted(self._registry, key=lambda skill: skill.name))

    def names(self) -> builtins.list[str]:
        """Return registered skill names in sorted order."""
        return sorted(skill.name for skill in self._registry)

    @property
    def discovery_errors(self) -> dict[str, str]:
        """Skills that failed to load, keyed by directory path.

        ``discover()`` skips a directory whose ``SKILL.md`` cannot be parsed
        or validated.  This mapping keeps the reason so a typo does not make
        a skill disappear silently.  It is refreshed by the next
        :meth:`discover` or :meth:`reload`.
        """
        return dict(self._last_errors)

    def find(self, name: str) -> Skill | None:
        """Return a skill by name, accepting ``_``/``-`` and case variants."""
        canonical = self._canonical_name(name)
        if canonical is None:
            return None
        return self._registry.get(canonical)

    def get(self, name: str) -> Skill:
        """Return a registered skill.

        Raises:
            SkillNotFoundError: If *name* is not registered.
        """
        canonical = self._canonical_name(name)
        if canonical is None:
            registered = ", ".join(self.names()) or "<none>"
            raise SkillNotFoundError(
                f"Skill {name!r} is not registered. Registered skills: {registered}"
            )
        return self._registry.get(canonical)

    def list(
        self,
        *,
        include: str | Iterable[str] | None = None,
        exclude: str | Iterable[str] | None = None,
    ) -> builtins.list[Skill]:
        """Return registered skills, optionally filtered by name glob.

        Args:
            include: Only keep skills whose name matches one of these
                ``fnmatch`` patterns.
            exclude: Drop skills whose name matches one of these patterns.

        Returns:
            Skills sorted by name.
        """
        skills = sorted(self._registry, key=lambda skill: skill.name)
        return _filter_skills(skills, include=include, exclude=exclude)

    # -- mutation -----------------------------------------------------------

    def add(self, skill: Skill | str | Path, *, override: bool = False) -> Skill:
        """Register a parsed skill or load one from a path.

        Args:
            skill: A ``Skill`` instance, a skill directory, or a
                ``SKILL.md`` file.
            override: Replace an existing skill with the same name.

        Returns:
            The registered skill.

        Raises:
            SkillError: If the path cannot be parsed.
            ValueError: If the name is already registered and *override* is
                false.
        """
        if not isinstance(skill, Skill):
            skill = Skill.load(skill)
        self._registry.register(skill, override=override)
        return skill

    def remove(self, name: str) -> None:
        """Remove a registered skill.

        Raises:
            SkillNotFoundError: If *name* is not registered.
        """
        skill = self.get(name)
        self._registry.unregister(skill.name)

    def clear(self) -> None:
        """Remove every registered skill and forget the discovery plan."""
        self._registry = SkillRegistry()
        self._last_calls = None
        self._last_errors = {}

    # -- discovery ----------------------------------------------------------

    def discover(
        self,
        *paths: str | Path,
        recursive: bool | None = None,
        override: bool = True,
        include: str | Iterable[str] | None = None,
        exclude: str | Iterable[str] | None = None,
        append: bool = False,
    ) -> builtins.list[Skill]:
        """Discover skills and register them.

        Args:
            *paths: Skill directories, ``SKILL.md`` files, or roots containing
                skill directories.  When omitted, the configured roots are
                used.
            recursive: Walk nested directories.  ``None`` uses each root's
                setting, or the store default for explicit paths.
            override: Let a later skill replace an earlier one with the same
                name.  Pass ``False`` to keep the first definition.
            include: Only register names matching these glob patterns.
            exclude: Skip names matching these glob patterns.
            append: Keep earlier calls in the reload plan instead of replacing
                it.  :meth:`apply_skill_config` uses this so a multi-source
                configuration reloads every source.

        Returns:
            Newly registered skills, in discovery order.
        """
        specs = tuple(self._discovery_specs(paths, recursive))
        raw, errors = self._collect(specs, override)
        call = _DiscoveryCall(
            specs=specs,
            override=override,
            include=_as_patterns(include),
            exclude=_as_patterns(exclude),
            errors=errors,
        )
        if append and self._last_calls:
            self._last_calls.append(call)
        else:
            self._last_calls = [call]
        self._last_errors = self._merged_errors()

        registered = self._register_skills(
            _filter_skills(raw, include=include, exclude=exclude),
            override=override,
        )
        self._warn_discovery_errors()
        return registered

    def reload(self) -> builtins.list[Skill]:
        """Re-run every retained discovery call from disk.

        Skills added individually with :meth:`add` are not retained.  When no
        discovery call has run yet, this discovers from the configured roots.
        Each source keeps its own paths, ``recursive`` flag, filters, and
        override setting.
        """
        calls = list(self._last_calls) if self._last_calls else []
        if not calls:
            self.clear()
            return self.discover()
        self._registry = SkillRegistry()
        registered: builtins.list[Skill] = []
        for call in calls:
            raw, errors = self._collect(call.specs, call.override)
            call.errors = errors
            registered.extend(
                self._register_skills(
                    _filter_skills(raw, include=call.include, exclude=call.exclude),
                    override=call.override,
                )
            )
        self._last_calls = calls
        self._last_errors = self._merged_errors()
        self._warn_discovery_errors()
        return registered

    def _collect(
        self,
        specs: Iterable[tuple[Path, bool]],
        override: bool,
    ) -> tuple[builtins.list[Skill], dict[str, str]]:
        """Load skills and record the directories that failed."""
        probe = SkillRegistry()
        raw: builtins.list[Skill] = []
        errors: dict[str, str] = {}
        for path, recursive_flag in specs:
            for candidate in _candidate_dirs(path, recursive_flag):
                try:
                    Skill.load(candidate)
                except (ParseError, ValidationError, OSError) as exc:
                    errors[str(candidate)] = str(exc)
            raw.extend(
                probe.discover(
                    path,
                    recursive=recursive_flag,
                    override=override,
                )
            )
        return raw, errors

    def _register_skills(
        self,
        skills: Iterable[Skill],
        *,
        override: bool,
    ) -> builtins.list[Skill]:
        registered: builtins.list[Skill] = []
        for skill in skills:
            if skill.name in self._registry and not override:
                continue
            self._registry.register(skill, override=override)
            registered.append(skill)
        return registered

    def _merged_errors(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for call in self._last_calls or []:
            merged.update(call.errors)
        return merged

    def _warn_discovery_errors(self) -> None:
        if self._last_errors:
            warnings.warn(
                f"{len(self._last_errors)} skill(s) failed to load; "
                "call discovery_errors for details.",
                UserWarning,
                stacklevel=3,
            )

    # -- selection ----------------------------------------------------------

    def select(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        available_tools: Iterable[str] | None = None,
        selector: Selector | None = None,
        include: str | Iterable[str] | None = None,
        exclude: str | Iterable[str] | None = None,
    ) -> builtins.list[SelectionResult]:
        """Rank skills by relevance to *query*.

        Args:
            query: User query or task description.
            top_k: Maximum number of results.
            min_score: Drop results below this score.
            available_tools: Tool names available to the model.  Skills with
                ``allowed-tools`` that are not satisfied are skipped.
            selector: Selection strategy.  Defaults to the store selector,
                then BM25 if available, then keyword overlap.
            include: Only consider names matching these glob patterns.
            exclude: Skip names matching these glob patterns.

        Returns:
            Results sorted by descending score.
        """
        skills = self.list(include=include, exclude=exclude)
        if available_tools is not None:
            skills = filter_compatible(skills, available_tools)
        if not skills:
            return []

        chosen = selector or self._selector
        if chosen is None:
            try:
                chosen = BM25Selector()
            except ImportError:
                chosen = KeywordSelector()

        results = chosen.select(query, skills, top_k)
        if min_score > 0.0:
            results = [result for result in results if result.score >= min_score]
        return results

    # -- prompt generation --------------------------------------------------

    def catalog(
        self,
        names: Iterable[str] | None = None,
        *,
        include_locations: bool = False,
    ) -> str:
        """Return the Level-1 ``<available_skills>`` catalog.

        Args:
            names: Optional names to include.  When omitted, every registered
                skill is included.  The output is sorted by name for stable
                prompt caching.
            include_locations: Emit a ``<location>`` for each skill.  Disabled
                by default because the value is prompt text: an absolute path
                would disclose the user's home layout.  When enabled, a path
                under the home directory is written as ``~/…``; other paths
                keep only the file name.
        """
        if names is None:
            skills = sorted(self._registry, key=lambda skill: skill.name)
        else:
            skills = sorted((self.get(name) for name in names), key=lambda s: s.name)
        return _format_catalog(skills, include_locations=include_locations)

    def activate(
        self,
        name: str,
        *,
        inline_resources: bool = False,
        max_inline_bytes: int = DEFAULT_MAX_INLINE_BYTES,
    ) -> str:
        """Return the Level-2 activation prompt for one skill.

        Args:
            name: Registered skill name.
            inline_resources: Include small resource files in the prompt.
                Every top-level bundled file is validated first, so a symlink
                that escapes the skill directory raises
                :class:`SkillResourceError` instead of leaking content.
            max_inline_bytes: Maximum size of each inlined file.

        Raises:
            SkillResourceError: If an inlined resource escapes the skill
                directory.
        """
        if max_inline_bytes <= 0:
            raise ValueError("max_inline_bytes must be positive")
        skill = self.get(name)
        if inline_resources:
            _ensure_inline_resources_safe(skill)
        return skill.to_prompt(
            inline_resources=inline_resources,
            max_inline_bytes=max_inline_bytes,
        )

    def compose(self, *names: str) -> str:
        """Return the activation prompts of several skills, joined."""
        return _compose_skills(*(self.get(name) for name in names))

    # -- resources ----------------------------------------------------------

    def resources(self, name: str) -> builtins.list[SkillResource]:
        """List files under ``scripts/``, ``references/``, and ``assets/``.

        The walk is recursive for inspection and admin display.  Files whose
        resolved path escapes the skill directory are omitted, matching the
        policy of :meth:`read_resource`.  The activation prompt only inlines
        top-level files.
        """
        skill = self.get(name)
        root = _skill_root(skill)
        found: builtins.list[SkillResource] = []
        for dirname in _RESOURCE_DIRS:
            directory = root / dirname
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    relative = path.relative_to(root).as_posix()
                    _safe_resource_path(root, relative)
                except (ValueError, SkillResourceError):
                    continue
                kind = cast(ResourceKind, _RESOURCE_DIRS[relative.split("/", 1)[0]])
                found.append(
                    SkillResource(
                        path=relative,
                        kind=kind,
                        size=_file_size(path),
                    )
                )
        return found

    def read_resource(
        self,
        name: str,
        path: str,
        *,
        max_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
        encoding: str = "utf-8",
    ) -> str:
        """Read one text file bundled with a skill.

        The path is resolved inside the skill directory.  Absolute paths,
        ``..`` segments, and symlinks that escape the directory are rejected.

        Args:
            name: Registered skill name.
            path: Skill-relative path, for example ``references/guide.md``.
            max_bytes: Maximum accepted file size.
            encoding: Text encoding used to decode the file.

        Returns:
            File content.

        Raises:
            SkillResourceError: If the path is unsafe, the file is missing,
                too large, or cannot be decoded as text.
        """
        if max_bytes <= 0 or max_bytes > MAX_RESOURCE_BYTES:
            raise ValueError(
                f"max_bytes must be between 1 and {MAX_RESOURCE_BYTES}, got {max_bytes}"
            )
        skill = self.get(name)
        root = _skill_root(skill)
        target = _safe_resource_path(root, path)
        if not target.is_file():
            raise SkillResourceError(
                f"Skill {skill.name!r} has no resource file at {path!r}"
            )
        size = _file_size(target)
        if size > max_bytes:
            raise SkillResourceError(
                f"Resource {path!r} is {size} bytes, above the {max_bytes}-byte limit"
            )
        try:
            return target.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            raise SkillResourceError(
                f"Resource {path!r} is not valid {encoding} text; "
                "use resources() to inspect binary assets"
            ) from exc
        except OSError as exc:
            raise SkillResourceError(f"Cannot read resource {path!r}: {exc}") from exc

    # -- validation and reporting ------------------------------------------

    def validate(
        self,
        target: str | Path | None = None,
        *,
        include_failures: bool = False,
    ) -> dict[str, builtins.list[str]]:
        """Return spec problems keyed by skill name or path.

        Args:
            target: A registered skill name or a filesystem path.  When
                omitted, every registered skill with a directory is checked.
            include_failures: When *target* is omitted, also report skills
                that failed to load during the last discovery.  Those names
                are unknown, so their directory path is the key.
        """
        if target is None:
            results: dict[str, builtins.list[str]] = {}
            for skill in self:
                if skill.path is None:
                    results[skill.name] = [
                        "Skill was parsed from text and has no directory to validate"
                    ]
                else:
                    results[skill.name] = _validate_path(skill.path)
            if include_failures:
                for path, message in self._last_errors.items():
                    results.setdefault(path, []).append(message)
            return results

        path = Path(target).expanduser()
        if path.exists():
            return {str(path): _validate_path(path)}

        skill = self.get(str(target))
        if skill.path is None:
            return {
                skill.name: [
                    "Skill was parsed from text and has no directory to validate"
                ]
            }
        return {skill.name: _validate_path(skill.path)}

    def status(
        self, *, include_resources: bool = True
    ) -> builtins.list[dict[str, Any]]:
        """Return JSON-serializable records for the admin panel."""
        records: builtins.list[dict[str, Any]] = []
        for skill in self:
            record = skill_to_dict(skill, include_instructions=False)
            record["scope"] = self._scope_for(skill)
            if include_resources:
                record["resources"] = [
                    resource.to_dict() for resource in self.resources(skill.name)
                ]
            records.append(record)
        return records

    # -- internal -----------------------------------------------------------

    def _canonical_name(self, name: str) -> str | None:
        if not isinstance(name, str):
            return None
        if name in self._registry:
            return name
        folded = name.strip().lower().replace("_", "-")
        for skill in self._registry:
            if skill.name.lower().replace("_", "-") == folded:
                return skill.name
        return None

    def _discovery_specs(
        self,
        paths: tuple[str | Path, ...],
        recursive: bool | None,
    ) -> builtins.list[tuple[Path, bool]]:
        if paths:
            return [
                (
                    Path(path).expanduser().resolve(),
                    self._recursive if recursive is None else recursive,
                )
                for path in paths
            ]
        specs: builtins.list[tuple[Path, bool]] = []
        for root in self._roots:
            specs.append(
                (root.path, root.recursive if recursive is None else recursive)
            )
        return specs

    def _scope_for(self, skill: Skill) -> Scope:
        if skill.path is None:
            return "custom"
        resolved = skill.path.resolve()
        for root in self._roots:
            try:
                resolved.relative_to(root.path)
            except ValueError:
                continue
            return root.scope
        return "custom"


def skill_to_dict(
    skill: Skill,
    *,
    include_instructions: bool = True,
) -> dict[str, Any]:
    """Convert a skill to a JSON-serializable dictionary."""
    data = skill.to_dict()
    if not include_instructions:
        data.pop("instructions", None)
    return data


def _as_patterns(value: str | Iterable[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _filter_skills(
    skills: Iterable[Skill],
    *,
    include: str | Iterable[str] | None = None,
    exclude: str | Iterable[str] | None = None,
) -> builtins.list[Skill]:
    include_patterns = _as_patterns(include)
    exclude_patterns = _as_patterns(exclude)
    result: builtins.list[Skill] = []
    for skill in skills:
        if include_patterns and not any(
            fnmatch.fnmatchcase(skill.name, pattern) for pattern in include_patterns
        ):
            continue
        if exclude_patterns and any(
            fnmatch.fnmatchcase(skill.name, pattern) for pattern in exclude_patterns
        ):
            continue
        result.append(skill)
    return result


def _skill_root(skill: Skill) -> Path:
    if skill.path is None:
        raise SkillResourceError(
            f"Skill {skill.name!r} was parsed from text and has no resources"
        )
    return skill.path.resolve()


def _has_skill_md(path: Path) -> bool:
    """Return whether *path* holds a ``SKILL.md`` in any accepted spelling."""
    for name in ("SKILL.md", "skill.md"):
        if (path / name).is_file():
            return True
    try:
        return any(
            entry.name.lower() == "skill.md" and entry.is_file()
            for entry in path.iterdir()
        )
    except OSError:
        return False


def _candidate_dirs(path: Path, recursive: bool) -> builtins.list[Path]:
    """Return the skill directories ``discover()`` will try to load.

    Mirrors the vendored precedence: a collection root's skill children win
    over the root's own ``SKILL.md``.
    """
    try:
        if not path.exists():
            return []
        if path.is_file():
            return [path.parent] if path.name.lower() == "skill.md" else []
        candidates = _walk_candidates(path, recursive)
        if not candidates and _has_skill_md(path):
            return [path]
        return candidates
    except OSError:
        return []


def _walk_candidates(path: Path, recursive: bool) -> builtins.list[Path]:
    """List child skill directories, recursively or one level deep."""
    if recursive:
        return list(_walk_skill_dirs(path))
    return [
        child
        for child in sorted(item for item in path.iterdir() if item.is_dir())
        if _has_skill_md(child)
    ]


def _walk_skill_dirs(root: Path) -> Iterator[Path]:
    """Yield directories below *root* that contain a ``SKILL.md``."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        current = Path(dirpath)
        if current == root:
            continue
        if any(name.lower() == "skill.md" for name in filenames):
            yield current
            dirnames.clear()


def _ensure_inline_resources_safe(skill: Skill) -> None:
    """Refuse to inline a resource that resolves outside the skill directory."""
    root = _skill_root(skill)
    for path in (*skill.scripts, *skill.references, *skill.assets):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise SkillResourceError(
                f"Resource {path} escapes the skill directory"
            ) from exc
        _safe_resource_path(root, relative)


def _format_catalog(
    skills: Iterable[Skill],
    *,
    include_locations: bool,
) -> str:
    """Build the Level-1 catalog without leaking absolute paths by default."""
    parts = ["<available_skills>"]
    for skill in skills:
        parts.append("<skill>")
        parts.append(f"<name>{html.escape(skill.name)}</name>")
        parts.append(f"<description>{html.escape(skill.description)}</description>")
        if include_locations and skill.path is not None:
            location = _display_location(skill.path)
            parts.append(f"<location>{html.escape(location)}</location>")
        parts.append("</skill>")
    parts.append("</available_skills>")
    return "\n".join(parts)


def _display_location(path: Path) -> str:
    """Return a ``~``-relative location, or only the file name outside home."""
    candidate = path
    for name in ("SKILL.md", "skill.md"):
        if (path / name).is_file():
            candidate = path / name
            break
    try:
        return "~/" + candidate.resolve().relative_to(Path.home()).as_posix()
    except (ValueError, OSError):
        return candidate.name


def _safe_resource_path(root: Path, relative: str) -> Path:
    if not relative or "\x00" in relative:
        raise SkillResourceError("Resource path must not be empty or contain NUL")
    candidate = Path(relative)
    if candidate.is_absolute():
        raise SkillResourceError(f"Resource path must be relative, got {relative!r}")
    if any(part == ".." for part in candidate.parts):
        raise SkillResourceError(
            f"Resource path must stay inside the skill directory, got {relative!r}"
        )
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SkillResourceError(
            f"Resource path escapes the skill directory: {relative!r}"
        ) from exc
    return resolved


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0

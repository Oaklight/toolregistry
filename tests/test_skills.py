"""Tests for Agent Skills loading, storage, and tool exposure."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from toolregistry import ToolRegistry
from toolregistry.config import SkillSource, ToolConfig, load_config, save_config
from toolregistry.skills import (
    KeywordSelector,
    SkillNotFoundError,
    SkillResourceError,
    SkillRoot,
    SkillStore,
    default_skill_roots,
)

SKILL_TEMPLATE = """\
---
name: {name}
description: {description}
{extra}---
# {title}

{body}
"""


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "A test skill",
    body: str = "Follow these instructions.",
    extra: str = "",
    resources: dict[str, str] | None = None,
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        SKILL_TEMPLATE.format(
            name=name,
            description=description,
            title=name.replace("-", " ").title(),
            body=body,
            extra=extra,
        ),
        encoding="utf-8",
    )
    for relative, content in (resources or {}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


# ---------------------------------------------------------------------------
# Roots and discovery
# ---------------------------------------------------------------------------


class TestRoots:
    def test_default_roots_report_scopes(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        project = tmp_path / "project"
        (home / ".agents" / "skills").mkdir(parents=True)
        (project / ".agents" / "skills").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("TOOLREGISTRY_SKILLS_PATH", raising=False)
        monkeypatch.delenv("AGENT_SKILLS_PATH", raising=False)

        roots = default_skill_roots(project)
        by_path = {str(root.path): root for root in roots}

        user = by_path[str((home / ".agents" / "skills").resolve())]
        project_root = by_path[str((project / ".agents" / "skills").resolve())]
        assert user.scope == "user"
        assert project_root.scope == "project"
        assert roots.index(user) < roots.index(project_root)

    def test_environment_paths_have_highest_precedence(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        home = tmp_path / "home"
        extra = tmp_path / "extra"
        home.mkdir()
        extra.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("TOOLREGISTRY_SKILLS_PATH", str(extra))
        monkeypatch.delenv("AGENT_SKILLS_PATH", raising=False)

        roots = default_skill_roots(tmp_path / "project")
        assert roots[-1].path == extra.resolve()
        assert roots[-1].scope == "custom"

    def test_duplicate_roots_are_removed(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("TOOLREGISTRY_SKILLS_PATH", str(home / ".agents" / "skills"))

        roots = default_skill_roots(tmp_path / "project")
        paths = [root.path for root in roots]
        assert len(paths) == len(set(paths))


class TestDiscovery:
    def test_discover_root(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha")
        _write_skill(tmp_path, "beta")

        store = SkillStore()
        found = store.discover(tmp_path)

        assert [skill.name for skill in found] == ["alpha", "beta"]
        assert store.names() == ["alpha", "beta"]

    def test_discover_direct_skill_directory(self, tmp_path: Path) -> None:
        directory = _write_skill(tmp_path, "alpha")

        store = SkillStore()
        assert [skill.name for skill in store.discover(directory)] == ["alpha"]

    def test_discover_direct_skill_md(self, tmp_path: Path) -> None:
        directory = _write_skill(tmp_path, "alpha")

        store = SkillStore()
        assert [skill.name for skill in store.discover(directory / "SKILL.md")] == [
            "alpha"
        ]

    def test_discover_ignores_missing_paths(self, tmp_path: Path) -> None:
        store = SkillStore()
        assert store.discover(tmp_path / "missing") == []

    def test_discover_recursive(self, tmp_path: Path) -> None:
        nested = tmp_path / "category"
        nested.mkdir()
        _write_skill(nested, "alpha")

        flat = SkillStore(recursive=False)
        assert flat.discover(tmp_path) == []
        assert [
            skill.name for skill in SkillStore().discover(tmp_path, recursive=True)
        ] == ["alpha"]

    def test_override_precedence(self, tmp_path: Path) -> None:
        low = tmp_path / "low"
        high = tmp_path / "high"
        _write_skill(low, "alpha", description="low description")
        _write_skill(high, "alpha", description="high description")

        store = SkillStore()
        store.discover(low, high, override=True)
        assert store.get("alpha").description == "high description"

        first_wins = SkillStore()
        first_wins.discover(low, high, override=False)
        assert first_wins.get("alpha").description == "low description"

    def test_include_and_exclude(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha-one")
        _write_skill(tmp_path, "alpha-two")
        _write_skill(tmp_path, "beta")

        included = SkillStore()
        included.discover(tmp_path, include="alpha-*")
        assert included.names() == ["alpha-one", "alpha-two"]

        excluded = SkillStore()
        excluded.discover(tmp_path, exclude="alpha-*")
        assert excluded.names() == ["beta"]

    def test_reload_reads_new_content(self, tmp_path: Path) -> None:
        directory = _write_skill(tmp_path, "alpha", description="first")
        store = SkillStore()
        store.discover(tmp_path)
        assert store.get("alpha").description == "first"

        (directory / "SKILL.md").write_text(
            SKILL_TEMPLATE.format(
                name="alpha",
                description="second",
                title="Alpha",
                body="Changed.",
                extra="",
            ),
            encoding="utf-8",
        )
        store.reload()
        assert store.get("alpha").description == "second"

    def test_name_alias_lookup(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha-skill")
        store = SkillStore()
        store.discover(tmp_path)

        assert store.get("alpha_skill").name == "alpha-skill"
        assert store.find("ALPHA-SKILL") is not None
        with pytest.raises(SkillNotFoundError):
            store.get("missing")


# ---------------------------------------------------------------------------
# Catalog, activation, and selection
# ---------------------------------------------------------------------------


class TestPrompts:
    def test_catalog_is_sorted(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "zeta")
        _write_skill(tmp_path, "alpha")
        store = SkillStore()
        store.discover(tmp_path)

        catalog = store.catalog()
        assert catalog.index("<name>alpha</name>") < catalog.index("<name>zeta</name>")
        assert "<location>" in catalog

    def test_activation_lists_resources(self, tmp_path: Path) -> None:
        _write_skill(
            tmp_path,
            "alpha",
            body="Do the thing.",
            resources={"references/guide.md": "GUIDE_BODY"},
        )
        store = SkillStore()
        store.discover(tmp_path)

        prompt = store.activate("alpha")
        assert "<skill_content" in prompt
        assert "references/guide.md" in prompt
        assert "GUIDE_BODY" not in prompt

        inlined = store.activate("alpha", inline_resources=True)
        assert "GUIDE_BODY" in inlined

    def test_compose(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha")
        _write_skill(tmp_path, "beta")
        store = SkillStore()
        store.discover(tmp_path)

        combined = store.compose("alpha", "beta")
        assert combined.count("<skill_content") == 2

    def test_select_relevance(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "pdf-processing", description="Extract PDF text")
        _write_skill(tmp_path, "code-review", description="Review code for bugs")
        store = SkillStore()
        store.discover(tmp_path)

        matches = store.select("extract text from PDF", selector=KeywordSelector())
        assert matches[0].skill.name == "pdf-processing"

    def test_select_filters_by_available_tools(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "needs-bash", extra="allowed-tools: Bash\n")
        _write_skill(tmp_path, "needs-read", extra="allowed-tools: Read\n")
        store = SkillStore()
        store.discover(tmp_path)

        matches = store.select(
            "needs bash read",
            selector=KeywordSelector(),
            available_tools=["Read"],
        )
        names = {match.skill.name for match in matches}
        assert "needs-bash" not in names
        assert "needs-read" in names


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


class TestResources:
    def test_resources_lists_nested_files(self, tmp_path: Path) -> None:
        _write_skill(
            tmp_path,
            "alpha",
            resources={
                "scripts/run.sh": "echo hi",
                "references/deep/guide.md": "guide",
                "assets/logo.txt": "logo",
            },
        )
        store = SkillStore()
        store.discover(tmp_path)

        resources = {item.path: item for item in store.resources("alpha")}
        assert resources["scripts/run.sh"].kind == "script"
        assert resources["references/deep/guide.md"].kind == "reference"
        assert resources["assets/logo.txt"].kind == "asset"

    def test_read_resource(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha", resources={"references/guide.md": "hello"})
        store = SkillStore()
        store.discover(tmp_path)

        assert store.read_resource("alpha", "references/guide.md") == "hello"

    def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha", resources={"references/guide.md": "hello"})
        store = SkillStore()
        store.discover(tmp_path)

        with pytest.raises(SkillResourceError, match="relative"):
            store.read_resource("alpha", str(tmp_path / "SKILL.md"))

    def test_parent_segment_rejected(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha", resources={"references/guide.md": "hello"})
        store = SkillStore()
        store.discover(tmp_path)

        with pytest.raises(SkillResourceError, match="inside"):
            store.read_resource("alpha", "../alpha/SKILL.md")

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("secret", encoding="utf-8")
        directory = _write_skill(tmp_path, "alpha", resources={"references/a.md": "a"})
        link = directory / "references" / "escape.md"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available")

        store = SkillStore()
        store.discover(tmp_path)
        with pytest.raises(SkillResourceError, match="escapes"):
            store.read_resource("alpha", "references/escape.md")

    def test_missing_resource(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha")
        store = SkillStore()
        store.discover(tmp_path)

        with pytest.raises(SkillResourceError, match="no resource"):
            store.read_resource("alpha", "references/missing.md")

    def test_oversized_resource(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha", resources={"references/big.md": "x" * 100})
        store = SkillStore()
        store.discover(tmp_path)

        with pytest.raises(SkillResourceError, match="above"):
            store.read_resource("alpha", "references/big.md", max_bytes=10)

    def test_binary_resource(self, tmp_path: Path) -> None:
        directory = _write_skill(tmp_path, "alpha")
        (directory / "assets").mkdir()
        (directory / "assets" / "blob.bin").write_bytes(b"\xff\xfe\x00")
        store = SkillStore()
        store.discover(tmp_path)

        with pytest.raises(SkillResourceError, match="not valid"):
            store.read_resource("alpha", "assets/blob.bin")


# ---------------------------------------------------------------------------
# Validation and status
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_skill(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha")
        store = SkillStore()
        store.discover(tmp_path)

        assert store.validate("alpha") == {"alpha": []}

    def test_name_mismatch_reported(self, tmp_path: Path) -> None:
        directory = tmp_path / "wrong-directory"
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            SKILL_TEMPLATE.format(
                name="alpha",
                description="desc",
                title="Alpha",
                body="body",
                extra="",
            ),
            encoding="utf-8",
        )
        store = SkillStore()
        store.discover(tmp_path)

        problems = store.validate("alpha")["alpha"]
        assert any("does not match directory" in problem for problem in problems)

    def test_status_shape(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha", resources={"references/guide.md": "guide"})
        store = SkillStore(roots=[SkillRoot(tmp_path, scope="project")])
        store.discover()

        status = store.status()
        assert status[0]["name"] == "alpha"
        assert status[0]["scope"] == "project"
        assert status[0]["resources"][0]["path"] == "references/guide.md"


# ---------------------------------------------------------------------------
# ToolRegistry integration
# ---------------------------------------------------------------------------


class TestRegistrySkills:
    def test_discover_and_activate(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha", body="Alpha instructions.")
        registry = ToolRegistry()

        assert [skill.name for skill in registry.discover_skills(tmp_path)] == ["alpha"]
        assert registry.get_skill("alpha") is not None
        assert registry.get_skill("missing") is None
        assert "Alpha instructions." in registry.activate_skill("alpha")
        assert "<name>alpha</name>" in registry.skills_catalog()

    def test_registry_is_lazy_until_first_skill_call(self) -> None:
        registry = ToolRegistry()
        assert registry._skill_store is None
        registry.skills_catalog()
        assert registry._skill_store is not None

    def test_catalog_tools(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha", body="Alpha instructions.")
        registry = ToolRegistry()
        registry.discover_skills(tmp_path)

        names = registry.register_skill_tools()
        assert names == ["skills-list", "skills-get", "skills-read_resource"]
        assert set(names) <= set(registry.list_tools())

        result = registry.invoke("skills-list", {})
        assert isinstance(result.result, str)
        assert "<name>alpha</name>" in result.result

        result = registry.invoke("skills-get", {"name": "alpha"})
        assert isinstance(result.result, str)
        assert "Alpha instructions." in result.result

    def test_catalog_tool_schema(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha")
        registry = ToolRegistry()
        registry.discover_skills(tmp_path)
        registry.register_skill_tools()

        list_params = registry.get_tool("skills-list").parameters
        assert set(list_params["properties"]) >= {"query", "limit"}
        get_params = registry.get_tool("skills-get").parameters
        assert "name" in get_params["properties"]

    def test_per_skill_tools(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha-skill", body="Alpha instructions.")
        registry = ToolRegistry()
        registry.discover_skills(tmp_path)

        names = registry.register_skill_tools(mode="per-skill")
        assert names == ["skills-alpha_skill"]
        result = registry.invoke("skills-alpha_skill", {})
        assert isinstance(result.result, str)
        assert "Alpha instructions." in result.result

    def test_both_modes_and_unregister(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha-skill")
        registry = ToolRegistry()
        registry.discover_skills(tmp_path)

        names = registry.register_skill_tools(mode="both")
        assert set(names) == {
            "skills-list",
            "skills-get",
            "skills-read_resource",
            "skills-alpha_skill",
        }
        removed = registry.unregister_skill_tools()
        assert set(removed) == set(names)
        assert registry.skill_tools == []
        assert not set(names) & set(registry.list_tools())

    def test_unscoped_tool_names(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha")
        registry = ToolRegistry()
        registry.discover_skills(tmp_path)

        assert registry.register_skill_tools(namespace=None)[0] == "list_skills"


# ---------------------------------------------------------------------------
# Declarative configuration
# ---------------------------------------------------------------------------


class TestSkillConfig:
    def test_yaml_config(self, tmp_path: Path) -> None:
        _write_skill(tmp_path / "skills", "alpha")
        config_path = tmp_path / "tools.yaml"
        config_path.write_text(
            """\
skills:
  - paths: [skills]
    recursive: true
    expose: catalog
    namespace: skills
""",
            encoding="utf-8",
        )

        config = load_config(config_path)
        assert len(config.skills) == 1
        assert config.skills[0].paths == ("skills",)
        assert config.skills[0].expose == "catalog"

    def test_single_mapping_accepted(self, tmp_path: Path) -> None:
        config_path = tmp_path / "tools.json"
        config_path.write_text('{"skills": {"path": "skills"}}', encoding="utf-8")

        config = load_config(config_path)
        assert config.skills[0].paths == ("skills",)

    def test_invalid_configs(self, tmp_path: Path) -> None:
        cases = [
            '{"skills": {"path": "a", "paths": ["b"]}}',
            '{"skills": [{"include": ["a"], "exclude": ["b"]}]}',
            '{"skills": [{"expose": "bogus"}]}',
            '{"skills": [{"unknown": true}]}',
            '{"skills": [{"recursive": "yes"}]}',
        ]
        for index, text in enumerate(cases):
            path = tmp_path / f"bad-{index}.json"
            path.write_text(text, encoding="utf-8")
            with pytest.raises(Exception):
                load_config(path)

    def test_round_trip(self, tmp_path: Path) -> None:
        config = ToolConfig(
            skills=(
                SkillSource(
                    paths=("skills",),
                    exclude=("draft-*",),
                    expose="per-skill",
                    namespace="skillz",
                    limit=5,
                ),
            )
        )
        path = tmp_path / "tools.yaml"
        save_config(config, path)

        reloaded = load_config(path)
        assert reloaded.skills == config.skills

    def test_apply_config_resolves_relative_to_config(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        project = tmp_path / "project"
        _write_skill(project / "skills", "alpha")
        config_path = project / "tools.yaml"
        config_path.write_text(
            "skills:\n  - path: skills\n    expose: catalog\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        config = load_config(config_path)
        registry = ToolRegistry()
        found = registry.apply_skill_config(config)

        assert [skill.name for skill in found] == ["alpha"]
        assert "skills-list" in registry.list_tools()

    def test_apply_config_can_skip_tools(self, tmp_path: Path) -> None:
        _write_skill(tmp_path / "skills", "alpha")
        config_path = tmp_path / "tools.yaml"
        config_path.write_text("skills:\n  - path: skills\n", encoding="utf-8")

        config = load_config(config_path)
        registry = ToolRegistry()
        registry.apply_skill_config(config, register_tools=False)

        assert registry.get_skill("alpha") is not None
        assert registry.skill_tools == []

    def test_apply_config_combines_multiple_sources(self, tmp_path: Path) -> None:
        _write_skill(tmp_path / "first", "alpha")
        _write_skill(tmp_path / "second", "beta")
        config_path = tmp_path / "tools.yaml"
        config_path.write_text(
            """\
skills:
  - path: first
    expose: per-skill
  - path: second
    expose: per-skill
""",
            encoding="utf-8",
        )

        config = load_config(config_path)
        registry = ToolRegistry()
        registry.apply_skill_config(config)

        assert {skill.name for skill in registry.list_skills()} == {"alpha", "beta"}
        assert set(registry.skill_tools) == {"skills-alpha", "skills-beta"}


def test_vendored_skills_module_is_package_importable() -> None:
    from toolregistry._vendor import skills as vendored

    skill = vendored.Skill.loads("---\nname: demo\ndescription: Demo\n---\nBody.\n")
    assert skill.name == "demo"
    assert vendored.to_catalog([skill]).count("<skill>") == 1


def test_environment_path_separator(tmp_path: Path, monkeypatch) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv(
        "TOOLREGISTRY_SKILLS_PATH", os.pathsep.join([str(first), str(second)])
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    roots = default_skill_roots(tmp_path / "project")
    custom = [root.path for root in roots if root.scope == "custom"]
    assert custom == [first.resolve(), second.resolve()]

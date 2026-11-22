---
title: Agent Skills
summary: Load, rank, and expose Agent Skills through ToolRegistry
description: Discover SKILL.md folders from standard roots, generate progressive-disclosure prompts, and expose skills as tools without provider-specific code.
keywords: agent skills, SKILL.md, progressive disclosure, skills catalog, skill activation
---

# Agent Skills

Agent Skills are a portable packaging format for reusable agent instructions. A skill is a directory with a `SKILL.md` file: YAML frontmatter carries the name and description, and the Markdown body carries the instructions. Optional `scripts/`, `references/`, and `assets/` directories bundle resources.

ToolRegistry loads skills separately from tools. The parsed model and selector come from the vendored zerodep `skills` module; the registry adds standard roots, precedence, safe resource access, prompt generation, and tool exposure.

## Progressive disclosure

Skills are loaded in three levels:

1. **Catalog** — name and description only, so a model can choose a skill cheaply.
2. **Activation** — the full instructions of one skill.
3. **Resources** — individual scripts, references, or assets, read on demand.

```python
from toolregistry import ToolRegistry

registry = ToolRegistry()
registry.discover_skills()          # scan the standard roots

print(registry.skills_catalog())    # level 1
print(registry.activate_skill("pdf-processing"))  # level 2
print(registry.read_skill_resource("pdf-processing", "references/guide.md"))  # level 3
```

`skills_catalog()` omits filesystem locations by default, because the catalog is prompt text. Pass `include_locations=True` to add a `~`-relative location; paths outside the home directory appear as a file name only.

Discovery is lazy. Constructing a `ToolRegistry` does not touch the filesystem; the first skill API call creates the store.

## Loading skills

### Standard roots

`discover_skills()` with no arguments scans these roots, lowest precedence first:

| Scope | Root |
|-------|------|
| system | `/etc/agents/skills` |
| system | `/usr/local/share/agents/skills` |
| system | `~/.codex/skills/.system` |
| user | `~/.agents/skills` |
| user | `~/.codex/skills` |
| user | `~/.claude/skills` |
| project | `<project>/.claude/skills` |
| project | `<project>/.codex/skills` |
| project | `<project>/.agents/skills` |

`TOOLREGISTRY_SKILLS_PATH` (or `AGENT_SKILLS_PATH`) adds `os.pathsep`-separated roots with the highest precedence.

Later roots replace earlier skills with the same name, so a project skill overrides a user skill and a user skill overrides a system skill. Pass `override=False` to keep the first definition.

### Explicit paths

A path may be a root directory, a skill directory, or a `SKILL.md` file:

```python
registry.discover_skills("/opt/company-skills")
registry.discover_skills("/opt/company-skills/pdf-processing")
registry.discover_skills("/opt/company-skills/pdf-processing/SKILL.md")
```

Use `recursive=False` to scan only immediate children of a root. Include and exclude patterns use `fnmatch`:

```python
registry.discover_skills(
    "/opt/company-skills",
    include=["pdf-*", "spreadsheet-*"],
    exclude=["*-draft"],
)
```

`reload_skills()` re-runs every source from disk, including sources added by `apply_skill_config()`, and each source keeps its own paths, recursion flag, filters, and override setting. Each `discover_skills()` call replaces the reload plan unless you pass `append=True`; `apply_skill_config()` does this automatically so every configured source survives a reload. Invalid skills are skipped during discovery and do not break the rest of the scan; `skill_discovery_errors()` returns the failed directories, and `validate_skills(include_failures=True)` includes them in the validation report.

`validate_skills()` is advisory for names: discovery still registers a skill whose name is outside the Agent Skills convention, and the tool builder sanitizes the exposed tool name.

## Selecting skills

`select_skills()` ranks skills against a task description. It uses BM25 when the vendored `sparse_search` module is available and falls back to keyword overlap. If a skill declares `allowed-tools` in its frontmatter, pass `available_tools` so skills that require a missing tool are filtered out:

```python
matches = registry.select_skills(
    "extract tables from a scanned PDF",
    top_k=3,
    available_tools=registry.list_tools(),
)
for match in matches:
    print(match.skill.name, match.score)
```

## Exposing skills as tools

`register_skill_tools()` turns skills into ordinary tools, so every provider and MCP client uses the same schema and execution path. Three modes are available.

| Mode | Tools | Names |
|------|-------|-------|
| `catalog` (default) | list, get, read-resource | `skills-list`, `skills-get`, `skills-read_resource` |
| `per-skill` | one activation tool per skill | `skills-<skill-name>` |
| `both` | both sets | both |

```python
registry.register_skill_tools()                      # catalog
registry.register_skill_tools(mode="per-skill")      # one tool per skill
registry.register_skill_tools(mode="both")
```

With `namespace=None`, the catalog tools are named `list_skills`, `get_skill`, and `read_skill_resource`; per-skill tools use the `skill_` prefix. Per-skill names are normalized to snake_case, accents are folded, and any remaining character outside `[A-Za-z0-9_-]` becomes `_`. The whole name is capped at 64 characters including the namespace; longer names and sanitized collisions get a stable hash suffix, so distinct skills stay distinct. `unregister_skill_tools()` removes only tools whose metadata source is `skills`.

The catalog tools support optional query ranking:

```python
registry.invoke("skills-list", {"query": "spreadsheet formula"})
```

## Configuration

Add a top-level `skills:` block to a JSONC or YAML config file. A single mapping is accepted as a one-item list.

```yaml
skills:
  - paths: [skills, ../shared-skills]
    recursive: true
    override: true
    exclude: ["*-draft"]
    expose: catalog
    namespace: skills
  - path: vendor/skills
    expose: per-skill
    limit: 20
```

Fields:

| Field | Default | Meaning |
|-------|---------|---------|
| `path` / `paths` | standard roots | Explicit skill roots, directories, or `SKILL.md` files. Relative paths resolve against the config file. |
| `recursive` | `true` | Walk nested directories. |
| `override` | `true` | Let a later source replace an earlier skill with the same name. |
| `include` / `exclude` | none | Name globs; mutually exclusive. |
| `enabled` | `true` | Skip the source when false. |
| `expose` | `catalog` | `catalog`, `per-skill`, `both`, or `none`. |
| `namespace` | `skills` | Namespace for generated tools; `null` keeps unscoped names. |
| `limit` | none | Per-skill mode: maximum number of tools. |

Because the config file records its own path, relative skill roots work from any working directory:

```python
from toolregistry import ToolRegistry
from toolregistry.config import load_config

config = load_config("tools.yaml")
registry = ToolRegistry()
registry.apply_skill_config(config)
```

Multiple sources share one skill store. Their tools are combined; a repeated catalog namespace replaces the earlier catalog with one that covers every skill discovered so far.

## Prompt integration

If you build prompts yourself, use the catalog for the system prompt and activate only the skills the model asks for:

```python
system_prompt = (
    "You can use the following skills. Call get_skill before following one.\n\n"
    + registry.skills_catalog()
)
```

`activate_skill(..., inline_resources=True)` embeds small resource files in the activation prompt. Files above `max_inline_bytes` (64 KiB by default) are listed by path only. Inlining is all-or-nothing per skill: if any bundled file escapes the skill directory, activation raises instead of inlining the remaining files.

## Resource safety

Resource reads accept only paths relative to the skill directory. Absolute paths, `..` segments, and symlinks that resolve outside the directory are rejected. Reads are capped at 64 KiB by default and at 10 MiB even when a caller requests more. Binary resources are listed by `resources()` but `read_skill_resource()` raises instead of returning undecodable text. `resources()` also walks nested directories, while inline activation includes only top-level files under `scripts/`, `references/`, and `assets/`.

## Admin panel

The admin panel adds a **Skills** tab and three read-only endpoints:

- `GET /api/skills` — catalog records, discovery roots, and exposed tool names.
- `GET /api/skills/<name>` — metadata, instructions, and resource list.
- `POST /api/skills/<name>/activate` — activation prompt; body accepts `inline_resources` and `max_inline_bytes`.
- `GET /api/skills/<name>/resources/<path>` — one text resource.

## See Also

- [ToolRegistry API](../api/core/toolregistry.md) — method reference
- [Declarative Configuration](configuration.md) — full config file format
- [Admin Panel](../admin/index.md) — panel overview

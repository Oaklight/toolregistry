---
title: Agent 技能
summary: 通过 ToolRegistry 加载、排序并暴露 Agent Skills
description: 从标准目录发现 SKILL.md 文件夹，生成渐进式披露提示词，并无需厂商专用代码即可把技能暴露为工具。
keywords: agent skills, SKILL.md, 渐进式披露, 技能目录, 技能激活
---

# Agent 技能

Agent Skills 是一种可移植的 Agent 指令打包格式。一个技能是一个包含 `SKILL.md` 的目录：YAML frontmatter 保存名称与描述，Markdown 正文保存指令。可选的 `scripts/`、`references/` 与 `assets/` 目录用于捆绑资源。

ToolRegistry 将技能与工具分开加载。解析模型和选择器来自 vendored 的 zerodep `skills` 模块；注册表额外提供标准目录、优先级、安全的资源访问、提示词生成和工具暴露。

## 渐进式披露

技能分三层加载：

1. **目录** — 只包含名称与描述，让模型以较低成本选择技能。
2. **激活** — 一个技能的完整指令。
3. **资源** — 按需读取单个脚本、参考文档或资产。

```python
from toolregistry import ToolRegistry

registry = ToolRegistry()
registry.discover_skills()          # 扫描标准目录

print(registry.skills_catalog())    # 第 1 层
print(registry.activate_skill("pdf-processing"))  # 第 2 层
print(registry.read_skill_resource("pdf-processing", "references/guide.md"))  # 第 3 层
```

发现是惰性的。构造 `ToolRegistry` 不会访问文件系统；第一次调用技能 API 时才创建存储。

## 加载技能

### 标准目录

不带参数调用 `discover_skills()` 会扫描以下目录，优先级从低到高：

| 作用域 | 目录 |
|--------|------|
| system | `/etc/agents/skills` |
| system | `/usr/local/share/agents/skills` |
| system | `~/.codex/skills/.system` |
| user | `~/.agents/skills` |
| user | `~/.codex/skills` |
| user | `~/.claude/skills` |
| project | `<project>/.claude/skills` |
| project | `<project>/.codex/skills` |
| project | `<project>/.agents/skills` |

`TOOLREGISTRY_SKILLS_PATH`（或 `AGENT_SKILLS_PATH`）可添加以 `os.pathsep` 分隔的自定义目录，优先级最高。

后加载的目录会替换同名技能，因此项目技能覆盖用户技能，用户技能覆盖系统技能。传入 `override=False` 可保留第一个定义。

### 显式路径

路径可以是根目录、技能目录或 `SKILL.md` 文件：

```python
registry.discover_skills("/opt/company-skills")
registry.discover_skills("/opt/company-skills/pdf-processing")
registry.discover_skills("/opt/company-skills/pdf-processing/SKILL.md")
```

使用 `recursive=False` 只扫描根目录的直接子目录。include 与 exclude 使用 `fnmatch` 模式：

```python
registry.discover_skills(
    "/opt/company-skills",
    include=["pdf-*", "spreadsheet-*"],
    exclude=["*-draft"],
)
```

`reload_skills()` 会从磁盘重新执行上一次发现。发现的无效技能会被跳过，不会中断其余扫描。使用 `validate_skills()` 明确报告问题。

## 选择技能

`select_skills()` 根据任务描述对技能排序。vendored 的 `sparse_search` 可用时使用 BM25，否则回退到关键词重叠。如果技能在 frontmatter 中声明了 `allowed-tools`，传入 `available_tools` 可以过滤掉依赖缺失工具的技能：

```python
matches = registry.select_skills(
    "从扫描版 PDF 中提取表格",
    top_k=3,
    available_tools=registry.list_tools(),
)
for match in matches:
    print(match.skill.name, match.score)
```

## 将技能暴露为工具

`register_skill_tools()` 把技能转换为普通工具，因此所有 provider 和 MCP 客户端都使用相同的 schema 与执行路径。支持三种模式。

| 模式 | 工具 | 名称 |
|------|------|------|
| `catalog`（默认） | 列表、获取、读资源 | `skills-list`、`skills-get`、`skills-read_resource` |
| `per-skill` | 每个技能一个激活工具 | `skills-<技能名>` |
| `both` | 两套工具 | 两者 |

```python
registry.register_skill_tools()                      # catalog
registry.register_skill_tools(mode="per-skill")      # 每个技能一个工具
registry.register_skill_tools(mode="both")
```

`namespace=None` 时，catalog 工具名为 `list_skills`、`get_skill`、`read_skill_resource`；per-skill 工具使用 `skill_` 前缀。`unregister_skill_tools()` 只删除元数据来源为 `skills` 的工具。

catalog 工具支持按查询排序：

```python
registry.invoke("skills-list", {"query": "电子表格公式"})
```

## 配置

在 JSONC 或 YAML 配置文件中添加顶层 `skills:` 块。单个映射会被视为只含一项的列表。

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

字段：

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `path` / `paths` | 标准目录 | 显式技能根目录、技能目录或 `SKILL.md` 文件。相对路径相对于配置文件解析。 |
| `recursive` | `true` | 遍历嵌套目录。 |
| `override` | `true` | 允许后加载的来源替换同名技能。 |
| `include` / `exclude` | 无 | 名称 glob；二者互斥。 |
| `enabled` | `true` | 为 false 时跳过该来源。 |
| `expose` | `catalog` | `catalog`、`per-skill`、`both` 或 `none`。 |
| `namespace` | `skills` | 生成工具的命名空间；`null` 保持无命名空间。 |
| `limit` | 无 | per-skill 模式下的最大工具数。 |

配置文件会记录自身路径，因此相对技能目录可在任意工作目录下使用：

```python
from toolregistry import ToolRegistry
from toolregistry.config import load_config

config = load_config("tools.yaml")
registry = ToolRegistry()
registry.apply_skill_config(config)
```

多个来源共享同一个技能存储。它们生成的工具会合并；重复的 catalog 命名空间会用覆盖当前已发现全部技能的版本替换前一个。

## 提示词集成

如果自行构建提示词，可在系统提示中使用目录，只在模型请求时激活技能：

```python
system_prompt = (
    "你可以使用以下技能。遵循某个技能前先调用 get_skill。\n\n"
    + registry.skills_catalog()
)
```

`activate_skill(..., inline_resources=True)` 会把较小的资源文件嵌入激活提示词。超过 `max_inline_bytes`（默认 64 KiB）的文件只列出路径。

## 资源安全

资源读取只接受相对于技能目录的路径。绝对路径、`..` 片段以及解析后离开目录的符号链接都会被拒绝。默认读取上限为 64 KiB，即使调用方请求更多也不会超过 10 MiB。二进制资源可由 `resources()` 列出，但 `read_skill_resource()` 会抛出错误，而不是返回无法解码的文本。

## 管理面板

管理面板新增 **技能** 标签页和三个只读端点：

- `GET /api/skills` — 目录记录、发现根目录和已暴露工具名。
- `GET /api/skills/<name>` — 元数据、指令和资源列表。
- `POST /api/skills/<name>/activate` — 激活提示词；请求体接受 `inline_resources` 与 `max_inline_bytes`。
- `GET /api/skills/<name>/resources/<path>` — 单个文本资源。

## 另见

- [ToolRegistry API](../api/core/toolregistry.md) — 方法参考
- [声明式配置](configuration.md) — 完整配置格式
- [管理面板](../admin/index.md) — 面板概览

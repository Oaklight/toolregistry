---
title: 结果大小管理
summary: 大型工具输出的自动截断与持久化
description: 如何配置 max_result_size 与 max_result_tokens、截断策略和结果持久化，防止过大的工具输出占满 LLM 上下文
keywords: 截断, 结果大小, max_result_size, max_result_tokens, 持久化, TruncatedResult
author: Oaklight
---

# 结果大小管理

某些工具会产生大量输出——数据库查询、文件读取、API 响应——可能超出 token 限制并占满 LLM 上下文。ToolRegistry 提供自动结果截断功能，并可选择将完整输出持久化到临时文件。

## 概览

当工具的结果超过大小限制时，ToolRegistry 会：

1. 当注册表启用 `persist_large_results` 时，将**完整结果**存入私有的按运行目录
   （默认关闭）
2. 使用可配置的策略**截断**结果
3. 在前面添加包含原始大小以及（已存储时）不透明结果 id 的头部信息
4. 将截断后的结果返回给 LLM

限制可以按字符（`max_result_size`）或按 token（`max_result_tokens`）给出。
能解析为 JSON 的结果会被结构化裁剪，在裁剪结果未超出限制时仍然可解析；否则按
文本截断，与此前行为一致。其余文本按行边界切分。

## 配置大小限制

### 单工具限制

在 `ToolMetadata` 上设置 `max_result_size`（单位：字符数）：

```python
from toolregistry import Tool, ToolMetadata

def query_database(sql: str) -> str:
    """Execute a SQL query and return results."""
    ...

registry.register(
    Tool.from_function(
        query_database,
        metadata=ToolMetadata(max_result_size=2000),
    )
)
```

### 注册表全局默认值

未显式设置限制的注册表会应用
`toolregistry.llm.truncation.DEFAULT_MAX_RESULT_SIZE`：64,000 个字符，按文档中的
估算约为 16k token。传入 `None` 可恢复旧版本的“不限制”行为：

```python
from toolregistry import ToolRegistry

# 显式设置更大或更小的默认值
registry = ToolRegistry(default_max_result_size=5000)

# 不设默认上限：只应用工具级限制
registry = ToolRegistry(default_max_result_size=None)
```

单工具的 `max_result_size` 优先于注册表默认值。工具将 `max_result_size` 留为
`None` 时会使用注册表默认值；除非修改，该默认值为 64,000 个字符。

该上限在 `execute_tool_calls()` 完成结果时应用。直接经 `route.handler` 调用的工具
（MCP 适配器即走此路径）尚未应用该上限，见
[toolregistry-server#286](https://github.com/Oaklight/toolregistry-server/issues/286)。

### Token 预算

`max_result_tokens` 以 token 为单位设置同样的限制：

```python
registry = ToolRegistry(default_max_result_tokens=4000)

# 或按工具设置：
ToolMetadata(max_result_tokens=1000)
```

token 按每 token 4 个字符的固定估算换算为字符
（`toolregistry.llm.truncation.CHARS_PER_TOKEN`）。这是一个数量级保护，而不是
分词器；需要精确截断时请使用 `max_result_size`。工具级取值覆盖该字段的注册表
默认值；字符限制与 token 限制同时存在时，取较小者。默认的 64,000 字符上限也是
字符限制，因此高于 16k 的 token 预算不会产生额外效果，除非提高或关闭默认字符
上限。

## 截断策略

内容决定机制。能解析为 JSON 的结果无论选择哪种策略都会先做结构化裁剪：对象根会
获得一个 `_truncation` 键，携带 `omitted_items` 与 `omitted_chars`（若该键已存在
则改名）；数组根会追加一个携带相同计数的哨兵元素；文档中过长的字符串会带上自身
的省略计数。只有裁剪结果未超出限制时才会使用这种输出；当元数据在预算内放不下时，
结果回退到下面的文本路径，可能不再可解析，与此前一致。对于其他文本，适用下面的
策略，`HEAD_TAIL` 在结果至少有三行时按行边界切分。

通过 `TruncationStrategy` 提供两种策略：

| 策略 | 行为 |
|------|------|
| `HEAD` | 仅保留前 `max_size` 个字符 |
| `HEAD_TAIL` | 保留首尾部分，中间插入省略标记 **（默认）** |

### HEAD_TAIL（默认）

将预算分配给结果的开头和结尾，在中间插入标记显示省略的字符数：

```
输出的开头部分...
... (truncated 15000 chars) ...
...输出的结尾部分
```

这种方式同时保留了开头（通常是列标题、初始上下文）和结尾（通常是摘要或最终结果）。

### HEAD

简单地在 `max_size` 处截断结果。适用于只需要开头部分的场景。

## 截断输出格式

发生截断时，发送给 LLM 的结果包含元数据头部：

```
[Truncated: 20000 chars -> 2000 chars | full output: res_0123456789abcdef0123456789abcdef (read it with read_result)]
输出的开头部分...
... (truncated 18000 chars) ...
...输出的结尾部分
```

完整结果已存储时，头部给出不透明 id，LLM 可以用内置的 `read_result` 工具分页读取
完整输出。头部中永远不会出现文件系统路径。

裁剪结果未超出限制时，JSON 结果保持为 JSON。对象根的元数据随文档一起返回
（计数取决于实际裁剪的内容）：

```json
{"items":[{"value":0},{"value":1}],"_truncation":{"omitted_items":198,"omitted_chars":2970}}
```

## 持久化

持久化是可选项。`ToolRegistry(persist_large_results=True)` 会把每次截断的完整结果
存入私有的按运行目录（目录权限 `0700`，文件权限 `0600`）。头部携带不透明 id
（`res_<32 hex>`，128 位随机数），内置的 `read_result(id, offset=0, limit=8000)`
工具可分页读取存储的文本。结果在 24 小时后过期；目录总量上限 64 MiB，超出时按
最旧优先淘汰，刚写入的文件不会被淘汰，即使它本身就超过配额。Phase 0 的淘汰是
全局且不考虑作用域的：一个租户写入足够多结果时，可能淘汰另一个租户仍存活的结果；
按租户与按会话的配额属于 Phase 1。读取时会比较 blob 大小与元数据，截断或未完整
恢复的文件会报告为 corrupt，而不会被当作完整内容返回；记录的 SHA-256 可供调用方
显式做完整校验，但不会在每次分页时重算。

不启用该选项时不会写入磁盘：截断结果只包含截断后的文本。

### 保留策略

```python
registry = ToolRegistry(
    persist_large_results=True,
    result_ttl_seconds=60 * 60,        # 硬过期，从创建时算起
    result_idle_ttl_seconds=10 * 60,   # 滑动过期，读取会刷新
    result_quota_bytes=32 * 1024 * 1024,
)
```

硬 TTL 是绝对期限，读取不会延长它。空闲 TTL 只在设置后生效，读取会延长它，
`stat()` 不会。实际过期时间取两者中较早者。也可用
`result_store=ResultStore(...)` 注入准备好的存储；只要提供 store，即使
`persist_large_results` 保持 `False` 也会启用持久化，且以 store 自身的保留设置为准。

### 归属作用域

`ResultStore` 可以为每个结果记录归属。`ResultScope(tenant, session)` 标识 API key
或部署租户，以及逻辑 agent 会话；`session=None` 的作用域表示租户内共享，带 session
的作用域则只对该会话可见。读取时会校验调用方作用域，因此一个 agent 即使知道 id
也读不到另一个 agent 的结果。

库模式调用方保持无作用域：

```python
from toolregistry.llm.truncation import ResultScope, use_result_scope

with use_result_scope(ResultScope(tenant="tenant-a", session="session-1")):
    ...  # 此处写入的结果会带上该作用域
```

服务端适配器应显式传入作用域，例如通过
`ToolRegistry(result_scope_provider=...)` 或按请求绑定，因为执行器的线程和进程
不会携带上下文变量。`result_scope_policy` 定义约束：`"optional"`（默认）接受
无作用域写入，`"required"` 要求任意作用域，`"session_required"` 要求带 session。
在强制策略下缺少作用域会抛出异常，而不是写入一个公开结果。ADR-004 之前的
`res_<12 hex>` 文件没有归属元数据，默认拒绝读取；`ResultStore(allow_legacy=True)`
允许无作用域调用方继续读取它们。该开关是进程级的，任何无作用域调用方都能读取
所有旧版 blob；因此只应在不存在无作用域请求路径时启用，并在迁移窗口结束后关闭。

内置 `read_result` 工具只做本地文件 I/O。注册表在派发线程上解析作用域，在 worker
中绑定该作用域，并始终使用线程后端运行它。这样 `use_result_scope()` 对工具调用
同样有效，也避免把注册表及其锁送进进程池。服务端适配器仍应绑定 provider 或按请求
处理器，而不是依赖环境状态。

## 编程接口

截断模块也可直接使用：

```python
from toolregistry.llm.truncation import (
    ResultStore,
    TruncationStrategy,
    truncate_result,
)

store = ResultStore(root="/var/tmp/my-results", ttl=3600, quota=32 * 1024 * 1024)
result = truncate_result(
    result_str=very_long_string,
    max_size=2000,
    strategy=TruncationStrategy.HEAD_TAIL,
    tool_name="my_tool",
    persist=True,
    store=store,
)

print(result.truncated)       # True
print(result.original_size)   # 20000
print(result.result_id)       # "res_0123456789abcdef0123456789abcdef"
print(result.full_path)       # 宿主路径，仅供程序使用
print(result.content)         # 截断后的内容
print(str(result))            # 头部给出 id，绝不给出路径
```

`truncate_result` 同样执行内容感知规则；需要自行换算 token 预算的调用方可以使用
导出的 `CHARS_PER_TOKEN`。`persist=True` 且未传 `store` 时使用进程级的
`default_result_store()`；传入自己的 `ResultStore` 可指定目录、TTL 与配额，结果
属于某个租户或会话时再传入 `scope=`。`ResultStore` 还提供 `put_result()`，返回
包含 id、大小、SHA-256、内容类型、工具名、归属与时间戳的 `ResultInfo`，以及
`stat(result_id, scope=...)`，读取同样的元数据但不会延长空闲 TTL。
`put_result()` 是子类扩展点，`put()` 只是委托给它的兼容包装。注册表内置的
`read_result` 只读取该注册表自身的 store（或它与其它注册表共享的注入 store），
因此直接调用 `truncate_result` 时必须用同一个 store 读回。

### TruncatedResult 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `content` | `str` | （可能截断的）文本内容 |
| `original_size` | `int` | 原始结果大小（字符数） |
| `truncated` | `bool` | 是否进行了截断 |
| `full_path` | `str \| None` | 存储结果的宿主路径；`str()` 从不渲染它 |
| `result_id` | `str \| None` | 供 `read_result` 使用的不透明 id；未存储时为 `None` |

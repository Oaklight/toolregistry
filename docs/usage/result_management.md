---
title: Result Size Management
summary: Automatic truncation and persistence for large tool outputs
description: How to configure max_result_size and max_result_tokens, truncation strategies, and result persistence to prevent oversized tool outputs from bloating LLM context
keywords: truncation, result size, max_result_size, max_result_tokens, persistence, TruncatedResult
author: Oaklight
---

# Result Size Management

Some tools produce large outputs — database queries, file reads, API responses — that can exceed token limits and bloat the LLM context. ToolRegistry provides automatic result truncation with optional persistence of the full output to a temporary file.

## Overview

When a tool's result exceeds a size limit, ToolRegistry:

1. Stores the **full result** in a private per-run directory when the
   registry enables `persist_large_results` (off by default)
2. **Truncates** the result using a configurable strategy
3. Prepends a header with the original size and, when stored, an opaque result id
4. Returns the truncated result to the LLM

A limit can be given in characters (`max_result_size`) or in tokens
(`max_result_tokens`). A result that parses as JSON is pruned structurally so
it still parses, but only when the pruned document fits the limit; otherwise it
is truncated as text, exactly as before. Other text is split on line
boundaries.

## Configuring Size Limits

### Per-Tool Limit

Set `max_result_size` on `ToolMetadata` (in characters):

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

### Registry-Wide Default

A registry without an explicit limit applies
`toolregistry.llm.truncation.DEFAULT_MAX_RESULT_SIZE`: 64,000 characters,
roughly 16k tokens at the documented estimate. Pass `None` to restore the
unlimited behaviour of earlier releases:

```python
from toolregistry import ToolRegistry

# Explicit larger or smaller default
registry = ToolRegistry(default_max_result_size=5000)

# No default cap: only per-tool limits apply
registry = ToolRegistry(default_max_result_size=None)
```

Per-tool `max_result_size` takes precedence over the registry default. A tool
that leaves `max_result_size` at `None` uses the registry default, which is
64,000 characters unless changed.

The cap is applied when `execute_tool_calls()` finalizes a result. A tool
invoked directly through `route.handler`, which is how the MCP adapter serves
it, is not capped yet; see
[toolregistry-server#286](https://github.com/Oaklight/toolregistry-server/issues/286).

### Token Budgets

`max_result_tokens` sets the same limit in tokens:

```python
registry = ToolRegistry(default_max_result_tokens=4000)

# or per tool:
ToolMetadata(max_result_tokens=1000)
```

Tokens are converted to characters with a fixed estimate of 4 characters per
token (`toolregistry.llm.truncation.CHARS_PER_TOKEN`). This is an
order-of-magnitude guard, not a tokenizer; use `max_result_size` when the cut
has to be exact. A tool-level value overrides the registry default for that
field, and when both a character and a token limit apply, the smaller one
wins. The default 64,000-character cap is a character limit, so a token budget
above 16k has no additional effect unless you raise or disable the default
character cap.

## Truncation Strategies

Content decides the mechanism. A result that parses as JSON is pruned
structurally, whatever strategy is selected: a dict root gains a `_truncation`
key with `omitted_items` and `omitted_chars` (renamed if the key already
exists), a list root gains one sentinel element with the same counts, and an
over-long string inside the document carries its own count. The pruned text is
used only when it fits the limit; when the metadata cannot fit within the
budget, the result falls back to the text path below and may no longer parse,
exactly as before this change. For other text, the strategies below apply, and
`HEAD_TAIL` splits on line boundaries when the result has at least three
lines.

Two strategies are available via `TruncationStrategy`:

| Strategy | Behavior |
|----------|----------|
| `HEAD` | Keep only the first `max_size` characters |
| `HEAD_TAIL` | Keep the first and last portions with a marker in the middle **(default)** |

### HEAD_TAIL (Default)

Splits the budget between the beginning and end of the result, inserting a marker showing how many characters were omitted:

```
First part of the output...
... (truncated 15000 chars) ...
...last part of the output
```

This preserves both the start (often column headers, initial context) and end (often summary or final results) of the output.

### HEAD

Simply cuts off the result at `max_size` characters. Use when only the beginning matters.

## Truncated Output Format

When truncation occurs, the result sent to the LLM includes a metadata header:

```
[Truncated: 20000 chars -> 2000 chars | full output: res_0123456789abcdef0123456789abcdef (read it with read_result)]
First part of the output...
... (truncated 18000 chars) ...
...last part of the output
```

When the full result was stored, the header names an opaque id and the LLM can
page through the full output with the built-in `read_result` tool. A filesystem
path is never placed in the header.

When the pruned document fits, JSON results stay JSON. For a dict root the
metadata rides in the document itself (the counts depend on what was pruned):

```json
{"items":[{"value":0},{"value":1}],"_truncation":{"omitted_items":198,"omitted_chars":2970}}
```

## Persistence

Persistence is opt-in. `ToolRegistry(persist_large_results=True)` stores each
truncated full result in a private per-run directory created with mode `0700`;
files are created with mode `0600`. The header carries an opaque id
(`res_<32 hex>`, 128 random bits), and the built-in
`read_result(id, offset=0, limit=8000)` tool pages through the stored text.
A result expires after 24 hours, and the directory is capped at 64 MiB with the
oldest file evicted first; the file just written is never evicted, even when it
alone exceeds the quota. Eviction is global and scope-blind in Phase 0: a
tenant that writes enough results can evict another tenant's live result.
Per-tenant and per-session quotas arrive in Phase 1. Reads compare the blob's
size with its metadata and report a truncated or partially restored blob as
corrupt instead of serving it; the recorded SHA-256 is available for an
explicit full verification but is not recomputed on every page.

Without the flag nothing is written to disk: a truncated result is only the
truncated text.

### Retention settings

```python
registry = ToolRegistry(
    persist_large_results=True,
    result_ttl_seconds=60 * 60,        # hard expiry, measured from creation
    result_idle_ttl_seconds=10 * 60,   # sliding expiry, refreshed by reads
    result_quota_bytes=32 * 1024 * 1024,
)
```

The hard TTL is absolute: reads never extend it. The idle TTL applies only when
set, and reads extend it; `stat()` does not. The effective expiry is the
earlier of the two. `result_store=ResultStore(...)` injects a prepared store
instead; supplying a store also enables persistence when
`persist_large_results` is left at `False`, and the store's own retention
settings win.

### Ownership scope

`ResultStore` can attach an owner to every result. A `ResultScope(tenant,
session)` identifies the API key or deployment tenant and the logical agent
session; a scope with `session=None` is tenant-shared, and a scope with a
session is private to that session. Reads check the caller's scope, so one
agent cannot read another agent's result even when it learns the id.

Library callers stay unscoped:

```python
from toolregistry.llm.truncation import ResultScope, use_result_scope

with use_result_scope(ResultScope(tenant="tenant-a", session="session-1")):
    ...  # results written here carry that scope
```

Server adapters should pass the scope explicitly, either through
`ToolRegistry(result_scope_provider=...)` or per request, because executor
threads and processes do not carry a context variable. `result_scope_policy`
controls the contract: `"optional"` (default) accepts unscoped writes,
`"required"` needs any scope, and `"session_required"` needs a session. A
missing scope under a required policy raises instead of writing a public
result. Pre-ADR-004 `res_<12 hex>` files carry no owner metadata and are denied
by default; `ResultStore(allow_legacy=True)` keeps them readable for an
unscoped caller. The flag is process-wide, so every unscoped caller can read
every legacy blob; enable it only while no unscoped request path exists, and
turn it off after the migration window.

The built-in `read_result` tool is local file I/O. The registry resolves its
scope on the dispatching thread, binds that scope in the worker, and always
runs it on the thread backend. That is what makes `use_result_scope()` work for
tool calls; it also avoids sending the registry and its locks to a process
pool. Server adapters still bind a provider or a per-request handler rather
than depending on ambient state.

## Programmatic Access

The truncation module can also be used directly:

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
print(result.full_path)       # host path; programmatic use only
print(result.content)         # truncated content
print(str(result))            # header names the id, never the path
```

`truncate_result` applies the same content-aware rules, and `CHARS_PER_TOKEN`
is exported for callers that convert a token budget themselves. `persist=True`
without a `store` uses the process-wide `default_result_store()`; pass your own
`ResultStore` to choose the directory, TTL and quota, and pass `scope=` when the
result belongs to a tenant or session. The `ResultStore` API also exposes
`put_result()`, which returns a `ResultInfo` with the id, size, SHA-256,
content type, tool name, owner, and timestamps, and `stat(result_id,
scope=...)`, which reads that metadata without extending the idle TTL.
`put_result()` is the extension point for subclasses; `put()` remains a
compatibility wrapper that delegates to it. A registry's built-in `read_result`
reads only from that registry's store (or from an injected store it shares with
other registries), so a direct `truncate_result` call must be read back through
the same store you passed it.

### TruncatedResult Fields

| Field | Type | Description |
|-------|------|-------------|
| `content` | `str` | The (possibly truncated) text content |
| `original_size` | `int` | Original result size in characters |
| `truncated` | `bool` | Whether truncation was applied |
| `full_path` | `str \| None` | Host path to the stored result; never rendered by `str()` |
| `result_id` | `str \| None` | Opaque id for `read_result`, or `None` when not stored |

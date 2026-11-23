# ADR-004: Result-Store Scoping, Ownership, and Session Lifecycle

**Date:** 2026-11-23
**Status:** Proposed

## Context

`toolregistry` has two deployment shapes:

1. **Library mode** — one process owns one `ToolRegistry` and its
   `ResultStore`.  There is no authenticated caller.
2. **Server mode** — `toolregistry-server` builds one `ToolRegistry` and
   serves it through MCP and OpenAPI adapters.  Several API keys, transports,
   and agent sessions may share that registry and therefore one
   `ResultStore`.

The current implementation assumes shape 1:

- `ResultStore` writes `res_<12 hex>` files into a flat directory.
- A stored result has no owner, tenant, or session field.
- `ResultStore.read(id)` authorizes by knowing the id alone.
- The id is 48 random bits.
- Eviction is global oldest-first, so one caller can evict another
  caller's live result.
- TTL starts at write time and is fixed.  Reads do not extend it.
- `ToolRegistry` exposes only `persist_large_results: bool`; TTL, idle TTL,
  quota, and the store itself are not injectable.
- `toolregistry-server` has `SessionContext`, but it is MCP-only, in-memory,
  keyed by the transport session object, and not bound to the authenticated
  `Principal`.

A shared registry therefore has a cross-agent read and eviction problem.
The `read_result` tool is registered once on the shared registry and accepts
only an id, so any caller that learns an id can read the full result.

## Decision

### 1. Visibility is a server-side security boundary

Introduce a `ResultScope` that is resolved from the authenticated caller at
execution time, never from tool arguments:

```python
@dataclass(frozen=True)
class ResultScope:
    tenant: str | None = None   # API-key id, operator, or deployment tenant
    session: str | None = None  # logical agent session
```

- In library mode the scope is `None`.  The store is a process-local trust
  domain, and a single process is assumed not to be hostile to itself.
- In server mode the adapter sets the scope for every tool call.  An unset
  scope in server mode is a configuration error, not "public".
- The scope travels in a `ContextVar` so `_finalize_result()`,
  `truncate_result()`, and the `read_result` tool can see it without changing
  every tool signature.

### 2. Separate logical agent sessions from transport sessions

Add a server-level `SessionRecord`:

```text
session_id      stable logical id
principal       authenticated key id, or operator
transport       mcp / openapi / admin / stdio
created_at      creation time
last_seen_at    last request
lease_expires   idle lease deadline
state           active / ended / expired
```

- An MCP transport session maps to one logical session when it is created.
- Reconnects may resume a logical session with a signed session token; a new
  transport session without that token creates a new logical session.
- OpenAPI is stateless.  It needs a client-supplied `X-Agent-Session` header
  or a server-issued session token that is bound to the principal.  Without
  one, the safest default is tenant scope only, documented as such.

### 3. Persist ownership metadata with every result

```text
id             res_ + 32 hex (128 random bits)
tenant         owner tenant
session        owner logical session
tool_name      producing tool
created_at     write time
last_access_at last successful read
hard_expires   created_at + hard TTL
idle_ttl       sliding TTL
size_bytes     content size
sha256         content digest
content_type   text/json/content-block
```

The smallest viable implementation is a sidecar JSON file or a SQLite index
next to the blobs.  A SQLite index is preferred once multi-process or admin
listing is required.

### 4. Authorization check at read time

`read_result(id, offset, limit)` succeeds only when:

- the stored `(tenant, session)` equals the caller's scope; or
- the caller is an operator with an explicit override; or
- the caller presents a valid capability token for that result.

Unknown and unauthorized results return the same error.  This prevents the
tool from becoming an existence oracle.

An id is a locator, not a credential.  Learning an id from a log, a shared
transcript, or another agent must not grant access.

### 5. TTL is a policy, not a disconnect event

Use an effective expiry:

```text
expires_at = min(
    created_at + hard_ttl,
    last_access_at + idle_ttl,
    session_end + disconnect_grace,
    revoked_at,
)
```

- Disconnect is a revocation signal.  It is not the definition of expiry.
- A crash, network partition, or lost finalizer must not leave data forever;
  TTL is the backstop.
- A graceful disconnect may revoke immediately, or after a configurable
  grace period when reconnect is expected.
- Reads extend `idle_ttl` only.  `hard_ttl` is absolute, so an active agent
  cannot retain data forever by reading it.
- Long-running agents can raise `hard_ttl` and renew a session lease.  They
  cannot opt out of a deployment-wide maximum.

### 6. Quotas are per scope plus a global cap

- Each tenant and each session has a byte quota.
- A global cap protects the host.
- Exceeding a tenant quota evicts that tenant's oldest results only.
- Exceeding the global cap evicts expired results first, then the oldest
  results of the tenant that triggered the write, then fails the write.
- A write never silently deletes another live tenant's result.

### 7. Explicit sharing is a capability, not ownership

Default sharing is none.  To share one result, mint a capability:

```text
cap_<opaque> = HMAC(server_key, result_id | audience | permissions | expiry)
```

- The capability is unguessable, audience-bound, expiring, and revocable
  through a short TTL or a revocation list.
- A capability grants read access to one result.  It does not transfer
  ownership and cannot be reused for other results.
- Sharing is audited.

### 8. Core API shape

```python
result_scope_var: ContextVar[ResultScope | None]

@contextmanager
def use_result_scope(scope: ResultScope | None) -> Iterator[None]: ...

class ResultStore:
    def put(self, text, *, scope=None, metadata=None) -> ResultHandle: ...
    def read(self, result_id, *, scope=None, offset=0, limit=...) -> str: ...
    def stat(self, result_id, *, scope=None) -> ResultInfo: ...
    def revoke_scope(self, scope) -> int: ...
    def share(self, result_id, *, audience, permissions, ttl) -> str: ...
```

`ToolRegistry` gains injectable, backward-compatible settings:

```python
ToolRegistry(
    persist_large_results=True,
    result_store=ResultStore(...),
    result_ttl_seconds=24 * 60 * 60,
    result_idle_ttl_seconds=60 * 60,
    result_quota_bytes=64 * 1024 * 1024,
    result_scope_provider=None,  # callable returning ResultScope | None
)
```

`read_result` keeps its current schema.  It resolves the scope from the
context variable or provider.  It never accepts an owner, tenant, or session
argument.

## Consequences

- One shared registry can safely serve many agents.
- Library mode stays simple; its boundary is the process, not an identity.
- Server mode must set a scope on every execution path, including OpenAPI and
  background jobs.  A missing scope must fail closed.
- Stored metadata grows, so an index and garbage collector become necessary.
- Result ids become longer and the truncation notice should name the paging
  hint, size, and expiry.
- Existing cross-session reads stop working.  This is intentional.
- If tenants share one OS user, filesystem permissions alone are not
  isolation.  Mutually untrusted tenants need per-tenant encryption or
  separate processes.

## Phases

### Phase 0 — Core scaffolding (backward compatible)

Goal: make the store scope-aware without changing default behavior.

- Add `ResultScope`, the context variable, and the scope context manager.
- Add owner metadata, 128-bit ids, `stat()`, and configurable TTL/quota.
- Add `result_store` and retention settings to `ToolRegistry`.
- Make `read_result` resolve a scope when one is set; keep current behavior
  when it is not.
- Add tests for scope matching, id entropy, TTL, quotas, and concurrent writes.

Acceptance: existing tests pass; new isolation tests pass in library mode;
no server changes are required yet.

### Phase 1 — Server enforcement (the actual fix)

Goal: isolate agents that share one registry.

- Bind scope to `(principal.key_id, logical session_id)` in MCP.
- Add an OpenAPI session identity (`X-Agent-Session` or issued token).
- Revoke results on session end, with a configurable grace period.
- Enforce per-tenant and per-session quotas.
- Return the same error for unknown and unauthorized ids.
- Add audit log entries for denials, revocations, and operator reads.

Acceptance: two keys cannot read each other's results; two sessions of one
key cannot read each other's results; disconnect plus grace expiry removes
results; one tenant cannot evict another tenant's live result.

### Phase 2 — Access ergonomics

Goal: make long content usable without weakening isolation.

- Add `stat_result`, `search_result`, line ranges, and JSON-pointer reads.
- Improve the truncation notice with size, expiry, and paging instructions.
- Expose results as MCP resources scoped to the session.
- Add explicit `share_result` capabilities.
- Add streaming reads for very large text and binary artifacts.

Acceptance: an agent can find and page the relevant part of a large result
without reading the whole value; a shared capability works only for its
audience and expiry.

### Phase 3 — Durable shared backend

Goal: survive restarts and serve multiple processes.

- SQLite index plus content-addressed blobs, with reference counts.
- Multi-process locks and leases.
- Per-tenant encryption at rest when tenants are mutually untrusted.
- Admin listing, inspection, deletion, and audit.
- Metrics: bytes per tenant, expiry rate, eviction rate, denial rate.

Acceptance: two server processes share one store without double-eviction or
cross-tenant reads; a restart preserves unexpired results.

### Phase 4 — Resumable long-running agents

Goal: support agents that outlive a transport connection.

- Signed logical-session tokens with resume and renewal.
- Per-tenant retention policies and export.
- Cross-agent handoff through explicit capabilities.
- Retention and deletion reporting for compliance.

Acceptance: an agent reconnects after a transport drop and reaches its own
unexpired results; the same results are unreachable without the session
token.

## Hash credentials

A content hash is not an ownership credential:

- Anyone who has the content can compute the hash.
- A hash check proves equality, not authority.
- Returning a hash on read and requiring it on write is optimistic
  concurrency control (an `ETag` / `If-Match` pattern), not authentication.

A signed HMAC handle can act as a capability:

- It must be unguessable, scoped to one result, bound to an audience, and
  expiring.
- It must be verified server-side and be revocable.
- It is appropriate for explicit sharing, not for default ownership.

Current results are immutable, so no edit/version check is needed.  If
mutable named artifacts are added later, use a version or ETag for
compare-and-swap, and keep ownership tied to the authenticated principal.

## Open questions

1. Does an OpenAPI caller get an `X-Agent-Session` header, or must it call a
   `POST /sessions` endpoint to obtain a signed token?
2. Should results survive a transport reconnect by default, or should only
   explicitly resumable sessions retain them?
3. Are tenants mutually untrusted on one host?  If yes, Phase 1 cannot rely
   on filesystem permissions alone and needs per-tenant encryption earlier.
4. Should a shared API key share results by default?  Recommendation: no;
   scope to the logical session.

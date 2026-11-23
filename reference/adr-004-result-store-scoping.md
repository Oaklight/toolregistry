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
- Server bootstrap sets `result_scope_policy="required"`.  A provider that
  returns `None` under that policy raises; it never becomes "public".  Library
  mode uses the default `"optional"` policy.
- Scope is an explicit argument on every store call.  `_finalize_result()`
  resolves it in the caller and passes `scope=`.  A `ContextVar` is only an
  in-process convenience for library callers; it is not the enforcement
  mechanism.
- Executor backends do not carry caller context: the thread pool submits
  without `copy_context()`, and the process pool cannot cross a context
  variable at all.  A tool body that needs the store must receive a
  serializable scope through `ExecutionContext` or an explicit parameter.
  Reading the context variable inside a pooled tool reads `None`.
- The registry-level `read_result` tool is the library fallback.  Server
  adapters bind a per-request handler that closes over the caller's scope:
  MCP uses the existing `handler_factory`, and OpenAPI calls a scope-aware
  handler directly.  The public schema stays `read_result(id, offset, limit)`.
- `default_result_store()`, the process-wide singleton used by
  `truncate_result(persist=True)`, is a second store.  It must use the same
  scope API.  In required mode an unscoped write through it is refused, and
  server paths must not use it without an explicit scope.

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
- OpenAPI is stateless.  The caller obtains a server-issued, signed,
  principal-bound, expiring token from `POST /sessions`.  `X-Agent-Session`
  may be sent as a client-side label, but a bare header is never identity and
  never part of authorization.
- `result_scope_policy` has three values.  `"optional"` is library mode.
  `"required"` requires a scope object; a tenant-only scope `(t, None)` passes.
  `"session_required"` requires a non-empty session and refuses a tenant-only
  caller.  Server bootstrap sets `"required"`; a deployment that wants every
  result session-private sets `"session_required"`.
- The trust assumption is one operator and many agents.  The store runs under
  one OS user and the server process can read every file, so filesystem
  permissions are not a tenant boundary.  Hosted multi-customer deployments
  need per-tenant encryption in Phase 1, not only Phase 3.

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

Write order is blob first, then metadata with an atomic rename.  A crash
between the two leaves an orphan blob, never a record that points at missing
bytes.  The orphan sweep uses the recorded `sha256` and runs before quota
accounting.

### 4. Authorization check at read time

`read_result(id, offset, limit)` succeeds only when:

- the stored `(tenant, session)` equals the caller's scope; or
- the caller is an operator with an explicit override; or
- the caller presents a valid capability token for that result.

Scope containment is asymmetric:

- A record with a session is session-private.  Only the exact `(tenant,
  session)` pair, an operator, or a valid capability can read it.
- A record with `session=None` is tenant-shared.  Any caller in the same
  tenant can read it, including a caller that later obtained a session token.
- A tenant-only caller cannot read a session-private record.  This keeps a
  pre-token result readable after the caller obtains a token while keeping a
  session result isolated.

Unknown and unauthorized results return the same error.  This prevents the
tool from becoming an existence oracle.

An id is a locator, not a credential.  Learning an id from a log, a shared
transcript, or another agent must not grant access.

An operator is an override, not a scope key.  The stored record always names a
real tenant and session, and an operator read is audited.  Pre-upgrade files
carry no owner metadata, so `read` denies them by default; an operator can
open them only through the explicit override.

A denial audit entry records the principal, tool, reason, and whether the id
existed.  It never records content.  An operator read records the same fields
plus the override that allowed it.

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
- Terms that do not exist for a live session, such as `session_end` and
  `revoked_at`, are omitted from the `min()`; they are not passed as `None`.
- Resume must happen before the grace expires, using the signed session token.
  A disconnect sets a non-terminal `session_end`; an explicit revoke sets a
  terminal `revoked_at` that resume does not clear.

### 6. Quotas are per scope plus a global cap

- Each tenant and each session has a byte quota.
- A global cap protects the host.
- Exceeding a tenant quota evicts that tenant's oldest results only.
- Exceeding the global cap evicts expired results first, then the oldest
  results of the tenant that triggered the write.
- If the triggering tenant cannot free enough space, the write is not
  persisted.  The tool call still returns its truncated content without a
  `res_` handle, plus one warning.  Storage pressure never fails a tool call.
  The per-session quota has the same observable outcome.
- A write never silently deletes another live tenant's result.

### 7. Explicit sharing is a capability, not ownership

Default sharing is none.  To share one result, mint a capability:

```text
cap_<opaque> = HMAC-SHA256(tenant_key, canonical_fields)
canonical_fields = length-prefixed {result_id, audience, permissions, expiry}
```

- Verify with `hmac.compare_digest`.  Do not concatenate bare fields; a
  length-prefixed or canonical-JSON encoding prevents boundary ambiguity.
- `tenant_key` is derived from the deployment key and the tenant id.  Key
  rotation keeps the previous key valid for an overlap window and records
  which key minted each capability.  A capability minted before a rotation
  remains valid only until its own expiry and the overlap window ends.
- The capability is unguessable, audience-bound, expiring, and revocable
  through a short TTL or a revocation list.
- A capability grants read access to one result.  It does not transfer
  ownership, cannot be reused for other results, and cannot be re-shared
  downstream.
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
    result_scope_policy="optional",  # required | session_required in server mode
)
```

`read_result` keeps its current schema.  It takes `id`, `offset`, and `limit`;
it takes no owner, tenant, or session argument.  The registry-level
registration is for library mode.  In server mode the adapter binds a handler
that closes over the resolved scope, so the tool never depends on ambient
state that a thread or process backend would drop.

## Consequences

- One shared registry can safely serve many agents.
- Library mode stays simple; its boundary is the process, not an identity.
- Server mode must set a scope on every execution path, including OpenAPI and
  background jobs.  A missing scope must fail closed.
- Stored metadata grows, so an index and garbage collector become necessary.
- Result ids become longer and the truncation notice should name the paging
  hint, size, and expiry.
- Existing `res_<12 hex>` ids keep working only while Phase 0 accepts both
  widths.  Pre-upgrade files have no owner metadata and deny by default.
- Existing cross-session reads stop working.  This is intentional.
- A quota failure degrades to truncated output without a handle.  It never
  turns into a failed tool call.
- OpenAPI callers need a server-issued session token for session isolation.
  A caller that sends only `X-Agent-Session` gets tenant scope.
- If tenants share one OS user, filesystem permissions alone are not
  isolation.  Mutually untrusted tenants need per-tenant encryption or
  separate processes.

## Phases

### Phase 0 — Core scaffolding (backward compatible)

Goal: make the store scope-aware without changing default behavior.

- Add `ResultScope`, the optional context variable, and the scope context
  manager.  Keep explicit `scope=` as the enforcement path.
- Add `result_scope_policy`: `"optional"`, `"required"` (scope object needed),
  and `"session_required"` (non-empty session needed).  A missing scope under
  a required policy raises.
- Add owner metadata, 128-bit ids, `stat()`, and configurable TTL/quota.
- Widen `_RESULT_ID_PATTERN` to accept 12 and 32 hex characters in Phase 0.
  New writes use 32 hex.  Pre-upgrade files without metadata deny by default
  and are swept or migrated explicitly, so they cannot leak forever.
- Add `result_store` and retention settings to `ToolRegistry`.
- Make the registry-level `read_result` resolve an explicit scope when one is
  provided.  Keep current behavior in optional library mode.  Do not rely on
  a context variable inside a thread or process backend.
- Apply the same scope API to `default_result_store()`.
- Add orphan-blob sweep and blob-before-metadata write order.
- Add tests for scope matching, id width compatibility, id entropy, TTL,
  quotas, storage-pressure degradation, and concurrent writes.

Acceptance: existing tests pass; new isolation tests pass in library mode;
no server changes are required yet.

### Phase 1 — Server enforcement (the actual fix)

Goal: isolate agents that share one registry.

- Bind scope to `(principal.key_id, logical session_id)` in MCP.  The operator
  is an override, not part of the scope key.
- Add `POST /sessions`, returning a signed, principal-bound, expiring session
  token for OpenAPI.  `X-Agent-Session` is a label only and never part of
  authorization.
- Set `result_scope_policy="required"` at server bootstrap; deployments that
  want every result session-private set `"session_required"`.
- Bind adapter-level `read_result` handlers per request, closing over the
  resolved scope.  Keep the registry-level tool as the library fallback.
- Implement asymmetric containment: session records are session-private;
  tenant-only records are tenant-shared and readable by any caller in the
  same tenant.
- Revoke results on session end, with a configurable grace period.  A
  disconnect is non-terminal; an explicit revoke is terminal.
- Enforce per-tenant and per-session quotas.  A quota failure degrades to
  truncated output without a handle, plus one warning.
- Return the same error for unknown and unauthorized ids.
- Add audit log entries for denials, revocations, and operator reads.  Record
  principal, tool, reason, and id existence; never content.
- Retain session results across transport reconnects by default, bounded by
  idle and hard TTL.  Resume requires the signed token before grace expiry.
- Enable per-tenant encryption for hosted multi-customer deployments.

Acceptance: two keys cannot read each other's results; two sessions of one
key cannot read each other's results; a caller in a tenant can read that
tenant's tenant-shared results but not another session's private result;
disconnect plus grace expiry removes results; a resume before grace reaches
the same logical session; one tenant cannot evict another tenant's live
result; quota pressure leaves the tool call successful with truncated output.

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
- Per-tenant encryption at rest for deployments that did not enable it in
  Phase 1.
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

## Resolved choices

1. **OpenAPI identity.** `POST /sessions` returns a signed, principal-bound,
   expiring token.  `X-Agent-Session` is a client-side label only; a bare
   header never establishes identity.
2. **Reconnect retention.** Results survive a transport reconnect by default,
   tied to the logical session and bounded by idle and hard TTL.  Resume must
   happen before the disconnect grace expires.  An explicit end revokes
   immediately.
3. **Tenant trust.** The default is one operator and many agents.  Hosted
   multi-customer deployments enable per-tenant encryption in Phase 1; other
   deployments get it in Phase 3.
4. **Shared-key default.** A shared API key does not share results.  Scope is
   the logical session; cross-session access goes through an explicit
   capability.

## Remaining decisions before Accepted

- Default values for `hard_ttl`, `idle_ttl`, and `disconnect_grace`, and
  whether an operator read returns content or metadata only.
- Session-token lifetime and renewal policy for OpenAPI.
- Whether a resumed session keeps the same `session_id` or a new id with a
  parent link.  Either way, the signed token names the logical session.

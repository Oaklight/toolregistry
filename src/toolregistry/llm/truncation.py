"""Result size management: truncation and persistence for large tool outputs."""

import contextlib
import contextvars
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

#: Characters per token used to convert a token budget into characters.
#: This is a fixed estimate, not a tokenizer: a token limit is an
#: order-of-magnitude guard, while ``max_result_size`` is the exact limit.
CHARS_PER_TOKEN = 4

#: Character budget a registry applies when no other limit is configured.
#: At :data:`CHARS_PER_TOKEN` this is about 16k tokens.  A tool's own
#: ``max_result_size`` or ``max_result_tokens`` still overrides it, and
#: ``ToolRegistry(default_max_result_size=None)`` disables the default cap.
DEFAULT_MAX_RESULT_SIZE = 64_000

#: Marker budget reserved for the line-aware omission notice.
_LINE_MARKER_RESERVE = 48

#: Marker budget reserved for the final text block a truncated content-block
#: list gains.  It covers the longest notice, which names the persisted
#: ``res_`` id and the ``read_result`` hint.
CONTENT_BLOCK_MARKER_RESERVE = 128

#: Smallest per-string cap applied when a JSON document is pruned.
_MIN_STRING_LIMIT = 64

#: Key that carries omission counts inside a pruned JSON document.
_METADATA_KEY = "_truncation"

#: Default lifetime of a persisted result, in seconds (24 hours).
DEFAULT_RESULT_TTL_SECONDS = 24 * 60 * 60.0

#: Default total size of the persistence directory, in bytes (64 MiB).
DEFAULT_RESULT_QUOTA_BYTES = 64 * 1024 * 1024

#: Default idle lifetime of a persisted result.  ``None`` means no sliding
#: deadline; the hard TTL still applies.
DEFAULT_RESULT_IDLE_TTL_SECONDS: float | None = None

#: Default scope policy.  Library callers do not provide a scope.
DEFAULT_RESULT_SCOPE_POLICY = "optional"

#: Characters returned by one ``read_result`` call when *limit* is omitted.
DEFAULT_READ_RESULT_LIMIT = 8000

#: Characters decoded per step while skipping to a read offset.
_READ_CHUNK = 65536

#: Hex characters in a newly allocated result id (128 random bits).
_RESULT_ID_HEX_CHARS = 32

#: Result ids written before ADR-004 had 12 hex characters (48 bits).
_LEGACY_RESULT_ID_HEX_CHARS = 12

_RESULT_ID_PATTERN = re.compile(
    rf"^res_(?:[0-9a-f]{{{_LEGACY_RESULT_ID_HEX_CHARS}}}"
    rf"|[0-9a-f]{{{_RESULT_ID_HEX_CHARS}}})$"
)

_RESULT_METADATA_SUFFIX = ".json"
_RESULT_BLOB_SUFFIX = ".txt"


class TruncationStrategy(str, Enum):
    """Strategy for truncating large results.

    Attributes:
        HEAD: Keep only the first ``max_size`` characters.
        HEAD_TAIL: Keep the first and last portions, with a marker in the
            middle indicating how much was omitted.  For text with line
            structure the split is made on line boundaries.
    """

    HEAD = "head"
    HEAD_TAIL = "head_tail"


ResultScopePolicy = Literal["optional", "required", "session_required"]


@dataclass(frozen=True)
class ResultScope:
    """Owner scope attached to a persisted result.

    Attributes:
        tenant: Authenticated tenant or API-key id.  ``None`` only in
            unscoped library mode.
        session: Logical agent session.  ``None`` marks a tenant-shared
            result; a non-``None`` session marks a session-private result.
    """

    tenant: str | None = None
    session: str | None = None

    def __post_init__(self) -> None:
        for name in ("tenant", "session"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")
        if self.session is not None and self.tenant is None:
            raise ValueError("session scope requires a tenant")


result_scope_var: contextvars.ContextVar[ResultScope | None] = contextvars.ContextVar(
    "toolregistry_result_scope", default=None
)
"""Current result scope, when a caller set one.

This is a convenience for in-process library callers.  Executor backends do
not carry context variables into worker threads or processes; server code
must pass a scope explicitly or bind a per-request handler.
"""


def current_result_scope() -> ResultScope | None:
    """Return the scope bound to the current context, if any."""
    return result_scope_var.get()


@contextlib.contextmanager
def use_result_scope(scope: ResultScope | None) -> Iterator[None]:
    """Bind *scope* for the duration of the context.

    Args:
        scope: Scope to bind, or ``None`` to bind an unscoped context.

    Yields:
        Nothing.  The previous binding is restored on exit.
    """
    token = result_scope_var.set(scope)
    try:
        yield
    finally:
        result_scope_var.reset(token)


@dataclass(frozen=True)
class ResultInfo:
    """Metadata for one persisted result.

    Attributes:
        result_id: Opaque result id.
        path: Host path to the content blob.  Programmatic use only; do not
            serialize this field outside the process, because it exposes a
            host path.
        size_bytes: Stored content size in bytes.  Reads compare the blob's
            actual size against this value and refuse a truncated record.
        sha256: SHA-256 digest of the stored content.  Reads do not hash the
            whole blob; use this for an explicit full verification.
        content_type: Stored content type.
        tool_name: Tool that produced the result.
        tenant: Owning tenant, or ``None`` in library mode.
        session: Owning logical session; ``None`` means tenant-shared.
        created_at: Wall-clock creation time.
        last_access_at: Wall-clock time of the last access that refreshed a
            sliding deadline.  It stays at ``created_at`` when no
            ``idle_ttl`` is configured.
        expires_at: Effective expiry after the TTL rules are applied.
        legacy: Whether this record predates owner metadata.
    """

    result_id: str
    path: str
    size_bytes: int
    sha256: str
    content_type: str
    tool_name: str
    tenant: str | None
    session: str | None
    created_at: float
    last_access_at: float
    expires_at: float
    legacy: bool = False


@dataclass
class TruncatedResult:
    """Container for a possibly-truncated tool result.

    Attributes:
        content: The (possibly truncated) text content.
        original_size: Original result size in characters.
        truncated: Whether truncation was applied.
        full_path: Host path to the persisted full result.  For programmatic
            callers only: :meth:`__str__` never renders it, because the
            string form is placed in the model context.
        result_id: Opaque id (``res_<32 hex>``) under which the full result
            can be read back with the ``read_result`` tool.  ``None`` when
            the result was not persisted.
    """

    content: str
    original_size: int
    truncated: bool = False
    full_path: str | None = None
    result_id: str | None = None

    def __str__(self) -> str:
        if not self.truncated:
            return self.content
        notice = truncation_notice(
            self.original_size,
            len(self.content),
            result_id=self.result_id,
            full_path=self.full_path,
        )
        return f"{notice}\n{self.content}"


def truncation_notice(
    original_size: int,
    kept_size: int,
    *,
    result_id: str | None = None,
    full_path: str | None = None,
) -> str:
    """Return the bracket notice that accompanies a truncated result.

    Args:
        original_size: Size before truncation, in characters.
        kept_size: Size of the content the caller kept.
        result_id: Opaque id of the persisted full result, when it was stored.
        full_path: Host path of the persisted result, used only when no
            opaque id is available.

    Returns:
        A one-line notice naming both sizes and, when available, how to read
        the full result.  A filesystem path is never rendered.
    """
    header_parts = [f"Truncated: {original_size} chars -> {kept_size} chars"]
    if result_id:
        header_parts.append(f"full output: {result_id} (read it with read_result)")
    elif full_path:
        header_parts.append("full output: persisted")
    return f"[{' | '.join(header_parts)}]"


class ResultStore:
    """Per-run storage for full tool results.

    Each result is a content blob (``<id>.txt``) plus a sidecar metadata
    record (``<id>.json``).  Files live in a private directory, are created
    with mode ``0600``, and older results are evicted when the directory
    would exceed *quota* bytes.  Eviction is global and scope-blind: a
    tenant that writes enough results can evict another tenant's live
    result.  Per-tenant and per-session quotas arrive in Phase 1.  The
    result being written is retained even when it alone exceeds the quota,
    so a write never fails for storage pressure; the trade-off is that a
    single oversized result can leave the directory above its quota.  New
    ids are ``res_<32 hex>`` (128 random bits); legacy ``res_<12 hex>`` ids
    remain readable when *allow_legacy* is on.

    Each write scans the directory twice (cleanup and quota accounting), so
    writes are O(n) in the number of stored results until a durable index
    lands in Phase 3.  :meth:`put_result` is the extension point for
    subclasses; :meth:`put` is the compatibility wrapper that delegates to
    it.

    Args:
        root: Directory to store results in.  ``None`` creates a fresh
            per-run directory under the system temp dir with mode ``0700``.
        ttl: Hard lifetime of a result, in seconds, measured from creation.
        quota: Total bytes the directory may hold.
        idle_ttl: Sliding lifetime measured from the last successful read.
            ``None`` disables the sliding deadline; the hard TTL still
            applies.
        scope_policy: ``"optional"`` for library mode, ``"required"`` when a
            scope object must exist, or ``"session_required"`` when the scope
            must carry a session.
        allow_legacy: Whether pre-ADR-004 files without owner metadata stay
            readable.  ``False`` denies and sweeps them.
        clock: Wall-clock source, injectable for tests.

    Raises:
        ValueError: If a retention value is invalid or *scope_policy* is
            unknown.
    """

    def __init__(
        self,
        root: str | None = None,
        *,
        ttl: float = DEFAULT_RESULT_TTL_SECONDS,
        quota: int = DEFAULT_RESULT_QUOTA_BYTES,
        idle_ttl: float | None = DEFAULT_RESULT_IDLE_TTL_SECONDS,
        scope_policy: ResultScopePolicy = DEFAULT_RESULT_SCOPE_POLICY,
        allow_legacy: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        if idle_ttl is not None and idle_ttl <= 0:
            raise ValueError("idle_ttl must be positive or None")
        if quota < 0:
            raise ValueError("quota must be non-negative")
        if scope_policy not in ("optional", "required", "session_required"):
            raise ValueError(f"unknown result scope policy {scope_policy!r}")
        if not isinstance(allow_legacy, bool):
            raise TypeError("allow_legacy must be a boolean")
        self.root = (
            root
            if root is not None
            else tempfile.mkdtemp(prefix="toolregistry-results-")
        )
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        self.ttl = ttl
        self.idle_ttl = idle_ttl
        self.quota = quota
        self.scope_policy = scope_policy
        self.allow_legacy = allow_legacy
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def hard_ttl(self) -> float:
        """The hard TTL, in seconds."""
        return self.ttl

    def put(
        self,
        text: str,
        *,
        scope: ResultScope | None = None,
        tool_name: str = "",
        content_type: str = "text/plain",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, str]:
        """Store *text*, returning ``(result_id, path)``.

        This is the compatibility wrapper around :meth:`put_result`.
        Cleanup runs first: expired files are removed, then the oldest files
        are evicted until the directory fits the quota.  The result just
        written is never evicted, even when it alone exceeds the quota.

        Args:
            text: Full result text.
            scope: Owner scope.  ``None`` uses the current context scope.
            tool_name: Tool that produced the result.
            content_type: Content type recorded in the metadata.
            metadata: Extra JSON-serializable metadata.

        Returns:
            ``(result_id, path)`` for the stored blob.
        """
        info = self.put_result(
            text,
            scope=scope,
            tool_name=tool_name,
            content_type=content_type,
            metadata=metadata,
        )
        return info.result_id, info.path

    def put_result(
        self,
        text: str,
        *,
        scope: ResultScope | None = None,
        tool_name: str = "",
        content_type: str = "text/plain",
        metadata: Mapping[str, Any] | None = None,
    ) -> ResultInfo:
        """Store *text* and return its metadata record.

        This is the extension point for subclasses that add encryption,
        redaction, or audit hooks; :meth:`put` is a compatibility wrapper
        that delegates here.

        The blob is written first, then its metadata is installed with an
        atomic rename.  A crash between the two leaves an orphan blob, never
        a metadata record that points at missing bytes.

        Args:
            text: Full result text.
            scope: Owner scope.  ``None`` uses the current context scope.
            tool_name: Tool that produced the result.
            content_type: Content type recorded in the metadata.
            metadata: Extra JSON-serializable metadata.

        Returns:
            Metadata for the stored result.

        Raises:
            ValueError: If the scope policy is not satisfied.
            TypeError: If *metadata* is not JSON-serializable.
        """
        resolved_scope = self._resolve_scope(scope)
        payload = text.encode("utf-8")
        extra = dict(metadata) if metadata is not None else {}

        with self._lock:
            self._cleanup_locked()
            for _ in range(8):
                result_id = f"res_{secrets.token_hex(_RESULT_ID_HEX_CHARS // 2)}"
                path = self.path_for(result_id)
                try:
                    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    continue
                try:
                    with os.fdopen(handle, "w", encoding="utf-8") as stream:
                        stream.write(text)
                        stream.flush()
                        os.fsync(stream.fileno())
                    now = self._clock()
                    os.utime(path, (now, now))
                    record = {
                        "version": 1,
                        "id": result_id,
                        "tenant": (resolved_scope.tenant if resolved_scope else None),
                        "session": (resolved_scope.session if resolved_scope else None),
                        "tool_name": tool_name,
                        "content_type": content_type,
                        "created_at": now,
                        "last_access_at": now,
                        "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "metadata": extra,
                    }
                    self._write_metadata_locked(result_id, record)
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(path)
                    with contextlib.suppress(OSError):
                        os.unlink(self._metadata_path(result_id))
                    raise
                self._enforce_quota_locked(keep_id=result_id)
                return self._info_from_record(record, path)
        raise RuntimeError("could not allocate a result id")

    def path_for(self, result_id: str) -> str:
        """Return the host path for *result_id*.

        Raises:
            ValueError: If *result_id* is not an opaque result id.
        """
        if not isinstance(result_id, str) or not _RESULT_ID_PATTERN.fullmatch(
            result_id
        ):
            raise ValueError(f"invalid result id {result_id!r}")
        return os.path.join(self.root, f"{result_id}{_RESULT_BLOB_SUFFIX}")

    def read(
        self,
        result_id: str,
        offset: int = 0,
        limit: int = DEFAULT_READ_RESULT_LIMIT,
        *,
        scope: ResultScope | None = None,
    ) -> str:
        """Return one character slice of a stored result.

        A read refreshes the sliding deadline only when ``idle_ttl`` is
        configured; with no sliding deadline the metadata is not rewritten.
        The blob's size is checked against its metadata first, so a
        truncated or partially restored file is reported as corrupt instead
        of being served.

        Args:
            result_id: Opaque id returned by :meth:`put`.
            offset: Character offset to start at.
            limit: Maximum characters to return.
            scope: Caller scope.  ``None`` uses the current context scope.

        Raises:
            ValueError: If the id is invalid or unknown, the caller is not
                authorized, the result expired or is corrupt, the scope
                policy is not satisfied, or *offset*/*limit* is invalid.
        """
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        resolved_scope = self._resolve_scope(scope)
        path = self.path_for(result_id)
        with self._lock:
            record = self._record_locked(result_id, path)
            self._authorize_locked(record, resolved_scope)
            if self._is_expired(record):
                self._remove_locked(result_id)
                raise ValueError(f"result id {result_id!r} has expired")

        try:
            with open(path, encoding="utf-8") as stream:
                remaining = offset
                while remaining > 0:
                    skipped = len(stream.read(min(remaining, _READ_CHUNK)))
                    if skipped == 0:
                        # Reading past the end still counts as an access so
                        # a paging client keeps its idle deadline alive.
                        result = ""
                        break
                    remaining -= skipped
                else:
                    result = stream.read(limit)
        except FileNotFoundError as exc:
            raise ValueError(f"unknown result id {result_id!r}") from exc

        if self.idle_ttl is not None:
            self._touch(result_id, record)
        return result

    def stat(
        self,
        result_id: str,
        *,
        scope: ResultScope | None = None,
    ) -> ResultInfo:
        """Return metadata for a stored result without extending its TTL.

        Args:
            result_id: Opaque id returned by :meth:`put`.
            scope: Caller scope.  ``None`` uses the current context scope.

        Returns:
            The stored result metadata.

        Raises:
            ValueError: If the id is invalid or unknown, the caller is not
                authorized, the result expired, or the scope policy is not
                satisfied.
        """
        resolved_scope = self._resolve_scope(scope)
        path = self.path_for(result_id)
        with self._lock:
            record = self._record_locked(result_id, path)
            self._authorize_locked(record, resolved_scope)
            if self._is_expired(record):
                self._remove_locked(result_id)
                raise ValueError(f"result id {result_id!r} has expired")
            return self._info_from_record(record, path)

    def _resolve_scope(self, scope: ResultScope | None) -> ResultScope | None:
        """Resolve and validate the caller scope against the policy."""
        resolved = scope if scope is not None else current_result_scope()
        if resolved is not None and not isinstance(resolved, ResultScope):
            raise TypeError("scope must be a ResultScope or None")
        if self.scope_policy == "session_required":
            if resolved is None or resolved.session is None:
                raise ValueError("a result scope with a session is required")
        elif self.scope_policy == "required" and resolved is None:
            raise ValueError("a result scope is required")
        return resolved

    def _authorize_locked(
        self, record: Mapping[str, Any], scope: ResultScope | None
    ) -> None:
        """Raise ``ValueError`` unless *scope* may read *record*."""
        result_id = str(record.get("id", ""))
        if record.get("legacy"):
            if self.allow_legacy and scope is None:
                return
            raise ValueError(f"unknown result id {result_id!r}")
        record_tenant = record.get("tenant")
        record_session = record.get("session")
        if record_tenant is None and record_session is None:
            if scope is None:
                return
            raise ValueError(f"unknown result id {result_id!r}")
        if scope is None or scope.tenant != record_tenant:
            raise ValueError(f"unknown result id {result_id!r}")
        if record_session is None:
            return
        if scope.session != record_session:
            raise ValueError(f"unknown result id {result_id!r}")

    def _record_locked(self, result_id: str, path: str) -> dict[str, Any]:
        """Return the metadata record, or deny/sweep a legacy record."""
        if not os.path.exists(path):
            self._remove_locked(result_id)
            raise ValueError(f"unknown result id {result_id!r}")
        record = self._read_metadata_locked(result_id)
        if record is not None:
            self._verify_size_locked(result_id, path, record)
            return record
        if not self.allow_legacy:
            self._remove_locked(result_id)
            raise ValueError(f"unknown result id {result_id!r}")
        return self._legacy_record_locked(result_id, path)

    def _verify_size_locked(
        self,
        result_id: str,
        path: str,
        record: Mapping[str, Any],
    ) -> None:
        """Refuse a blob whose size no longer matches its metadata.  Holds lock.

        A truncated write, a partial restore, or a hand-edited file is
        swept instead of being served as if intact.  The full digest is
        recorded but not recomputed here, because hashing the whole blob on
        every page would defeat paging; callers that need that check can
        compare :attr:`ResultInfo.sha256` explicitly.
        """
        expected = record.get("size_bytes")
        if isinstance(expected, bool) or not isinstance(expected, int):
            return
        try:
            actual = os.stat(path).st_size
        except OSError:
            self._remove_locked(result_id)
            raise ValueError(f"unknown result id {result_id!r}") from None
        if actual != expected:
            self._remove_locked(result_id)
            raise ValueError(f"result id {result_id!r} is corrupt")

    def _legacy_record_locked(self, result_id: str, path: str) -> dict[str, Any]:
        """Build an in-memory record for a pre-ADR-004 blob."""
        stat = os.stat(path)
        return {
            "version": 0,
            "id": result_id,
            "tenant": None,
            "session": None,
            "tool_name": "",
            "content_type": "text/plain",
            "created_at": stat.st_mtime,
            "last_access_at": stat.st_mtime,
            "size_bytes": stat.st_size,
            "sha256": "",
            "metadata": {},
            "legacy": True,
        }

    def _read_metadata_locked(self, result_id: str) -> dict[str, Any] | None:
        """Return a metadata record, or ``None`` when it is absent/corrupt."""
        path = self._metadata_path(result_id)
        try:
            with open(path, encoding="utf-8") as stream:
                record = json.load(stream)
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("id") != result_id:
            return None
        return record

    def _write_metadata_locked(self, result_id: str, record: Mapping[str, Any]) -> None:
        """Atomically install the metadata record.  Holds the lock."""
        meta_path = self._metadata_path(result_id)
        temp_path = os.path.join(
            self.root,
            f".{result_id}.{secrets.token_hex(4)}{_RESULT_METADATA_SUFFIX}.tmp",
        )
        handle = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(
                    record,
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, meta_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
            raise

    def _metadata_path(self, result_id: str) -> str:
        """Return the metadata path for *result_id*."""
        return os.path.join(self.root, f"{result_id}{_RESULT_METADATA_SUFFIX}")

    def _touch(self, result_id: str, record: Mapping[str, Any]) -> None:
        """Update the sliding deadline after a successful read.

        The caller no longer holds the store lock; this method re-acquires it
        so a concurrent eviction cannot race the metadata rewrite.
        """
        if record.get("legacy"):
            return
        with self._lock:
            current = self._read_metadata_locked(result_id)
            if current is None:
                return
            current["last_access_at"] = self._clock()
            with contextlib.suppress(OSError):
                self._write_metadata_locked(result_id, current)

    def _expires_at(self, record: Mapping[str, Any]) -> float:
        """Return the effective expiry for *record*."""
        created = float(record.get("created_at", 0.0))
        last_access = float(record.get("last_access_at", created))
        expires = created + self.ttl
        if self.idle_ttl is not None:
            expires = min(expires, last_access + self.idle_ttl)
        return expires

    def _is_expired(self, record: Mapping[str, Any]) -> bool:
        """Return whether *record* is past its effective expiry."""
        return self._clock() > self._expires_at(record)

    def _info_from_record(self, record: Mapping[str, Any], path: str) -> ResultInfo:
        """Build a :class:`ResultInfo` from a metadata record."""
        created = float(record.get("created_at", 0.0))
        last_access = float(record.get("last_access_at", created))
        return ResultInfo(
            result_id=str(record.get("id", "")),
            path=path,
            size_bytes=int(record.get("size_bytes", 0)),
            sha256=str(record.get("sha256", "")),
            content_type=str(record.get("content_type", "text/plain")),
            tool_name=str(record.get("tool_name", "")),
            tenant=record.get("tenant"),
            session=record.get("session"),
            created_at=created,
            last_access_at=last_access,
            expires_at=self._expires_at(record),
            legacy=bool(record.get("legacy", False)),
        )

    def _remove_locked(self, result_id: str) -> None:
        """Remove a blob and its metadata.  Holds the lock."""
        with contextlib.suppress(OSError):
            os.unlink(self.path_for(result_id))
        with contextlib.suppress(OSError):
            os.unlink(self._metadata_path(result_id))

    def _cleanup_locked(self) -> None:
        """Remove expired, legacy, orphan, and temporary files.  Holds lock."""
        now = self._clock()
        live_ids: set[str] = set()
        for path in self._files():
            result_id = os.path.basename(path)[: -len(_RESULT_BLOB_SUFFIX)]
            record = self._read_metadata_locked(result_id)
            if record is None:
                if not self.allow_legacy:
                    with contextlib.suppress(OSError):
                        os.unlink(path)
                    with contextlib.suppress(OSError):
                        os.unlink(self._metadata_path(result_id))
                    continue
                record = self._legacy_record_locked(result_id, path)
            if now > self._expires_at(record):
                self._remove_locked(result_id)
                continue
            live_ids.add(result_id)

        try:
            names = os.listdir(self.root)
        except OSError:
            return
        for name in names:
            if name.endswith(".tmp"):
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(self.root, name))
                continue
            if not name.endswith(_RESULT_METADATA_SUFFIX):
                continue
            result_id = name[: -len(_RESULT_METADATA_SUFFIX)]
            if result_id not in live_ids:
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(self.root, name))

    def _enforce_quota_locked(self, keep_id: str) -> None:
        """Evict older results until the directory fits.  Holds lock.

        Eviction is global oldest-first and ignores scope, so one tenant's
        writes can remove another tenant's live result.  Per-scope quotas
        are a Phase 1 concern.
        """
        entries: list[tuple[float, int, str]] = []
        for path in self._files():
            result_id = os.path.basename(path)[: -len(_RESULT_BLOB_SUFFIX)]
            try:
                stat = os.stat(path)
            except OSError:
                continue
            record = self._read_metadata_locked(result_id)
            created = (
                float(record.get("created_at", stat.st_mtime))
                if record is not None
                else stat.st_mtime
            )
            size = stat.st_size
            try:
                size += os.stat(self._metadata_path(result_id)).st_size
            except OSError:
                pass
            entries.append((created, size, result_id))
        total = sum(size for _, size, _ in entries)
        for _, size, result_id in sorted(entries):
            if total <= self.quota:
                break
            if result_id == keep_id:
                continue
            self._remove_locked(result_id)
            total -= size

    def _files(self) -> list[str]:
        """Return the stored result blobs, ignoring foreign files."""
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        paths = []
        for name in names:
            if not name.endswith(_RESULT_BLOB_SUFFIX):
                continue
            result_id = name[: -len(_RESULT_BLOB_SUFFIX)]
            if _RESULT_ID_PATTERN.fullmatch(result_id):
                paths.append(os.path.join(self.root, name))
        return paths


_DEFAULT_STORE: ResultStore | None = None
_DEFAULT_STORE_LOCK = threading.Lock()


def default_result_store() -> ResultStore:
    """Return the process-wide store used by ``truncate_result(persist=True)``."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _DEFAULT_STORE_LOCK:
            if _DEFAULT_STORE is None:
                _DEFAULT_STORE = ResultStore()
    return _DEFAULT_STORE


# ---------------------------------------------------------------------------
# JSON-aware truncation
# ---------------------------------------------------------------------------


def _json_text(value: Any) -> str:
    """Serialize *value* compactly, so pruned output stays valid JSON."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _result_content_type(text: str) -> str:
    """Return ``application/json`` when *text* parses as JSON."""
    stripped = text.lstrip()
    if not stripped or stripped[0] not in "[{":
        return "text/plain"
    try:
        json.loads(text)
    except (TypeError, ValueError):
        return "text/plain"
    return "application/json"


def _truncate_string(value: str, limit: int) -> tuple[str, int]:
    """Cap one string, returning ``(text, omitted_chars)``.

    The marker names how many characters of the value were dropped, so a
    reader can tell a truncated string from a short one.
    """
    if len(value) <= limit:
        return value, 0
    keep = max(0, limit - 16)
    for _ in range(3):
        omitted = len(value) - keep
        marker = f"...[+{omitted} chars]"
        next_keep = max(0, limit - len(marker))
        if next_keep == keep:
            break
        keep = next_keep
    omitted = len(value) - keep
    return value[:keep] + f"...[+{omitted} chars]", omitted


def _cap_strings(value: Any, limit: int) -> tuple[Any, int]:
    """Recursively cap strings deeper than *limit*.

    Returns the rewritten value and the number of characters dropped.
    """
    if isinstance(value, str):
        return _truncate_string(value, limit)
    if isinstance(value, dict):
        rewritten: dict[Any, Any] = {}
        omitted = 0
        for key, item in value.items():
            rewritten[key], item_omitted = _cap_strings(item, limit)
            omitted += item_omitted
        return rewritten, omitted
    if isinstance(value, list):
        rewritten_list: list[Any] = []
        omitted = 0
        for item in value:
            rewritten_item, item_omitted = _cap_strings(item, limit)
            rewritten_list.append(rewritten_item)
            omitted += item_omitted
        return rewritten_list, omitted
    return value, 0


def _omission_counts(omitted_items: int, omitted_chars: int) -> dict[str, int]:
    """Return the metadata payload carried by a pruned document."""
    return {"omitted_items": omitted_items, "omitted_chars": omitted_chars}


def _free_metadata_key(root: dict[Any, Any]) -> str:
    """Return a metadata key that does not collide with a key in *root*."""
    if _METADATA_KEY not in root:
        return _METADATA_KEY
    index = 1
    while f"{_METADATA_KEY}_{index}" in root:
        index += 1
    return f"{_METADATA_KEY}_{index}"


def _prune_dict(
    root: dict[Any, Any], max_size: int, omitted_chars: int
) -> tuple[str, int, int]:
    """Drop trailing items until the serialized document fits.

    Returns ``(text, omitted_items, omitted_chars)``.  The original keys are
    kept in order; the metadata key is added last.  Counting serialized
    sizes up front keeps the loop linear in the number of items.
    """
    keys = list(root)
    key_sizes = {key: len(_json_text(key)) for key in keys}
    value_sizes = {key: len(_json_text(root[key])) for key in keys}
    meta_key = _free_metadata_key(root)
    meta_key_size = len(_json_text(meta_key)) + 1
    omitted_items = 0
    body = sum(key_sizes[key] + 1 + value_sizes[key] for key in keys)

    def serialized_size() -> int:
        metadata = _omission_counts(omitted_items, omitted_chars)
        meta_size = meta_key_size + len(_json_text(metadata))
        separators = max(0, len(keys) - 1)
        comma_before_meta = 1 if keys else 0
        return 2 + body + separators + comma_before_meta + meta_size

    while keys and serialized_size() > max_size:
        key = keys.pop()
        entry_size = key_sizes[key] + 1 + value_sizes[key]
        body -= entry_size
        omitted_items += 1
        omitted_chars += entry_size

    data = {key: root[key] for key in keys}
    data[meta_key] = _omission_counts(omitted_items, omitted_chars)
    return _json_text(data), omitted_items, omitted_chars


def _prune_list(
    root: list[Any], max_size: int, omitted_chars: int
) -> tuple[str, int, int]:
    """Drop trailing elements until the serialized list fits.

    The result keeps the remaining elements and appends one sentinel object
    ``{"_truncation": {...}}``.  Returns ``(text, omitted_items,
    omitted_chars)``.
    """
    sizes = [len(_json_text(item)) for item in root]
    omitted_items = 0
    body = sum(sizes)

    def serialized_size() -> int:
        sentinel = len(
            _json_text({_METADATA_KEY: _omission_counts(omitted_items, omitted_chars)})
        )
        separators = max(0, len(sizes) - 1)
        comma_before_sentinel = 1 if sizes else 0
        return 2 + body + separators + comma_before_sentinel + sentinel

    while sizes and serialized_size() > max_size:
        piece = sizes.pop()
        body -= piece
        omitted_items += 1
        omitted_chars += piece

    data = list(root[: len(sizes)])
    data.append({_METADATA_KEY: _omission_counts(omitted_items, omitted_chars)})
    return _json_text(data), omitted_items, omitted_chars


def _truncate_json_value(value: Any, max_size: int) -> str:
    """Return JSON text for *value* that fits *max_size* where possible."""
    if isinstance(value, (dict, list)):
        string_limit = max(_MIN_STRING_LIMIT, max_size // 4)
        capped, string_omitted = _cap_strings(value, string_limit)
        if isinstance(capped, dict):
            text, _, _ = _prune_dict(capped, max_size, string_omitted)
        else:
            text, _, _ = _prune_list(capped, max_size, string_omitted)
        return text
    if isinstance(value, str):
        return _json_text(_truncate_string(value, max(1, max_size - 2))[0])
    return _json_text(value)


def _truncate_json_document(text: str, max_size: int) -> str | None:
    """Return pruned JSON text when *text* parses as JSON and fits.

    A malformed document is not an error here: it falls through to the text
    path.  So does a document whose pruned form still exceeds *max_size*,
    for example when the omission metadata alone is larger than the budget.
    The returned text always parses with :func:`json.loads` and is never
    longer than *max_size*.

    ``RecursionError`` is caught as well: :func:`json.loads` and the
    recursive string capping both raise it on deeply nested documents, and
    such a result must fall back to text truncation instead of escaping.
    """
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    try:
        pruned = _truncate_json_value(value, max_size)
    except (TypeError, ValueError, RecursionError):
        return None
    if len(pruned) > max_size:
        return None
    return pruned


# ---------------------------------------------------------------------------
# Text truncation
# ---------------------------------------------------------------------------


def _head_tail_chars(text: str, max_size: int) -> str:
    """Legacy character head+tail split with a character-count marker."""
    marker = f"\n... (truncated {len(text) - max_size} chars) ...\n"
    available = max_size - len(marker)
    if available <= 0:
        # max_size too small for marker; fall back to pure head
        return text[:max_size]
    head_size = available // 2
    tail_size = available - head_size
    return text[:head_size] + marker + text[-tail_size:]


def _line_head_tail(text: str, lines: list[str], max_size: int) -> str | None:
    """Split *text* on line boundaries, or return ``None`` without structure.

    ``lines`` must come from ``text.splitlines(keepends=True)``.  Returns
    ``None`` when the budget cannot hold the marker or when a boundary line
    is too long, so the caller can fall back to a character split.
    """
    available = max_size - _LINE_MARKER_RESERVE
    if available <= 0:
        return None
    head_budget = available // 2
    tail_budget = available - head_budget
    if len(lines[0]) > head_budget or len(lines[-1]) > tail_budget:
        return None

    head_lines: list[str] = []
    head_used = 0
    for line in lines:
        if head_used + len(line) > head_budget:
            break
        head_lines.append(line)
        head_used += len(line)

    tail_lines: list[str] = []
    tail_used = 0
    for line in reversed(lines[len(head_lines) :]):
        if tail_used + len(line) > tail_budget:
            break
        tail_lines.append(line)
        tail_used += len(line)
    tail_lines.reverse()

    omitted_lines = len(lines) - len(head_lines) - len(tail_lines)
    omitted_chars = len(text) - head_used - tail_used
    marker = f"\n... ({omitted_lines} lines, {omitted_chars} chars omitted) ...\n"
    content = "".join(head_lines) + marker + "".join(tail_lines)
    if len(content) > max_size:
        return None
    return content


def _truncate_text(text: str, max_size: int, strategy: TruncationStrategy) -> str:
    """Truncate non-JSON text according to *strategy*."""
    if strategy == TruncationStrategy.HEAD:
        return text[:max_size]
    lines = text.splitlines(keepends=True)
    if len(lines) >= 3:
        content = _line_head_tail(text, lines, max_size)
        if content is not None:
            return content
    return _head_tail_chars(text, max_size)


def truncate_result(
    result_str: str,
    max_size: int,
    *,
    strategy: TruncationStrategy = TruncationStrategy.HEAD_TAIL,
    tool_name: str = "",
    persist: bool = True,
    store: ResultStore | None = None,
    scope: ResultScope | None = None,
) -> TruncatedResult:
    """Truncate a string result if it exceeds ``max_size`` characters.

    A document that parses as JSON is pruned structurally when the pruned
    form fits ``max_size``, so the output still parses: a dict root gains a
    ``_truncation`` key (renamed if the key already exists) with
    ``omitted_items``/``omitted_chars``, a list root gains one sentinel
    element with the same counts, and over-long strings inside the structure
    carry their own omission count.  When the pruned form cannot fit — for
    example when the omission metadata alone is larger than the budget — the
    document is truncated as text and may no longer parse.  Other text is
    truncated on line boundaries when it has useful line structure, with a
    marker naming the omitted lines and characters; text without line
    structure falls back to a character head+tail split.  Invalid JSON and
    deeply nested JSON are treated as plain text and never raise.

    The returned content is never longer than ``max_size``.

    Args:
        result_str: The full result string.
        max_size: Maximum allowed size in characters.
        strategy: Truncation strategy for non-JSON text.
        tool_name: Tool name recorded in the result metadata.
        persist: Whether to store the full result.
        store: Store to write to.  ``None`` uses
            :func:`default_result_store`, a process-wide per-run store.
        scope: Owner scope recorded with the stored result.  ``None`` uses
            the scope bound to the current context.  Server code passes the
            scope explicitly instead of relying on the context variable.

    Returns:
        A ``TruncatedResult`` with the (possibly truncated) content and
        metadata about the operation.
    """
    if len(result_str) <= max_size:
        return TruncatedResult(
            content=result_str,
            original_size=len(result_str),
            truncated=False,
        )

    full_path = None
    result_id = None
    if persist:
        info = (store or default_result_store()).put_result(
            result_str,
            scope=scope,
            tool_name=tool_name,
            content_type=_result_content_type(result_str),
        )
        result_id = info.result_id
        full_path = info.path

    content = _truncate_json_document(result_str, max_size)
    if content is None:
        content = _truncate_text(result_str, max_size, strategy)

    return TruncatedResult(
        content=content,
        original_size=len(result_str),
        truncated=True,
        full_path=full_path,
        result_id=result_id,
    )

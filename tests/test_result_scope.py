"""Result-store ownership, TTL, metadata, and id compatibility (ADR-004)."""

import hashlib
import json
import os
import re

import pytest

from toolregistry import Tool, ToolMetadata, ToolRegistry
from toolregistry.llm.truncation import (
    ResultScope,
    ResultStore,
    current_result_scope,
    default_result_store,
    truncate_result,
    use_result_scope,
)

_NEW_ID = re.compile(r"res_[0-9a-f]{32}")


def _extract_id(text: str) -> str:
    """Return the result id from a truncation header."""
    match = _NEW_ID.search(text)
    assert match is not None, text
    return match.group(0)


def _normalize_id(message: str) -> str:
    """Replace result ids so two error messages can be compared."""
    return re.sub(r"res_[0-9a-f]+", "<id>", message)


def _legacy_blob(root, content: str, *, char: str = "a") -> tuple[str, str]:
    """Create a pre-ADR-004 blob with no metadata sidecar."""
    result_id = f"res_{char * 12}"
    path = os.path.join(str(root), f"{result_id}.txt")
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(content)
    return result_id, path


def _registry_with_big(store: ResultStore, **kwargs) -> ToolRegistry:
    """Build a registry whose one tool returns 400 characters."""
    registry = ToolRegistry(result_store=store, **kwargs)

    def big(n: int = 400) -> str:
        """Return a large string."""
        return "x" * n

    registry.register(
        Tool.from_function(big, metadata=ToolMetadata(max_result_size=50))
    )
    return registry


class TestScopeMatching:
    def test_the_same_scope_can_read_the_result(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        scope = ResultScope(tenant="tenant-a", session="session-1")
        result_id = store.put("hello", scope=scope)[0]

        assert store.read(result_id, scope=scope) == "hello"

    @pytest.mark.parametrize(
        "reader",
        [
            ResultScope(tenant="tenant-a", session="session-2"),
            ResultScope(tenant="tenant-b", session="session-1"),
            ResultScope(tenant="tenant-b"),
        ],
    )
    def test_another_tenant_or_session_is_denied(self, tmp_path, reader):
        store = ResultStore(root=str(tmp_path))
        scope = ResultScope(tenant="tenant-a", session="session-1")
        result_id = store.put("hello", scope=scope)[0]

        with pytest.raises(ValueError, match="unknown result id"):
            store.read(result_id, scope=reader)

    def test_a_tenant_shared_result_is_readable_across_sessions(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        owner = ResultScope(tenant="tenant-a")
        result_id = store.put("shared", scope=owner)[0]

        for reader in (
            ResultScope(tenant="tenant-a"),
            ResultScope(tenant="tenant-a", session="later-session"),
        ):
            assert store.read(result_id, scope=reader) == "shared"

    def test_a_tenant_only_caller_cannot_read_a_session_result(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        result_id = store.put(
            "private",
            scope=ResultScope(tenant="tenant-a", session="session-1"),
        )[0]

        with pytest.raises(ValueError, match="unknown result id"):
            store.read(result_id, scope=ResultScope(tenant="tenant-a"))

    def test_an_unscoped_caller_cannot_read_a_scoped_result(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        result_id = store.put(
            "private",
            scope=ResultScope(tenant="tenant-a", session="session-1"),
        )[0]

        with pytest.raises(ValueError, match="unknown result id"):
            store.read(result_id)

    def test_a_scoped_caller_cannot_read_an_unscoped_result(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        result_id = store.put("library")[0]

        with pytest.raises(ValueError, match="unknown result id"):
            store.read(result_id, scope=ResultScope(tenant="tenant-a"))

    def test_unknown_and_unauthorized_errors_are_identical(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        result_id = store.put(
            "private",
            scope=ResultScope(tenant="tenant-a", session="session-1"),
        )[0]
        reader = ResultScope(tenant="tenant-b", session="session-1")

        with pytest.raises(ValueError) as denied:
            store.read(result_id, scope=reader)
        with pytest.raises(ValueError) as unknown:
            store.read("res_" + "0" * 32, scope=reader)

        assert _normalize_id(str(denied.value)) == _normalize_id(str(unknown.value))

    def test_stat_obeys_the_same_scope(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        scope = ResultScope(tenant="tenant-a", session="session-1")
        result_id = store.put("hello", scope=scope)[0]

        assert store.stat(result_id, scope=scope).size_bytes == 5
        with pytest.raises(ValueError, match="unknown result id"):
            store.stat(result_id, scope=ResultScope(tenant="tenant-b"))


class TestScopeContext:
    def test_use_result_scope_binds_and_restores_the_scope(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        scope = ResultScope(tenant="tenant-a", session="session-1")

        assert current_result_scope() is None
        with use_result_scope(scope):
            assert current_result_scope() == scope
            result_id = store.put("hello")[0]
            assert store.read(result_id) == "hello"

        assert current_result_scope() is None
        with pytest.raises(ValueError, match="unknown result id"):
            store.read(result_id)

    def test_an_explicit_scope_wins_over_the_context(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        context_scope = ResultScope(tenant="tenant-a", session="session-1")
        explicit = ResultScope(tenant="tenant-b", session="session-2")

        with use_result_scope(context_scope):
            result_id = store.put("hello", scope=explicit)[0]

        assert store.read(result_id, scope=explicit) == "hello"
        with pytest.raises(ValueError, match="unknown result id"):
            store.read(result_id, scope=context_scope)

    def test_the_default_store_honours_the_context_scope(self):
        scope = ResultScope(tenant="tenant-a", session="session-1")

        with use_result_scope(scope):
            truncated = truncate_result("x" * 100, max_size=10)

        assert (
            default_result_store().stat(truncated.result_id, scope=scope).session
            == "session-1"
        )


class TestScopePolicy:
    def test_required_rejects_an_unscoped_write(self, tmp_path):
        store = ResultStore(root=str(tmp_path), scope_policy="required")

        with pytest.raises(ValueError, match="scope is required"):
            store.put("hello")

        scope = ResultScope(tenant="tenant-a")
        result_id = store.put("hello", scope=scope)[0]
        assert store.read(result_id, scope=scope) == "hello"

    def test_session_required_rejects_a_tenant_only_scope(self, tmp_path):
        store = ResultStore(root=str(tmp_path), scope_policy="session_required")

        with pytest.raises(ValueError, match="with a session is required"):
            store.put("hello", scope=ResultScope(tenant="tenant-a"))

        scope = ResultScope(tenant="tenant-a", session="session-1")
        result_id = store.put("hello", scope=scope)[0]
        assert store.read(result_id, scope=scope) == "hello"

    def test_required_rejects_an_unscoped_read(self, tmp_path):
        store = ResultStore(root=str(tmp_path), scope_policy="required")
        scope = ResultScope(tenant="tenant-a")
        result_id = store.put("hello", scope=scope)[0]

        with pytest.raises(ValueError, match="scope is required"):
            store.read(result_id)

    def test_invalid_scope_objects_are_refused(self):
        with pytest.raises(ValueError, match="session scope requires a tenant"):
            ResultScope(session="session-1")
        with pytest.raises(ValueError, match="tenant must be a non-empty string"):
            ResultScope(tenant="")

    def test_invalid_policy_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="unknown result scope policy"):
            ResultStore(root=str(tmp_path), scope_policy="public")


class TestIds:
    def test_new_ids_use_128_random_bits(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        ids = {store.put("same content")[0] for _ in range(5)}

        assert len(ids) == 5
        assert all(_NEW_ID.fullmatch(result_id) for result_id in ids)

    @pytest.mark.parametrize(
        "bad_id",
        [
            "res_" + "a" * 13,
            "res_" + "a" * 31,
            "res_" + "a" * 33,
            "res_" + "A" * 32,
            "res_../../etc/passwd",
        ],
    )
    def test_only_two_id_widths_are_accepted(self, tmp_path, bad_id):
        store = ResultStore(root=str(tmp_path))
        with pytest.raises(ValueError, match="invalid result id"):
            store.path_for(bad_id)

    def test_a_legacy_id_is_readable_when_allowed(self, tmp_path):
        store = ResultStore(root=str(tmp_path), allow_legacy=True)
        result_id, _ = _legacy_blob(tmp_path, "legacy")

        assert store.read(result_id) == "legacy"
        info = store.stat(result_id)
        assert info.legacy is True
        assert info.tenant is None
        assert info.session is None

    def test_a_legacy_id_is_denied_by_default(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        result_id, path = _legacy_blob(tmp_path, "legacy")

        with pytest.raises(ValueError, match="unknown result id"):
            store.read(result_id)
        assert not os.path.exists(path)

    def test_a_legacy_id_is_never_readable_with_a_scope(self, tmp_path):
        store = ResultStore(root=str(tmp_path), allow_legacy=True)
        result_id, _ = _legacy_blob(tmp_path, "legacy")

        with pytest.raises(ValueError, match="unknown result id"):
            store.read(result_id, scope=ResultScope(tenant="tenant-a"))

    def test_a_write_sweeps_legacy_blobs_when_not_allowed(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        result_id, path = _legacy_blob(tmp_path, "legacy")

        store.put("new")

        assert not os.path.exists(path)
        with pytest.raises(ValueError, match="unknown result id"):
            store.read(result_id)


class TestRetention:
    def test_the_hard_ttl_is_absolute(self, tmp_path):
        now = [1000.0]
        store = ResultStore(
            root=str(tmp_path),
            ttl=30,
            idle_ttl=10,
            clock=lambda: now[0],
        )
        result_id = store.put("hello")[0]

        for _ in range(3):
            now[0] += 9
            assert store.read(result_id) == "hello"

        now[0] += 4
        with pytest.raises(ValueError, match="expired"):
            store.read(result_id)

    def test_reads_extend_the_idle_ttl(self, tmp_path):
        now = [1000.0]
        store = ResultStore(
            root=str(tmp_path),
            ttl=1000,
            idle_ttl=10,
            clock=lambda: now[0],
        )
        result_id = store.put("hello")[0]

        now[0] += 9
        assert store.read(result_id) == "hello"
        now[0] += 9
        assert store.read(result_id) == "hello"

    def test_a_read_past_the_end_still_extends_the_idle_ttl(self, tmp_path):
        now = [1000.0]
        store = ResultStore(
            root=str(tmp_path),
            ttl=1000,
            idle_ttl=10,
            clock=lambda: now[0],
        )
        result_id = store.put("hello")[0]

        now[0] += 9
        assert store.read(result_id, offset=100) == ""
        now[0] += 9
        assert store.read(result_id) == "hello"

    def test_stat_does_not_extend_the_idle_ttl(self, tmp_path):
        now = [1000.0]
        store = ResultStore(
            root=str(tmp_path),
            ttl=1000,
            idle_ttl=10,
            clock=lambda: now[0],
        )
        result_id = store.put("hello")[0]

        now[0] += 9
        assert store.stat(result_id).last_access_at == 1000.0
        now[0] += 2
        with pytest.raises(ValueError, match="expired"):
            store.stat(result_id)

    def test_idle_ttl_must_be_positive_or_none(self, tmp_path):
        for value in (0, -1):
            with pytest.raises(ValueError, match="idle_ttl"):
                ResultStore(root=str(tmp_path), idle_ttl=value)

    def test_storage_pressure_keeps_the_newest_result_readable(self, tmp_path):
        store = ResultStore(root=str(tmp_path), quota=1)
        result_id = store.put("x" * 100)[0]

        assert store.read(result_id) == "x" * 100


class TestMetadata:
    def test_stat_reports_owner_content_and_digest(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        scope = ResultScope(tenant="tenant-a", session="session-1")
        text = '{"answer": 42}'
        info = store.put_result(
            text,
            scope=scope,
            tool_name="lookup",
            content_type="application/json",
        )

        assert info.result_id == store.stat(info.result_id, scope=scope).result_id
        assert info.size_bytes == len(text.encode("utf-8"))
        assert info.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert info.content_type == "application/json"
        assert info.tool_name == "lookup"
        assert info.tenant == "tenant-a"
        assert info.session == "session-1"
        assert info.legacy is False
        assert info.created_at <= info.last_access_at
        assert info.expires_at > info.created_at

    def test_truncate_result_records_tool_and_content_type(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        payload = json.dumps({"items": ["x" * 200]})
        truncated = truncate_result(
            payload,
            max_size=40,
            tool_name="lookup",
            store=store,
        )

        info = store.stat(truncated.result_id)
        assert info.tool_name == "lookup"
        assert info.content_type == "application/json"
        assert info.size_bytes == len(payload.encode("utf-8"))

    def test_a_write_sweeps_orphan_blobs_and_temp_files(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        orphan_id, orphan_path = _legacy_blob(tmp_path, "orphan", char="b")
        temp_path = os.path.join(str(tmp_path), ".res_deadbeef.json.tmp")
        with open(temp_path, "w", encoding="utf-8") as stream:
            stream.write("{}")

        store.put("kept")

        assert not os.path.exists(orphan_path)
        assert not os.path.exists(temp_path)
        assert not os.path.exists(os.path.join(str(tmp_path), f"{orphan_id}.json"))

    def test_a_write_sweeps_metadata_without_a_blob(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        stray = os.path.join(str(tmp_path), f"res_{'c' * 12}.json")
        with open(stray, "w", encoding="utf-8") as stream:
            json.dump({"id": f"res_{'c' * 12}"}, stream)

        store.put("kept")

        assert not os.path.exists(stray)


class TestRegistryWiring:
    def test_an_injected_store_enables_persistence(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        registry = _registry_with_big(store)

        assert "read_result" in registry
        assert registry._result_store is store

        result = registry.invoke("big", {}, execution_mode="thread").result
        result_id = _extract_id(result)
        info = store.stat(result_id)
        assert info.tool_name == "big"
        assert info.content_type == "text/plain"
        assert registry.read_result(result_id) == "x" * 400

    def test_retention_settings_reach_the_built_store(self):
        registry = ToolRegistry(
            persist_large_results=True,
            result_ttl_seconds=12,
            result_idle_ttl_seconds=3,
            result_quota_bytes=99,
            result_scope_policy="required",
        )

        assert registry._result_store.ttl == 12
        assert registry._result_store.idle_ttl == 3
        assert registry._result_store.quota == 99
        assert registry._result_store.scope_policy == "required"

    def test_the_provider_scope_is_used_for_writes_and_reads(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        holder = [ResultScope(tenant="tenant-a", session="session-1")]
        registry = _registry_with_big(store, result_scope_provider=lambda: holder[0])

        result_id = _extract_id(
            registry.invoke("big", {}, execution_mode="thread").result
        )
        assert store.stat(result_id, scope=holder[0]).size_bytes == 400
        assert registry.read_result(result_id) == "x" * 400

        holder[0] = ResultScope(tenant="tenant-a", session="session-2")
        with pytest.raises(ValueError, match="unknown result id"):
            registry.read_result(result_id)

    def test_the_context_scope_is_used_when_no_provider_is_set(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        registry = _registry_with_big(store)
        scope = ResultScope(tenant="tenant-a", session="session-1")

        with use_result_scope(scope):
            result_id = _extract_id(
                registry.invoke("big", {}, execution_mode="thread").result
            )
            assert registry.read_result(result_id) == "x" * 400

        with pytest.raises(ValueError, match="unknown result id"):
            registry.read_result(result_id)

    def test_session_required_rejects_a_tenant_only_provider(self, tmp_path):
        store = ResultStore(root=str(tmp_path), scope_policy="session_required")
        registry = _registry_with_big(
            store,
            result_scope_provider=lambda: ResultScope(tenant="tenant-a"),
        )

        result = registry.invoke("big", {}, execution_mode="thread")
        assert "session is required" in result.message

    def test_invalid_registry_settings_are_refused(self):
        with pytest.raises(TypeError, match="result_store"):
            ToolRegistry(result_store="not-a-store")
        with pytest.raises(ValueError, match="result_scope_policy"):
            ToolRegistry(persist_large_results=True, result_scope_policy="bogus")
        with pytest.raises(TypeError, match="result_scope_provider"):
            ToolRegistry(result_scope_provider=42)
        with pytest.raises(ValueError, match="ttl must be positive"):
            ToolRegistry(persist_large_results=True, result_ttl_seconds=0)

    def test_multimodal_results_record_the_scope_and_tool(self, tmp_path):
        store = ResultStore(root=str(tmp_path))
        scope = ResultScope(tenant="tenant-a", session="session-1")
        registry = ToolRegistry(result_store=store)

        def blocks(n: int = 400) -> list:
            """Return a large content block list."""
            return [{"type": "text", "text": "y" * n}]

        registry.register(
            Tool.from_function(blocks, metadata=ToolMetadata(max_result_size=50))
        )

        with use_result_scope(scope):
            result = registry.invoke("blocks", {}, execution_mode="thread").result

        notice = result[-1]["text"]
        result_id = _extract_id(notice)
        info = store.stat(result_id, scope=scope)
        assert info.tool_name == "blocks"
        assert info.content_type == "application/json"

    def test_library_mode_keeps_the_unscoped_default(self):
        registry = ToolRegistry(default_max_result_size=50)
        assert registry._result_store is None
        assert registry._current_result_scope() is None

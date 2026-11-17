"""Memory operations with authorization, shared by the REST routes and the MCP tools.

Every call resolves the target tenant, authorizes the principal for it *before* the
tenant is looked up, and then works only inside that tenant's collection. Writes and
denials are reported to the audit recorder.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from memory_mcp.admin.audit import Audit
from memory_mcp.admin.authz import authorize, repo_allowed, resolve_tenant, stamped_agent
from memory_mcp.admin.models import AuthzError, Principal
from memory_mcp.admin.tenants import TenantService
from memory_mcp.store import COLLECTION, MemoryRecord, MemoryStore


class MemoryOps:
    def __init__(self, store: MemoryStore, tenants: TenantService | None,
                 settings: Callable[[], dict[str, Any]], audit: Audit):
        """`tenants` is None in legacy mode: one collection, no `tenant` argument accepted."""
        self._store = store
        self._tenants = tenants
        self._settings = settings
        self._audit = audit

    # ── plumbing ──────────────────────────────────────────────────────────────
    def _target(self, p: Principal, action: str, requested: str | None,
                attempt: dict[str, Any]) -> str:
        """Authorize and return the collection; records the tenant in `attempt` for auditing."""
        if self._tenants is None:
            if requested is not None:
                raise AuthzError(400, "tenant_not_supported", "This server has no tenants; omit tenant")
            return COLLECTION
        tenant = resolve_tenant(p, requested, self._tenants, self._settings())
        attempt["tenant"] = tenant
        authorize(p, action, tenant)  # before any lookup: no existence leak
        return self._tenants.collection_for(tenant)

    def _deny(self, p: Principal, action: str, attempt: dict[str, Any], target: str | None,
              rule: str) -> None:
        self._audit.record(p, action, attempt.get("tenant"), target, "denied", {"rule": rule})

    def _run(self, p: Principal, action: str, requested: str | None, target: str | None,
             fn: Callable[[str, dict[str, Any]], Any]) -> Any:
        attempt: dict[str, Any] = {"tenant": requested}
        try:
            collection = self._target(p, action, requested, attempt)
            return fn(collection, attempt)
        except AuthzError as e:
            self._deny(p, action, attempt, target, e.code)
            raise

    def _get_visible(self, p: Principal, action: str, memory_id: str, collection: str,
                     attempt: dict[str, Any]) -> MemoryRecord | None:
        """The record in `collection`, or None if missing or outside the caller's repo patterns."""
        record = self._store.get(memory_id, collection=collection)
        if record is not None and not repo_allowed(p, record.source_repo):
            self._deny(p, action, attempt, memory_id, "source_repo_not_allowed")
            return None
        return record

    def _filter(self, p: Principal, records: list[MemoryRecord]) -> list[MemoryRecord]:
        return [r for r in records if repo_allowed(p, r.source_repo)]

    def _check_type(self, type: str) -> None:
        allowed = self._settings().get("allowed_types") if self._tenants is not None else None
        if allowed and type not in allowed:
            raise AuthzError(400, "type_not_allowed",
                             f"type '{type}' is not allowed; allowed: {', '.join(allowed)}")

    # ── reads ─────────────────────────────────────────────────────────────────
    def list(self, p: Principal, tenant: str | None, *, type: str | None = None,
             source_repo: str | None = None, agent: str | None = None, tags: list[str] | None = None,
             limit: int = 1000, offset: str | None = None) -> tuple[list[MemoryRecord], str | None]:
        def go(collection, _attempt):
            if source_repo is not None and not repo_allowed(p, source_repo):
                return [], None
            records, next_offset = self._store.list_memories(
                filter_type=type, filter_source_repo=source_repo, filter_agent=agent,
                filter_tags=tags, limit=limit, offset=offset, collection=collection)
            return self._filter(p, records), next_offset
        return self._run(p, "list_memories", tenant, None, go)

    def search(self, p: Principal, tenant: str | None, query: str, limit: int = 10,
               filter_type: str | None = None, filter_source_repo: str | None = None) -> list[MemoryRecord]:
        def go(collection, _attempt):
            if filter_source_repo is not None and not repo_allowed(p, filter_source_repo):
                return []
            records = self._store.search(query=query, limit=limit, filter_type=filter_type,
                                         filter_source_repo=filter_source_repo, collection=collection)
            return self._filter(p, records)
        return self._run(p, "search_memories", tenant, None, go)

    def get(self, p: Principal, tenant: str | None, memory_id: str) -> MemoryRecord | None:
        action = "search_memories"
        return self._run(p, action, tenant, memory_id,
                         lambda collection, attempt: self._get_visible(p, action, memory_id, collection, attempt))

    # ── writes ────────────────────────────────────────────────────────────────
    def save(self, p: Principal, tenant: str | None, *, type: str, name: str, content: str,
             source_repo: str = "global", agent: str | None = None, tags: list[str] | None = None) -> MemoryRecord:
        action = "save_memory"
        tags = list(tags or [])

        def go(collection, attempt):
            if not repo_allowed(p, source_repo):
                raise AuthzError(403, "source_repo_not_allowed",
                                 f"source_repo '{source_repo}' is not allowed for this caller")
            self._check_type(type)
            match = self._store.find_by_name(name, source_repo, collection=collection)
            if match:
                record = self._store.update(match.id, content, tags, type=type, collection=collection)
            else:
                now = MemoryStore.now_iso()
                record = self._store.upsert(MemoryRecord(
                    id=MemoryStore.new_id(), type=type, name=name, content=content,
                    source_repo=source_repo, agent=stamped_agent(p, agent), tags=tags,
                    created_at=now, updated_at=now,
                ), collection=collection)
            self._audit.record(p, action, attempt.get("tenant"), record.id, "allowed")
            return record
        return self._run(p, action, tenant, name, go)

    def update(self, p: Principal, tenant: str | None, memory_id: str, content: str,
               tags: list[str] | None = None) -> MemoryRecord | None:
        action = "save_memory"

        def go(collection, attempt):
            if self._get_visible(p, action, memory_id, collection, attempt) is None:
                return None
            record = self._store.update(memory_id, content, list(tags or []), collection=collection)
            if record is not None:
                self._audit.record(p, action, attempt.get("tenant"), memory_id, "allowed")
            return record
        return self._run(p, action, tenant, memory_id, go)

    def delete(self, p: Principal, tenant: str | None, memory_id: str) -> bool:
        action = "delete_memory"

        def go(collection, attempt):
            if self._get_visible(p, action, memory_id, collection, attempt) is None:
                return False
            ok = self._store.delete(memory_id, collection=collection)
            if ok:
                self._audit.record(p, action, attempt.get("tenant"), memory_id, "allowed")
            return ok
        return self._run(p, action, tenant, memory_id, go)

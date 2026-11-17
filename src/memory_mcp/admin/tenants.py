"""Tenant registry: which tenants exist, their Qdrant collections, and cached lookups."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from memory_mcp.admin.models import AuthzError, Tenant
from memory_mcp.admin.store import AdminStore
from memory_mcp.kg.models import GRAPH_ID_RE
from memory_mcp.kg.store import EntityFilter
from memory_mcp.store import COLLECTION, MemoryStore

_STATUSES = ("active", "disabled")


class TenantService:
    def __init__(self, store: AdminStore, memory_store: MemoryStore, kg_store=None,
                 ttl: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self._store = store
        self._memory = memory_store
        self._kg = kg_store
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: dict[str, Tenant] = {}
        self._loaded_at: float | None = None

    # cache
    def _refresh(self) -> None:
        with self._lock:
            self._snapshot = {t.id: t for t in self._store.list_tenants()}
            self._loaded_at = self._clock()

    def _invalidate(self) -> None:
        with self._lock:
            self._loaded_at = None

    def _fresh(self) -> bool:
        return self._loaded_at is not None and self._clock() - self._loaded_at < self._ttl

    def all(self) -> list[Tenant]:
        if not self._fresh():
            self._refresh()
        return sorted(self._snapshot.values(), key=lambda t: t.id)

    def get(self, id: str) -> Tenant | None:
        if not self._fresh():
            self._refresh()
        t = self._snapshot.get(id)
        if t is None:
            self._refresh()
            t = self._snapshot.get(id)
        return t

    def collection_for(self, id: str) -> str:
        t = self.get(id)
        if t is None:
            raise AuthzError(404, "unknown_tenant", f"Unknown tenant '{id}'")
        if t.status == "disabled":
            raise AuthzError(403, "tenant_disabled", f"Tenant '{id}' is disabled")
        return t.collection

    # lifecycle
    def bootstrap(self, tenant_id: str | None, name: str | None, existing_graphs: list[str]) -> None:
        if self._store.list_tenants():
            return
        if not tenant_id:
            raise RuntimeError("BOOTSTRAP_TENANT_ID required")
        self._store.insert_tenant(
            Tenant(id=tenant_id, name=name or tenant_id, description="", collection=COLLECTION,
                   status="active"),
            by="bootstrap")
        self._store.put_settings({"default_tenant": tenant_id}, by="bootstrap")
        if self._kg is not None:
            self._kg.ensure_graph(tenant_id)
        for gid in existing_graphs:
            if gid == tenant_id:
                continue
            collection = f"memories_{gid}"
            self._store.insert_tenant(
                Tenant(id=gid, name=gid, description="", collection=collection, status="active"),
                by="bootstrap")
            self._memory.ensure_collection(collection)
            if self._kg is not None:
                self._kg.ensure_graph(gid)
        self._invalidate()

    def create(self, id: str, name: str, description: str, by: str) -> Tenant:
        if not GRAPH_ID_RE.match(id):
            raise ValueError(f"invalid tenant id {id!r}")
        if self._store.get_tenant(id) is not None:
            raise ValueError(f"tenant '{id}' already exists")
        tenant = Tenant(id=id, name=name, description=description,
                        collection=f"memories_{id}", status="active")
        self._store.insert_tenant(tenant, by=by)
        try:
            self._memory.ensure_collection(tenant.collection)
            if self._kg is not None:
                self._kg.ensure_graph(id)
        except Exception:
            self._store.delete_tenant(id)
            raise
        finally:
            self._invalidate()
        return tenant

    def set_status(self, id: str, status: str) -> Tenant | None:
        if status not in _STATUSES:
            raise ValueError(f"invalid status {status!r}")
        t = self._store.update_tenant(id, status=status)
        self._invalidate()
        return t

    def rename(self, id: str, name: str | None = None, description: str | None = None) -> Tenant | None:
        t = self._store.update_tenant(id, name=name, description=description)
        self._invalidate()
        return t

    def stats(self) -> list[dict]:
        keys = [k for k in self._store.list_keys() if k.revoked_at is None]
        out = []
        for t in self.all():
            out.append({
                "id": t.id, "name": t.name, "description": t.description, "status": t.status,
                "memories": self._memory.count(t.collection),
                "entities": self._kg.count_entities(t.id, EntityFilter()) if self._kg is not None else 0,
                "keys": sum(1 for k in keys if t.id in k.tenants),
            })
        return out

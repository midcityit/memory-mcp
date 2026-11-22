"""Admin persistence: protocol plus the in-memory implementation used by tests."""
from __future__ import annotations

import base64
import dataclasses
import json
import threading
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone
from typing import Any, Protocol

from memory_mcp.admin.models import ApiKey, RoleMapping, Tenant


class AdminStore(Protocol):
    def ensure_schema(self) -> None: ...
    def ping(self) -> None: ...
    def bootstrap_lock(self) -> AbstractContextManager[None]:
        """Held while bootstrapping tenants, so only one process (worker) bootstraps at a time."""
        ...
    def list_tenants(self) -> list[Tenant]: ...
    def get_tenant(self, id: str) -> Tenant | None: ...
    def insert_tenant(self, t: Tenant, by: str) -> None: ...
    def update_tenant(self, id: str, *, name: str | None = None, description: str | None = None,
                      status: str | None = None) -> Tenant | None: ...
    def delete_tenant(self, id: str) -> None: ...
    def insert_key(self, k: ApiKey) -> None: ...
    def get_key(self, id: str) -> ApiKey | None: ...
    def list_keys(self) -> list[ApiKey]: ...
    def set_key_expiry(self, id: str, at: datetime) -> None: ...
    def revoke_key(self, id: str, at: datetime) -> bool: ...
    def touch_key(self, id: str, at: datetime) -> None: ...
    def list_mappings(self) -> list[RoleMapping]: ...
    def replace_mappings(self, rows: list[RoleMapping], by: str) -> None: ...
    def get_settings(self) -> dict[str, Any]: ...
    def put_settings(self, values: dict[str, Any], by: str) -> None: ...
    def add_audit(self, row: dict) -> None: ...
    def query_audit(self, *, tenant: str | None = None, principal: str | None = None,
                    action: str | None = None, outcome: str | None = None,
                    since: datetime | None = None, until: datetime | None = None,
                    limit: int = 100, cursor: str | None = None) -> tuple[list[dict], str | None]: ...
    def purge_audit(self, before: datetime) -> int: ...


def encode_cursor(ts: datetime, id: int) -> str:
    raw = json.dumps([ts.isoformat(), id]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        ts, id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(ts), int(id)
    except (ValueError, TypeError) as e:
        raise ValueError("invalid cursor") from e


class InMemoryAdminStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tenants: dict[str, Tenant] = {}
        self._keys: dict[str, ApiKey] = {}
        self._mappings: list[RoleMapping] = []
        self._settings: dict[str, Any] = {}
        self._audit: list[dict] = []
        self._next_audit_id = 1

    def ensure_schema(self) -> None:
        pass

    def ping(self) -> None:
        pass

    def bootstrap_lock(self) -> AbstractContextManager[None]:
        # One process only: nothing to serialize against.
        return nullcontext()

    # tenants
    def list_tenants(self) -> list[Tenant]:
        with self._lock:
            return sorted(self._tenants.values(), key=lambda t: t.id)

    def get_tenant(self, id: str) -> Tenant | None:
        with self._lock:
            return self._tenants.get(id)

    def insert_tenant(self, t: Tenant, by: str) -> None:
        with self._lock:
            if t.id in self._tenants:
                raise ValueError("exists")
            self._tenants[t.id] = t

    def update_tenant(self, id: str, *, name: str | None = None, description: str | None = None,
                      status: str | None = None) -> Tenant | None:
        with self._lock:
            cur = self._tenants.get(id)
            if cur is None:
                return None
            changes = {k: v for k, v in (("name", name), ("description", description),
                                         ("status", status)) if v is not None}
            new = dataclasses.replace(cur, **changes)
            self._tenants[id] = new
            return new

    def delete_tenant(self, id: str) -> None:
        with self._lock:
            self._tenants.pop(id, None)

    # keys
    def insert_key(self, k: ApiKey) -> None:
        with self._lock:
            if k.id in self._keys:
                raise ValueError("exists")
            self._keys[k.id] = k

    def get_key(self, id: str) -> ApiKey | None:
        with self._lock:
            return self._keys.get(id)

    def list_keys(self) -> list[ApiKey]:
        with self._lock:
            return sorted(self._keys.values(), key=lambda k: (k.created_at, k.id))

    def _update_key(self, id: str, **changes: Any) -> bool:
        cur = self._keys.get(id)
        if cur is None:
            return False
        self._keys[id] = dataclasses.replace(cur, **changes)
        return True

    def set_key_expiry(self, id: str, at: datetime) -> None:
        with self._lock:
            self._update_key(id, expires_at=at)

    def revoke_key(self, id: str, at: datetime) -> bool:
        """Revoke once; returns False if unknown or already revoked."""
        with self._lock:
            cur = self._keys.get(id)
            if cur is None or cur.revoked_at is not None:
                return False
            return self._update_key(id, revoked_at=at)

    def touch_key(self, id: str, at: datetime) -> None:
        with self._lock:
            self._update_key(id, last_used_at=at)

    # role mappings
    def list_mappings(self) -> list[RoleMapping]:
        with self._lock:
            return sorted(self._mappings, key=lambda m: (m.group_id, m.tenant, m.role))

    def replace_mappings(self, rows: list[RoleMapping], by: str) -> None:
        with self._lock:
            self._mappings = list(dict.fromkeys(rows))

    # settings
    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def put_settings(self, values: dict[str, Any], by: str) -> None:
        with self._lock:
            self._settings.update(values)

    # audit
    def add_audit(self, row: dict) -> None:
        with self._lock:
            stored = dict(row)
            if stored.get("ts") is None:
                stored["ts"] = datetime.now(timezone.utc)
            stored["id"] = self._next_audit_id
            self._next_audit_id += 1
            stored.setdefault("detail", None)
            self._audit.append(stored)

    def query_audit(self, *, tenant: str | None = None, principal: str | None = None,
                    action: str | None = None, outcome: str | None = None,
                    since: datetime | None = None, until: datetime | None = None,
                    limit: int = 100, cursor: str | None = None) -> tuple[list[dict], str | None]:
        with self._lock:
            rows = [r for r in self._audit
                    if (tenant is None or r.get("tenant") == tenant)
                    and (principal is None or r.get("principal_id") == principal)
                    and (action is None or r.get("action") == action)
                    and (outcome is None or r.get("outcome") == outcome)
                    and (since is None or r["ts"] >= since)
                    and (until is None or r["ts"] <= until)]
            rows.sort(key=lambda r: (r["ts"], r["id"]), reverse=True)
            if cursor is not None:
                cts, cid = decode_cursor(cursor)
                rows = [r for r in rows if (r["ts"], r["id"]) < (cts, cid)]
            page = rows[:limit]
            nxt = encode_cursor(page[-1]["ts"], page[-1]["id"]) if len(rows) > limit else None
            return [dict(r) for r in page], nxt

    def purge_audit(self, before: datetime) -> int:
        with self._lock:
            keep = [r for r in self._audit if r["ts"] >= before]
            removed = len(self._audit) - len(keep)
            self._audit = keep
            return removed

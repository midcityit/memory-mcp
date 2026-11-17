"""Copy memories from one tenant to another (spec 5.8).

Copy, never move: the source collection is only read. Each copy keeps the source
vector as is and carries a ``copied_from`` payload ``{tenant, id, at, by}``. A memory
whose ``(name, source_repo)`` already exists in the destination updates that memory
in place (keeping its id) instead of creating a duplicate.
"""
from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable

from memory_mcp.admin.models import AuthzError
from memory_mcp.admin.tenants import TenantService
from memory_mcp.store import MemoryRecord, MemoryStore

MAX_COPY_IDS = 500


def _is_point_id(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def copy_memories(memory_store: MemoryStore, tenants: TenantService, from_: str, to: str,
                  ids: list[str], dry_run: bool, by: str,
                  on_write: Callable[[str, str], None] | None = None) -> dict:
    """Copy `ids` from tenant `from_` into tenant `to`.

    Returns ``{created: [{id, name}], updated: [{id, name}], missing: [id]}``. With
    `dry_run` nothing is written and the result says what would happen. `on_write`
    is called with ``(destination_id, source_id)`` after each real write.
    Authorization is the caller's job; this checks only the request itself.
    """
    if from_ == to:
        raise AuthzError(400, "same_tenant", "Source and destination tenants must differ")
    if len(ids) > MAX_COPY_IDS:
        raise AuthzError(400, "too_many_ids", f"Copy at most {MAX_COPY_IDS} memories per request")
    source = tenants.collection_for(from_)
    dest = tenants.collection_for(to)

    created: list[dict] = []
    updated: list[dict] = []
    missing: list[str] = []
    # Destination state as this call leaves it, so two sources with the same
    # (name, source_repo) resolve the same way in a dry run as in a real one.
    planned: dict[tuple[str, str], MemoryRecord] = {}

    for source_id in dict.fromkeys(ids):
        point = memory_store.get_point(source_id, source) if _is_point_id(source_id) else None
        if point is None:
            missing.append(source_id)
            continue
        record, vector = point
        key = (record.name, record.source_repo)
        existing = planned.get(key) or memory_store.find_by_name(record.name, record.source_repo,
                                                                 collection=dest)
        now = MemoryStore.now_iso()
        if existing is not None:
            copy = dataclasses.replace(existing, type=record.type, content=record.content,
                                       tags=list(record.tags), updated_at=now, stale=False)
            bucket = updated
        else:
            copy = dataclasses.replace(record, id=MemoryStore.new_id(), tags=list(record.tags),
                                       created_at=now, updated_at=now, stale=False)
            bucket = created
        if not dry_run:
            memory_store.put_point(copy, vector, dest, extra_payload={
                "copied_from": {"tenant": from_, "id": record.id, "at": now, "by": by},
            })
            if on_write is not None:
                on_write(copy.id, record.id)
        planned[key] = copy
        bucket.append({"id": copy.id, "name": copy.name})

    return {"created": created, "updated": updated, "missing": missing}

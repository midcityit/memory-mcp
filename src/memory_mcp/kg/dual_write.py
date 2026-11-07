"""RCL-50: dual-write GraphStore for the Qdrant -> Postgres transition window.

Wraps a PRIMARY store (reads + writes) and a SECONDARY store (writes only). Every
mutating call lands in both; every read goes to the primary. This keeps the two
stores consistent while we cut over: run the one-time backfill
(scripts/kg_migrate_qdrant_to_postgres.py), then enable dual-write so no new write
is lost, let it bake, verify parity, and finally flip KG_BACKEND to make Postgres
primary (optionally keeping Qdrant as the secondary until decommission in RCL-53).

Wiring (server): if KG_DUAL_WRITE=true, build_kg wraps the two concrete stores as
DualWriteGraphStore(primary, secondary). The wrapper exposes the full GraphStore
surface; reads delegate to primary, writes fan out to both. A secondary write
failure is logged and swallowed by default (primary is the source of truth during
the window) unless KG_DUAL_WRITE_STRICT=true.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("memory_mcp.kg.dual_write")

# Methods that MUTATE state — fan out to both stores.
_WRITE_METHODS = frozenset({
    "ensure_graph", "ensure_xrefs",
    "upsert_entity", "delete_entity_hard",
    "put_edge",
    "put_xref", "mark_xrefs_dangling", "delete_xref", "delete_xrefs_touching",
})


class DualWriteGraphStore:
    """Primary for reads; primary + secondary for writes. Same surface as the
    concrete stores, so KGQuery/KGService are unchanged."""

    def __init__(self, primary, secondary, *, strict: bool = False):
        self._primary = primary
        self._secondary = secondary
        self._strict = strict

    # embedder is read off the primary (both share the same Embedder instance).
    @property
    def embedder(self):
        return self._primary.embedder

    # Feature marker: only advertise SQL traversal if the PRIMARY (the read store)
    # supports it, since reads come from primary.
    @property
    def supports_sql_traversal(self):
        return getattr(self._primary, "supports_sql_traversal", False)

    def __getattr__(self, name):
        # Called only for attributes not found normally (i.e. the store methods).
        prim_attr = getattr(self._primary, name)
        if name not in _WRITE_METHODS or not callable(prim_attr):
            return prim_attr  # read / non-method -> primary only

        sec_attr = getattr(self._secondary, name, None)

        def fanout(*args, **kwargs):
            result = prim_attr(*args, **kwargs)  # primary is source of truth
            if callable(sec_attr):
                try:
                    sec_attr(*args, **kwargs)
                except Exception:
                    logger.exception("dual-write: secondary %s failed", name)
                    if self._strict:
                        raise
            return result

        return fanout

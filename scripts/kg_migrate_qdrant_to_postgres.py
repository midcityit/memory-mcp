#!/usr/bin/env python3
"""RCL-50: migrate the knowledge graph from Qdrant to Postgres (pgvector + AGE).

Reads every entity/edge/xref from the Qdrant store and writes it into the Postgres
store, RE-EMBEDDING each entity (PostgresGraphStore.upsert_entity recomputes the
vector(384) via the same embedder, so pgvector resolve matches current behaviour).
Then spot-checks counts + a sample resolve for parity.

This is a store-to-store copy (not via the JSONL /kg/export) so it reuses the live
embedder and the exact model objects — no serialization round-trip.

Env:
  QDRANT_URL                 source Qdrant
  KG_PG_DSN                  target Postgres (pgvector + AGE)
  KG_CONFIG_DIR              optional registry/catalog dir (else packaged)
  MIGRATE_GRAPHS             optional comma list (default: all registry graphs)

Usage:
  python scripts/kg_migrate_qdrant_to_postgres.py            # migrate + verify
  python scripts/kg_migrate_qdrant_to_postgres.py --verify   # verify only (no writes)

Dual-write window: during the transition the server runs with a DualWriteGraphStore
(see src/memory_mcp/kg/dual_write.py) so every write lands in BOTH stores. Run this
backfill first, flip KG_DUAL_WRITE=true, let it bake, then cut KG_BACKEND=postgres.
"""
from __future__ import annotations

import argparse
import os
import sys

from qdrant_client import QdrantClient

from memory_mcp.embedding import Embedder
from memory_mcp.kg.registry import Registry
from memory_mcp.kg.store import EntityFilter, QdrantGraphStore
from memory_mcp.kg.pg_store import PostgresGraphStore


def _graphs() -> list[str]:
    reg = Registry.load(config_dir=os.environ["KG_CONFIG_DIR"]) if os.environ.get("KG_CONFIG_DIR") else Registry.load()
    want = os.environ.get("MIGRATE_GRAPHS")
    allg = list(reg.graphs)
    return [g for g in allg if g in want.split(",")] if want else allg


def _stores():
    embedder = Embedder()
    src = QdrantGraphStore(QdrantClient(url=os.environ["QDRANT_URL"]), embedder)
    dst = PostgresGraphStore(os.environ["KG_PG_DSN"], embedder)
    return src, dst


def migrate(src: QdrantGraphStore, dst: PostgresGraphStore, graphs: list[str]) -> dict:
    report = {}
    for g in graphs:
        ne = nd = nx = 0
        for e in src.iter_entities(g):
            dst.upsert_entity(g, e)   # recomputes embedding in pgvector
            ne += 1
        for ed in src.iter_edges(g):
            dst.put_edge(g, ed)
            nd += 1
        for x in src.xrefs_for_graph(g):
            if x.src.startswith(f"{g}::"):   # export-once-by-source-graph, same rule as /kg/export
                dst.put_xref(x)
                nx += 1
        report[g] = {"entities": ne, "edges": nd, "xrefs": nx}
    return report


def verify(src: QdrantGraphStore, dst: PostgresGraphStore, graphs: list[str]) -> dict:
    """Count parity per graph + a sample resolve top-k parity."""
    out = {}
    for g in graphs:
        sc = src.count_entities(g, EntityFilter(include_retired=True))
        dc = dst.count_entities(g, EntityFilter(include_retired=True))
        # sample resolve parity: take a few entities, resolve their display_name in
        # both stores, compare the top-1 key.
        sample = []
        for i, e in enumerate(src.iter_entities(g)):
            if i >= 5:
                break
            sample.append(e)
        matches = 0
        for e in sample:
            vec = src.embedder.embed(f"{e.display_name} {' '.join(e.aliases)} {e.type}")
            s_top = src.search_entities(g, vec, EntityFilter(), 1)
            d_top = dst.search_entities(g, vec, EntityFilter(), 1)
            if s_top and d_top and s_top[0][0].key == d_top[0][0].key:
                matches += 1
        out[g] = {"src_entities": sc, "dst_entities": dc, "count_ok": sc == dc,
                  "resolve_sample": len(sample), "resolve_top1_match": matches}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="verify parity only, no writes")
    args = ap.parse_args()
    graphs = _graphs()
    src, dst = _stores()
    if not args.verify:
        rep = migrate(src, dst, graphs)
        print("MIGRATED:")
        for g, c in rep.items():
            print(f"  {g}: {c['entities']} entities, {c['edges']} edges, {c['xrefs']} xrefs")
    ver = verify(src, dst, graphs)
    print("VERIFY:")
    ok = True
    for g, v in ver.items():
        flag = "OK" if v["count_ok"] and v["resolve_top1_match"] == v["resolve_sample"] else "MISMATCH"
        if flag != "OK":
            ok = False
        print(f"  {g}: src={v['src_entities']} dst={v['dst_entities']} count_ok={v['count_ok']} "
              f"resolve_top1={v['resolve_top1_match']}/{v['resolve_sample']} -> {flag}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

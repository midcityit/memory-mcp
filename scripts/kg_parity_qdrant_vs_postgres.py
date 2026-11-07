#!/usr/bin/env python3
"""RCL-51: behavioral parity diff — Qdrant vs Postgres KG backends.

Builds a KGQuery against BOTH backends over the same seeded acceptance data and
calls every query method with identical args, then diffs the normalized results.
Covers all 9 query endpoints named in the RCL-51 acceptance:
  resolve, get_entity, find, traverse, path, impact, overview, related_across, for_memory

Complements scripts/kg_acceptance.py (which runs the §10 checks against ONE live
backend over REST): run that twice (KG_BACKEND=qdrant / =postgres) for the "§10
green on Postgres incl. depth-3 p95 SLO" criterion; run THIS for the "no
behavioral diff vs Qdrant" criterion.

Env:
  QDRANT_URL                 Qdrant with the seeded mcit/vtv graphs
  KG_PG_DSN                  Postgres with the SAME data migrated (RCL-50 backfill)
  KG_CONFIG_DIR              optional registry/catalog dir

Exit 0 iff every method's result matches across backends (order-insensitive where
the contract allows). Prints a per-method PASS/DIFF table and the first diff.
"""
from __future__ import annotations

import json
import os
import sys

from qdrant_client import QdrantClient

from memory_mcp.embedding import Embedder
from memory_mcp.kg import KG
from memory_mcp.kg.query import KGQuery
from memory_mcp.kg.registry import Registry
from memory_mcp.kg.store import QdrantGraphStore
from memory_mcp.kg.pg_store import PostgresGraphStore

GRAPH = "mcit"
DEP = "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"
NODE = "kubernetes:mcit-k8s/_cluster/core/Node/opi-5"


def _reg():
    return Registry.load(config_dir=os.environ["KG_CONFIG_DIR"]) if os.environ.get("KG_CONFIG_DIR") else Registry.load()


def _query(store, reg):
    return KGQuery(reg, store, max_chars=6000)


def _norm(obj):
    """Order-insensitive normalization: sort any list of dicts/keys by a stable
    json key so a different row order between backends is not a false diff. The
    contracts are set-like for node/edge/result lists."""
    if isinstance(obj, dict):
        return {k: _norm(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        normed = [_norm(x) for x in obj]
        try:
            return sorted(normed, key=lambda x: json.dumps(x, sort_keys=True, default=str))
        except Exception:
            return normed
    return obj


def main() -> int:
    reg = _reg()
    emb = Embedder()
    q_q = _query(QdrantGraphStore(QdrantClient(url=os.environ["QDRANT_URL"]), emb), reg)
    q_p = _query(PostgresGraphStore(os.environ["KG_PG_DSN"], emb), reg)

    # (name, callable taking a KGQuery) — identical args to both.
    cases = [
        ("resolve", lambda q: q.resolve("memory-mcp", graphs=GRAPH)),
        ("get_entity", lambda q: q.get_entity(GRAPH, DEP)),
        ("find", lambda q: q.find(GRAPH, kind="work")),
        ("traverse_d3", lambda q: q.traverse(GRAPH, DEP, max_depth=3)),
        ("traverse_d6_both", lambda q: q.traverse(GRAPH, DEP, direction="both", max_depth=6)),
        ("path", lambda q: q.path(GRAPH, DEP, NODE)),
        ("impact", lambda q: q.impact(GRAPH, NODE)),
        ("overview", lambda q: q.overview(graphs=GRAPH)),
        ("related_across", lambda q: q.related_across(GRAPH, DEP)),
        ("for_memory", lambda q: q.for_memory("d73c22c3-9a7d-4fe2-a5b8-5668341cc9d3")),
    ]

    rows, first_diff, ok = [], None, True
    for name, fn in cases:
        try:
            rq, rp = _norm(fn(q_q)), _norm(fn(q_p))
        except Exception as e:
            rows.append((name, f"ERROR {type(e).__name__}: {e}"))
            ok = False
            continue
        # 'score' (cosine) can differ at float noise; drop it from comparison.
        def strip_scores(o):
            if isinstance(o, dict):
                return {k: strip_scores(v) for k, v in o.items() if k != "score"}
            if isinstance(o, list):
                return [strip_scores(x) for x in o]
            return o
        match = strip_scores(rq) == strip_scores(rp)
        rows.append((name, "PASS" if match else "DIFF"))
        if not match and first_diff is None:
            first_diff = (name, rq, rp)
            ok = False

    print(f"{'method':18} {'result'}")
    print("-" * 40)
    for name, res in rows:
        print(f"{name:18} {res}")
    if first_diff:
        n, a, b = first_diff
        print(f"\nFirst diff in {n}:\n  qdrant={json.dumps(a, default=str)[:600]}\n  postgres={json.dumps(b, default=str)[:600]}")
    print("\nPARITY:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

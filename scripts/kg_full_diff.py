#!/usr/bin/env python3
"""RCL-64: full content diff (edges keyed by src,relation,dst,valid_from: history rows are distinct), Qdrant vs Postgres, for every graph.

Compares entities, edges and xrefs row-by-row (not just entity counts):
same key set, and the same content per key. Read-only on both stores.
Compares every field, updated_at included (fixed in memory-mcp #51).
Timestamps are compared as instants (UTC, millisecond), not as strings, so
'2026-11-10T07:53:48Z' and '2026-11-10 07:53:48+00:00' are equal.

Env: QDRANT_URL, KG_PG_DSN. Exit 0 iff every graph matches exactly.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from qdrant_client import QdrantClient

from memory_mcp.embedding import Embedder
from memory_mcp.kg.pg_store import PostgresGraphStore
from memory_mcp.kg.registry import Registry
from memory_mcp.kg.store import QdrantGraphStore

TS = {"created_at", "updated_at", "valid_from", "valid_to"}
SKIP = set()


def _ts(v):
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        d = v
    else:
        s = str(v).replace("Z", "+00:00").replace(" ", "T", 1)
        d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return round(d.timestamp() * 1000)


def _norm(obj) -> dict:
    p = dict(vars(obj))
    out = {}
    for k, v in p.items():
        if k in SKIP:
            continue
        if k in TS:
            out[k] = _ts(v)
        elif isinstance(v, list):
            out[k] = sorted(v)
        elif isinstance(v, dict):
            out[k] = json.loads(json.dumps(v, sort_keys=True))
        else:
            out[k] = v
    return out


def _index(rows, keyfn):
    idx = {}
    dup = 0
    for r in rows:
        k = keyfn(r)
        if k in idx:
            dup += 1
        idx[k] = _norm(r)
    return idx, dup


def _cmp(name, src, dst, src_dup, dst_dup):
    sk, dk = set(src), set(dst)
    only_s, only_d = sorted(sk - dk), sorted(dk - sk)
    changed = sorted(k for k in sk & dk if src[k] != dst[k])
    ok = not (only_s or only_d or changed or dst_dup)
    print(f"  {name:9} qdrant={len(src):5} postgres={len(dst):5} "
          f"only_qdrant={len(only_s)} only_pg={len(only_d)} differ={len(changed)} "
          f"pg_dup_keys={dst_dup} -> {'OK' if ok else 'MISMATCH'}")
    for label, keys in (("only_qdrant", only_s), ("only_pg", only_d)):
        for k in keys[:3]:
            print(f"      {label}: {k}")
    for k in changed[:3]:
        a, b = src[k], dst[k]
        fields = sorted(f for f in set(a) | set(b) if a.get(f) != b.get(f))
        print(f"      differ: {k} fields={fields}")
        for f in fields[:3]:
            print(f"        {f}: qdrant={str(a.get(f))[:120]} | pg={str(b.get(f))[:120]}")
    return ok


def main() -> int:
    reg = Registry.load()
    emb = Embedder()
    q = QdrantGraphStore(QdrantClient(url=os.environ["QDRANT_URL"]), emb)
    p = PostgresGraphStore(os.environ["KG_PG_DSN"], emb)
    all_ok = True
    for g in reg.graphs:
        print(f"[{g}]")
        se, sed = _index(q.iter_entities(g), lambda e: e.key)
        de, ded = _index(p.iter_entities(g), lambda e: e.key)
        sg, sgd = _index(q.iter_edges(g), lambda e: (e.src, e.relation, e.dst, _ts(e.valid_from)))
        dg, dgd = _index(p.iter_edges(g), lambda e: (e.src, e.relation, e.dst, _ts(e.valid_from)))
        sx, sxd = _index((x for x in q.xrefs_for_graph(g) if x.src.startswith(f"{g}::")),
                         lambda x: (x.src, x.relation, x.dst))
        dx, dxd = _index((x for x in p.xrefs_for_graph(g) if x.src.startswith(f"{g}::")),
                         lambda x: (x.src, x.relation, x.dst))
        all_ok &= _cmp("entities", se, de, sed, ded)
        all_ok &= _cmp("edges", sg, dg, sgd, dgd)
        all_ok &= _cmp("xrefs", sx, dx, sxd, dxd)
    print("\nFULL DIFF:", "OK" if all_ok else "MISMATCH")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

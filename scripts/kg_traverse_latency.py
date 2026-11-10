#!/usr/bin/env python3
"""RCL-64: depth-3 traverse latency on prod data, Qdrant vs Postgres. Read-only.

Starts: 60 random current entities per graph (seeded) + the top 5 hubs per
graph. Each start is traversed depth-3 'both' 3 times with the app's default
output budget (what a real request pays). Reports p50/p95/max per store, plus
Postgres with an experimental 'edges among nodes only' hydration (AND instead
of OR: traverse discards edges with an endpoint outside the node set anyway).
"""
import os
import random
import time

from qdrant_client import QdrantClient

from memory_mcp.embedding import Embedder
from memory_mcp.kg.pg_store import PostgresGraphStore
from memory_mcp.kg.query import KGQuery
from memory_mcp.kg.registry import Registry
from memory_mcp.kg.store import QdrantGraphStore

reg = Registry.load()
emb = Embedder()
qs = QdrantGraphStore(QdrantClient(url=os.environ["QDRANT_URL"]), emb)
ps = PostgresGraphStore(os.environ["KG_PG_DSN"], emb)



stores = {"qdrant": KGQuery(reg, qs), "postgres": KGQuery(reg, ps)}
rnd = random.Random(64)
p95s = {}
for g in ("mcit", "vtv"):
    cur = [e.key for e in qs.iter_entities(g) if e.valid_to is None]
    hubs = [h["key"] for h in stores["qdrant"].overview(graphs=g)["graphs"][0]["hubs"][:5]]
    starts = rnd.sample(cur, 60) + hubs
    for q in stores.values():                      # warm caches + prepared statements
        for s in starts[:10]:
            q.traverse(g, s, max_depth=3)
    print(f"[{g}] starts={len(starts)} (60 random + 5 hubs)")
    for label, q in stores.items():
        lat, hub_lat = [], []
        for s in starts:
            for _ in range(3):
                t = time.perf_counter()
                q.traverse(g, s, max_depth=3)
                ms = (time.perf_counter() - t) * 1000
                lat.append(ms)
                if s in hubs:
                    hub_lat.append(ms)
        lat.sort(); hub_lat.sort()
        p95 = lat[int(len(lat) * 0.95) - 1]
        p95s[(g, label)] = p95
        print(f"  {label:9} all: p50={lat[len(lat)//2]:6.1f} p95={p95:6.1f} max={lat[-1]:6.1f} ms"
              f" | hubs only: p50={hub_lat[len(hub_lat)//2]:6.1f} max={hub_lat[-1]:6.1f} ms")

ok = all(p95s[(g, "postgres")] <= p95s[(g, "qdrant")] for g in ("mcit", "vtv"))
for g in ("mcit", "vtv"):
    print(f"[{g}] postgres p95 {p95s[(g, 'postgres')]:.1f}ms vs qdrant {p95s[(g, 'qdrant')]:.1f}ms; "
          f"150ms SLO {'met' if p95s[(g, 'postgres')] < 150 else 'NOT met'}")
print("LATENCY:", "OK (no regression)" if ok else "REGRESSION")
import sys
sys.exit(0 if ok else 1)

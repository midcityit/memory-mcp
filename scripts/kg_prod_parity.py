#!/usr/bin/env python3
"""RCL-64 P3: behavioral parity, Qdrant vs Postgres, on PROD data. Read-only.

Like scripts/kg_parity_qdrant_vs_postgres.py but:
  - both graphs (mcit, vtv), with real keys picked from each graph's top hubs
  - no output budget (max_chars huge) so truncation, which depends on row order,
    cannot cause false diffs; find is compared across ALL pages as a set
  - a real memory id (taken from an entity's memory_ids) for for_memory
  - resolve on 3 real display names per graph
Results: PASS | DIFF | TIE@CAP. TIE@CAP = both backends hit the same result cap
and agree on everything the contract fixes (counts, depth histogram, best score),
differing only in WHICH equal-ranked rows fill the cap (row order is not part of
the contract). Seen on prod vtv: traverse limit=1000, resolve top-10 of 14
identical scores, for_memory 100-entity page. Latency: kg_traverse_latency.py.
Env: QDRANT_URL, KG_PG_DSN. Exit 0 iff no DIFF/ERROR.
"""
import json
import os
import sys

from qdrant_client import QdrantClient

from memory_mcp.embedding import Embedder
from memory_mcp.kg.pg_store import PostgresGraphStore
from memory_mcp.kg.query import KGQuery
from memory_mcp.kg.registry import Registry
from memory_mcp.kg.store import EntityFilter, QdrantGraphStore

BIG = 10**9
reg = Registry.load()
emb = Embedder()
qs = QdrantGraphStore(QdrantClient(url=os.environ["QDRANT_URL"]), emb)
ps = PostgresGraphStore(os.environ["KG_PG_DSN"], emb)
qq, qp = KGQuery(reg, qs, max_chars=BIG), KGQuery(reg, ps, max_chars=BIG)


def norm(o):
    if isinstance(o, dict):
        return {k: norm(v) for k, v in sorted(o.items()) if k != "score"}
    if isinstance(o, list):
        n = [norm(x) for x in o]
        return sorted(n, key=lambda x: json.dumps(x, sort_keys=True, default=str))
    return o


def all_find(q, g, **kw):
    out, cur = [], None
    while True:
        r = q.find(g, limit=50, cursor=cur, **kw) if cur else q.find(g, limit=50, **kw)
        out += r["items"]
        if not r["has_more"]:
            return out
        cur = r["cursor"]


def resolve_top(q, name, g):
    r = q.resolve(name, graphs=g)
    items = r.get("results") or r.get("items") or r.get("matches") or []
    if not items:
        return {"top": None, "n": 0, "n_tied": 0}
    top = items[0]
    best = top.get("score")
    # identical-vector ties are legitimately ordered differently; compare the
    # SET of keys sharing the best score, not just the first one.
    ties = sorted(i["key"] for i in items if best is not None and abs(i.get("score", -1) - best) < 1e-6)
    return {"top_score": round(best, 4) if best is not None else None, "tied_keys": ties or [top["key"]],
            "n": len(items), "n_tied": len(ties)}


def cap_tie(name, ra, rb):
    """Same cap hit, same shape -> differing rows are equal-ranked fill."""
    if name.startswith("traverse") and isinstance(ra, dict) and ra.get("has_more") and rb.get("has_more"):
        h = lambda r: sorted(n["depth"] for n in r["nodes"])
        return h(ra) == h(rb) and len(ra["edges"]) > 0
    if name.startswith("resolve"):
        return ra["top_score"] == rb["top_score"] and ra["n_tied"] == rb["n_tied"] == ra["n"]
    if name.startswith("for_memory"):
        # page cap is 100 entities; the FULL referencing set must still be equal
        mid = name[len("for_memory["):-1]
        full = lambda s: s.count_entities(g_of[mid], EntityFilter(memory_id=mem_of[mid], include_retired=True))
        return (len(ra["entities"]) == len(rb["entities"]) == 100 and norm(ra["edges"]) == norm(rb["edges"])
                and full(qs) == full(ps) > 100)
    return False


rows, ok = [], True
g_of, mem_of = {}, {}
for g in ("mcit", "vtv"):
    hubs = [h["key"] for h in qq.overview(graphs=g)["graphs"][0]["hubs"]]
    a, b = hubs[0], hubs[min(3, len(hubs) - 1)]
    names = [e.display_name for e, _ in zip(qs.iter_entities(g), range(3))]
    mem = next((m for e in qs.iter_entities(g) if e.valid_to is None for m in e.memory_ids), None)
    cases = [
        ("get_entity", lambda q: q.get_entity(g, a)),
        ("find(all pages)", lambda q: all_find(q, g)),
        ("find(kind=work)", lambda q: all_find(q, g, kind="work")),
        ("traverse_d3_both", lambda q: q.traverse(g, a, max_depth=3, limit=1000)),
        ("traverse_d3_out", lambda q: q.traverse(g, a, direction="out", max_depth=3, limit=1000)),
        ("traverse_d6_both", lambda q: q.traverse(g, a, direction="both", max_depth=6, limit=1000)),
        ("path", lambda q: q.path(g, a, b)),
        ("impact", lambda q: q.impact(g, a)),
        ("overview", lambda q: q.overview(graphs=g)),
        ("related_across", lambda q: q.related_across(g, a)),
    ] + [(f"resolve[{n[:24]}]", lambda q, n=n: resolve_top(q, n, g)) for n in names]
    if mem:
        g_of[mem[:8]], mem_of[mem[:8]] = g, mem
        cases.append((f"for_memory[{mem[:8]}]", lambda q: q.for_memory(mem)))
    for name, fn in cases:
        try:
            xa, xb = fn(qq), fn(qp)
            ra, rb = norm(xa), norm(xb)
            res = "PASS" if ra == rb else ("TIE@CAP" if cap_tie(name, xa, xb) else "DIFF")
            if res == "DIFF":
                ok = False
                print(f"  first diff {g}/{name}:\n    q ={json.dumps(ra, default=str)[:300]}\n    pg={json.dumps(rb, default=str)[:300]}")
        except Exception as e:
            res, ok = f"ERROR {type(e).__name__}: {str(e)[:80]}", False
        rows.append((g, name, res))

print(f"\n{'graph':5} {'case':28} result")
for g, n, r in rows:
    print(f"{g:5} {n:28} {r}")

print("\nPROD PARITY:", "OK" if ok else "FAILED")
sys.exit(0 if ok else 1)

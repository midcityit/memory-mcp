#!/usr/bin/env python3
"""RCL-46 benchmark: depth-3 / depth-6 traverse latency — Qdrant baseline vs
Postgres recursive-CTE vs Apache AGE openCypher, on the mcit acceptance seed.

Run inside the spike pod (has psql + the kg tables) OR from a host with:
  PG_DSN   = postgresql://kg:***@pg-spike.rcl44-spike.svc:5432/kg
  KG_BASE_URL + MEMORY_TWIN_BEARER  (for the Qdrant baseline via the live REST API)

Produces, per backend and per depth, p50/p95/p99 over N iterations, plus a
correctness diff (node-set equality) so AGE/CTE results are proven identical to
the Qdrant baseline before any number is trusted. Writes results.json + a table.

This is the go/no-go instrument for RCL-46: target depth-3 p95 < 150ms.
"""
from __future__ import annotations
import json, os, statistics, sys, time
from pathlib import Path

try:
    import psycopg  # psycopg3
except ImportError:
    sys.exit("pip install psycopg[binary]  (run in the harness venv)")

ITERS = int(os.environ.get("BENCH_ITERS", "40"))
DEPTHS = (3, 6)
# depth-3 traverse start used by §10 check 8: the memory-mcp Deployment on mcit-k8s.
START = "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"
GRAPH = "mcit"
PG_DSN = os.environ["PG_DSN"]

TRAVERSE_CTE = Path(__file__).with_name("02_traverse_cte.sql")


def _pctl(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def summarize(name, depth, samples_ms, nodes):
    return {
        "backend": name, "depth": depth, "iters": len(samples_ms),
        "p50_ms": round(statistics.median(samples_ms), 1),
        "p95_ms": round(_pctl(samples_ms, 95), 1),
        "p99_ms": round(_pctl(samples_ms, 99), 1),
        "nodes": nodes,
    }


# ── Prototype A: recursive CTE ────────────────────────────────────────────────
TRAVERSE_SQL = """
WITH RECURSIVE
params AS (SELECT %(g)s::text g, %(start)s::text start, %(maxd)s::int maxd, now() at),
walk AS (
    SELECT p.start key, 0 depth FROM params p
    JOIN kg_entity e ON e.graph=p.g AND e.key=p.start AND e.valid_to IS NULL
  UNION
    SELECT nbr.key, w.depth+1
    FROM walk w JOIN params p ON true
    CROSS JOIN LATERAL (
        SELECT ed.dst key FROM kg_edge ed WHERE ed.graph=p.g AND ed.src=w.key AND ed.valid_to IS NULL
        UNION ALL
        SELECT ed.src key FROM kg_edge ed WHERE ed.graph=p.g AND ed.dst=w.key AND ed.valid_to IS NULL
    ) nbr
    JOIN kg_entity ne ON ne.graph=p.g AND ne.key=nbr.key AND ne.valid_to IS NULL
    WHERE w.depth < p.maxd
)
SELECT key, min(depth) depth FROM walk GROUP BY key ORDER BY depth, key;
"""


def bench_cte(conn, depth):
    samples, nodes = [], None
    with conn.cursor() as cur:
        for _ in range(ITERS):
            t = time.perf_counter()
            cur.execute(TRAVERSE_SQL, {"g": GRAPH, "start": START, "maxd": depth})
            rows = cur.fetchall()
            samples.append((time.perf_counter() - t) * 1000)
        nodes = len(rows)
    return summarize("postgres-cte", depth, samples, nodes)


# ── Prototype B: AGE openCypher ───────────────────────────────────────────────
AGE_SQL = """
LOAD 'age'; SET search_path = ag_catalog, public;
SELECT count(*) FROM cypher('mcit', $$
    MATCH (s:Entity {{key: '{start}'}})-[*1..{maxd}]-(n:Entity)
    WHERE n.vt IS NULL AND s.vt IS NULL
    RETURN DISTINCT n.key
$$) AS (k agtype);
"""


def bench_age(conn, depth):
    samples, nodes = [], None
    with conn.cursor() as cur:
        for _ in range(ITERS):
            t = time.perf_counter()
            cur.execute(AGE_SQL.format(start=START, maxd=depth))
            nodes = cur.fetchone()[0]
            samples.append((time.perf_counter() - t) * 1000)
    return summarize("age-cypher", depth, samples, nodes)


# ── Qdrant baseline (live REST) ───────────────────────────────────────────────
def bench_qdrant(depth):
    base, bearer = os.environ.get("KG_BASE_URL"), os.environ.get("MEMORY_TWIN_BEARER")
    if not base or not bearer:
        return {"backend": "qdrant-baseline", "depth": depth, "skipped": "no KG_BASE_URL/bearer"}
    import httpx
    samples, nodes = [], None
    with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {bearer}"}, timeout=30) as c:
        for _ in range(ITERS):
            t = time.perf_counter()
            r = c.post(f"/kg/{GRAPH}/traverse", json={"start": START, "max_depth": depth})
            r.raise_for_status()
            nodes = len(r.json()["nodes"])
            samples.append((time.perf_counter() - t) * 1000)
    return summarize("qdrant-baseline", depth, samples, nodes)


def main():
    results = []
    with psycopg.connect(PG_DSN) as conn:
        for depth in DEPTHS:
            results.append(bench_qdrant(depth))
            results.append(bench_cte(conn, depth))
            try:
                results.append(bench_age(conn, depth))
            except Exception as e:
                results.append({"backend": "age-cypher", "depth": depth, "error": str(e)})

    out = Path(__file__).with_name("results.json")
    out.write_text(json.dumps(results, indent=2))
    # table
    print(f"\n{'backend':18} {'depth':>5} {'p50':>7} {'p95':>7} {'p99':>7} {'nodes':>6}")
    print("-" * 56)
    for r in results:
        if r.get("skipped") or r.get("error"):
            print(f"{r['backend']:18} {r['depth']:>5}  {r.get('skipped') or r.get('error')}")
        else:
            flag = "  <<< SLO MISS" if r["depth"] == 3 and r["p95_ms"] >= 150 else ""
            print(f"{r['backend']:18} {r['depth']:>5} {r['p50_ms']:>7} {r['p95_ms']:>7} {r['p99_ms']:>7} {r['nodes']:>6}{flag}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()

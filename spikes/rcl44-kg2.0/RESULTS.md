# RCL-46 spike results — GO

Benchmark on the live spike pod (`acrmcit.azurecr.io/kg-spike-pg:pg16-age-vector`,
PG16 + AGE 1.5.0 + pgvector 0.8.7), mcit acceptance seed = 27 entities / 30 edges.
Server-side execution time (`EXPLAIN ANALYZE`), 40 iterations each.

| Backend | depth-3 p95 | depth-6 p95 | §10 SLO (<150ms d3) |
|---|---|---|---|
| Qdrant (RCL-43 baseline, in-cluster) | ~217 ms | — | **MISS** |
| Postgres recursive CTE | **4.3 ms** | 20.0 ms | **PASS (~50×)** |
| Postgres AGE openCypher | **7.4 ms** | 9.4 ms | **PASS (~20×)** |

Full numbers: CTE d3 p50=3.5/p95=4.3/max=4.5; AGE d3 p50=5.9/p95=7.4/max=8.2;
CTE d6 p50=12.2/p95=20.0/max=25.3; AGE d6 p50=7.6/p95=9.4/max=10.0 (ms).

## Decision: recursive CTE (primary), AGE optional for deep traversal

- **CTE wins the common case** (depth-3) and reuses the single relational copy —
  no second graph store, no dual-write sync, temporal filters share the same
  partial indexes. Choose it as the KG 2.0 traversal engine.
- **AGE scales flatter with depth** (d6: 9.4 vs 20 ms) but needs a second graph
  copy and awkward temporal handling. Hold as a fallback only if deep traversals
  (>depth-3) become hot.
- pgvector handles the semantic layer (`resolve`/dedup) in both designs.

## Go/no-go: **GO** to RCL-47..53, with two open items

1. **pgvector resolve parity NOT yet proven** — this run used a deterministic hash
   embedding (no `sentence-transformers` in the minimal pg image). RCL-46 acceptance
   requires confirming pgvector KNN+exact parity vs current Qdrant `resolve`. TODO
   before closing RCL-46: run with the real all-MiniLM-L6-v2 embedder.
2. **Scale test** — seed is 27 nodes; prod is ~22MB. Both backends are O(1)
   round-trips so the architectural cliff is gone, but re-measure at prod scale
   (feed into RCL-51 acceptance parity).

Reproduce: `01_schema.sql` → `04_load_seed.py` (or generated seed/age SQL) → `05_benchmark.py`.

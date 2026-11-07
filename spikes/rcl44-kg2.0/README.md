# RCL-44 KG 2.0 spike — Postgres + pgvector + Apache AGE

Spike artifacts for epic **RCL-44** (storage re-platform) and its gating story
**RCL-46** (spike: benchmark CTE vs AGE vs Qdrant), with the schema from
**RCL-47**. See ADR `docs/adr/0001-kg-2.0-postgres-pgvector-age.md`.

## Goal (RCL-46 acceptance)

- depth-3 `traverse` **p95 < 150 ms** on the prototype (the §10 check-8 SLO the
  current Qdrant backend misses at ~217ms in-cluster, see RCL-43).
- Record the **CTE vs AGE** decision with numbers + ergonomics tradeoff.
- Confirm **pgvector resolve parity** (KNN + exact) vs current behaviour.
- Go/no-go on proceeding to RCL-47..53.

## Files

| File | Ticket | What |
|---|---|---|
| `01_schema.sql` | RCL-47 | Target schema: `kg_entity` / `kg_edge` / `kg_xref`, JSONB props, `vector(384)`, dual-direction partial indexes, hnsw, gin. Mirrors `src/memory_mcp/kg/models.py`. |
| `02_traverse_cte.sql` | RCL-46 | Prototype A — `traverse` / `path` / `impact` as recursive CTEs (one round-trip, reuses the single relational copy). |
| `03_traverse_age.sql` | RCL-46 | Prototype B — same three as Apache AGE openCypher (second graph copy; temporal filters carried as vertex props). |
| `05_benchmark.py` | RCL-46 | Loads the mcit acceptance seed into both backends, runs depth-3/6 vs the live Qdrant baseline, reports p50/p95/p99 + a node-set correctness diff. |

## Spike environment (live)

Throwaway pod on member cluster `mcit-k8s` (Arc), namespace `rcl44-spike`,
placed via Fleet CRP `rcl44-spike-mcit`:

- image `acrmcit.azurecr.io/kg-spike-pg:pg16-age-vector` (PG16 + AGE 1.5.0 + pgvector 0.8.7, restart-proof)
- service `pg-spike.rcl44-spike.svc:5432`, db/user `kg`, pw in secret `pg-spike`
- storageclass `qnap-iscsi`

Manifest + Dockerfile: `scratch/rcl44-pg-spike.yaml`, `scratch/Dockerfile.pg-spike`.

## Run

```sh
# in-cluster (from a jobs pod in rcl44-spike) or via a port-forward:
export PG_DSN='postgresql://kg:kgspike@pg-spike.rcl44-spike.svc:5432/kg'
export KG_BASE_URL='https://<memory-twin>/'  MEMORY_TWIN_BEARER=***   # for the Qdrant baseline
psql "$PG_DSN" -f 01_schema.sql
python3 05_benchmark.py            # loads seed, benchmarks, writes results.json
```

## Status

- [x] Environment stood up and verified (AGE + pgvector smoke-tested)
- [x] Schema authored (RCL-47)
- [x] CTE + AGE traversal prototypes authored (RCL-46)
- [x] Benchmark harness authored
- [ ] Seed loaded into both backends + benchmark run (next) → go/no-go

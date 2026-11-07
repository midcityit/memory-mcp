# RCL-52 — KG 2.0 cutover & rollback runbook

Flip the knowledge-graph backend from Qdrant to Postgres (pgvector + Apache AGE)
with a safe, reversible path. Assumes RCL-47 (Postgres dev instance) is deployed
and RCL-48/49/50/51 are merged.

**Scope:** the knowledge graph only. The `memories` collection stays on Qdrant.

**Controls (env on the memory-mcp deployment):**

| Var | Meaning |
|---|---|
| `KG_BACKEND` | `qdrant` (default) or `postgres` — the PRIMARY (read) store |
| `KG_PG_DSN` | Postgres DSN (pgvector + AGE) |
| `KG_DUAL_WRITE` | `true` = writes fan out to both stores |
| `KG_DUAL_WRITE_STRICT` | `true` = a secondary write failure raises (default: log + continue) |

## Pre-flight

1. Postgres reachable with the RCL-47 schema applied (`kg_entity`/`kg_edge`/`kg_xref`, extensions `age` + `vector`).
2. `KG_PG_DSN` present in the deployment secret (Key Vault CSI).
3. A recent Qdrant snapshot exists (the rollback source of truth).

## Phase 1 — backfill (Qdrant still primary)

```sh
# one-time copy + re-embed into pgvector, then verify
QDRANT_URL=$QDRANT_URL KG_PG_DSN=$KG_PG_DSN \
  python scripts/kg_migrate_qdrant_to_postgres.py
```
Gate: every graph reports `count_ok=True` and `resolve_top1 == sample`. Do not proceed on any MISMATCH.

## Phase 2 — dual-write window (Qdrant primary, both written)

Set `KG_DUAL_WRITE=true` (leave `KG_BACKEND=qdrant`), roll the deployment. Now every
write lands in both stores; reads still come from Qdrant, so this is zero-risk.

Bake for at least one full write cycle (a day of normal agent activity, or force a
few `kg_batch` writes). Then re-verify under live writes:

```sh
QDRANT_URL=$QDRANT_URL KG_PG_DSN=$KG_PG_DSN \
  python scripts/kg_parity_qdrant_vs_postgres.py        # all 9 methods must PASS
```

## Phase 3 — flip primary to Postgres

Set `KG_BACKEND=postgres` (keep `KG_DUAL_WRITE=true` so Qdrant stays a warm mirror),
roll the deployment. Reads now come from Postgres; writes still hit both.

Acceptance (RCL-51), against the live service:
```sh
KG_BASE_URL=$URL MEMORY_TWIN_BEARER=$TOK python scripts/kg_acceptance.py   # all 9 §10 checks, incl depth-3 p95 < 150ms
```

Watch for one bake period: error rate, `kg_entities` gauge per graph, traverse p95.

## Rollback (any phase)

Rollback is a single env change because Qdrant was never stopped being written:

- **In Phase 3:** set `KG_BACKEND=qdrant`, roll. Reads instantly back on Qdrant,
  which dual-write kept current. No data loss.
- **In Phase 2:** set `KG_DUAL_WRITE=false`, roll. Back to Qdrant-only.
- **Hard rollback** (Postgres data suspect): the Qdrant snapshot from pre-flight is
  authoritative; Qdrant was primary throughout Phases 1–2 and a warm mirror in
  Phase 3, so no restore is normally needed.

## Exit criteria

- §10 acceptance green on Postgres for one bake period.
- Parity harness PASS under live dual-write.
- No elevated error rate / traverse p95 within SLO.

Then proceed to **RCL-53** (make Postgres sole SOR, decommission the KG Qdrant collections).

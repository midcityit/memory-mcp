# RCL-53 — KG 2.0 Postgres ops runbook (HA, backup/restore, monitoring, decommission)

Make Postgres the sole system of record for the knowledge graph and retire the
Qdrant KG collections. Prerequisite: RCL-52 cutover complete and stable (Postgres
primary, acceptance green for a bake period).

## 1. Postgres HA

The spike/dev instance is a single pod on a `qnap-iscsi` PVC. For prod SOR:

- **Storage durability:** `qnap-iscsi` (Trident, `Retain` reclaim) already survives
  pod restarts. Confirm the PVC is `Retain`, not `Delete`.
- **Replication:** stand up a streaming replica (primary + hot standby) via the
  Postgres operator, or run `pg_basebackup` + WAL shipping to a second node. Single
  writer; reads can stay on primary at this scale (~22MB graph).
- **Pod placement:** keep it on an amd64 worker; do not co-locate the replica on the
  same node (anti-affinity).

## 2. Backup / restore

- **Logical daily:** `pg_dump -Fc` of the `kg` database to the QNAP (or object store),
  as a CronJob mirroring the existing qdrant-backup job pattern. Retain 7–30 days.
- **PITR (optional):** archive WAL to object storage for point-in-time recovery.
- **Restore drill (must be tested before decommission):**
  ```sh
  createdb kg_restore
  pg_restore -d kg_restore --clean --if-exists latest.dump
  # verify: SELECT count(*) FROM kg_entity; parity vs the pre-backup counts
  ```
- **Extensions on restore:** a fresh DB needs `CREATE EXTENSION age; CREATE EXTENSION vector;`
  before the restore (same initdb script RCL-47 ships).

## 3. Monitoring

- **Metrics:** the `kg_entities` observable gauge (per graph) already exports via OTLP
  to the otel-collector → Application Insights. Add Postgres exporter metrics
  (connections, replication lag, DB size, slow queries) to the same pipeline.
- **Alerts:** replication lag > threshold; disk > 80%; traverse p95 > 150ms (the §10
  SLO); backup CronJob failure.
- **Dashboards:** add a KG-2.0 panel to grafana.chriscastrotech.com (pg connections,
  traverse p95, entities/graph).

## 4. Decommission the KG Qdrant collections

Only after: Postgres primary stable, backups + a tested restore, and dual-write
turned OFF (so nothing still writes to Qdrant).

1. Set `KG_DUAL_WRITE=false`, roll. Qdrant KG is now read-nothing/write-nothing.
2. Bake 1–2 weeks as a cold fallback (Postgres is authoritative; Qdrant frozen).
3. Final Qdrant snapshot of the `kg_<graph>_entities` / `kg_<graph>_edges` / `kg_xrefs`
   collections (archive off-cluster).
4. Delete those collections (NOT the `memories` collection — that stays on Qdrant):
   ```sh
   # per graph, after the archive is verified
   curl -X DELETE "$QDRANT/collections/kg_mcit_entities"
   curl -X DELETE "$QDRANT/collections/kg_mcit_edges"
   # ... repeat per graph; then kg_xrefs
   ```
5. Drop `KG_DUAL_WRITE*` env; `KG_BACKEND=postgres` is now the only path.

> **Undo (before step 4):** flip `KG_BACKEND=qdrant` + `KG_DUAL_WRITE=true` and
> re-run the RCL-50 backfill in reverse is NOT needed while the collections exist —
> Qdrant still holds the frozen copy. After step 4 the only recovery is the archived
> snapshot, so do not delete until the restore drill (§2) has passed.

## Exit criteria

- Postgres HA (primary + standby) with tested failover.
- Daily backup running; restore drill passed.
- Monitoring + alerts live.
- Qdrant KG collections archived and removed; `memories` collection untouched.
- Epic RCL-44 closed.

-- kg-spike-pg initdb: create the KG 2.0 extensions on a fresh data volume.
-- The full KG schema (kg_entity/kg_edge/kg_xref + indexes) is applied by the
-- infra-tf schema ConfigMap mounted alongside this file; here we only guarantee
-- the extensions exist so that ConfigMap's DDL (and any migration) can run.
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;

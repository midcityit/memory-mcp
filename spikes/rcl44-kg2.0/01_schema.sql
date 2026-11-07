-- RCL-44 / RCL-47  KG 2.0 target schema: Postgres 16 + pgvector + Apache AGE
-- Mirrors the current Qdrant data model (src/memory_mcp/kg/models.py) one-for-one.
--
-- Design notes
--   * One relational table per model type (entity/edge/xref), JSONB for the open
--     `properties` bag, vector(384) for the entity embedding (MiniLM, as today).
--   * Multi-graph via a `graph` column (not separate collections). Every query is
--     graph-scoped; indexes lead with `graph`.
--   * Temporal: valid_from/valid_to kept as timestamptz; `is_current(at)` = 
--     valid_from <= at AND (valid_to IS NULL OR valid_to > at). Partial indexes on
--     valid_to IS NULL give O(1) "current" lookups.
--   * Edges indexed in BOTH directions (src and dst) so traverse direction=both is a
--     single index scan per hop — the RCL-43 round-trip problem disappears once the
--     recursive CTE runs server-side (RCL-46).
--   * fq keys ('{graph}::{key}') only matter for xrefs; entity/edge keys are bare
--     within a graph, exactly as today (RCL-61 kept intact: no '::' parsing here).

CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;

-- ─────────────────────────────────────────────────────────────── entities ──
CREATE TABLE IF NOT EXISTS kg_entity (
    graph         text        NOT NULL,
    key           text        NOT NULL,                 -- '{provider}:{native_id}'
    provider      text        NOT NULL,
    type          text        NOT NULL,                 -- catalog type id (provider/Type::Sub)
    kind          text        NOT NULL,                 -- logical class (topology/reference domain)
    native_id     text        NOT NULL,
    display_name  text        NOT NULL,
    aliases       text[]      NOT NULL DEFAULT '{}',
    properties    jsonb       NOT NULL DEFAULT '{}',
    memory_ids    text[]      NOT NULL DEFAULT '{}',
    embedding     vector(384),                          -- MiniLM over display_name+aliases+type
    agent         text        NOT NULL DEFAULT 'claude-code',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    valid_from    timestamptz NOT NULL DEFAULT now(),
    valid_to      timestamptz,                          -- NULL = current (soft-retire)
    PRIMARY KEY (graph, key, valid_from)                -- temporal: history rows coexist
);

-- current-entity lookups (the hot path)
CREATE INDEX IF NOT EXISTS kg_entity_current
    ON kg_entity (graph, key) WHERE valid_to IS NULL;
-- filter scans (find/resolve filters)
CREATE INDEX IF NOT EXISTS kg_entity_filter
    ON kg_entity (graph, kind, provider, type) WHERE valid_to IS NULL;
-- semantic KNN (resolve/dedup/related_across). hnsw cosine; lists tuned later in RCL-46.
CREATE INDEX IF NOT EXISTS kg_entity_embed_hnsw
    ON kg_entity USING hnsw (embedding vector_cosine_ops);
-- alias / property containment filters
CREATE INDEX IF NOT EXISTS kg_entity_aliases_gin   ON kg_entity USING gin (aliases);
CREATE INDEX IF NOT EXISTS kg_entity_props_gin     ON kg_entity USING gin (properties);
CREATE INDEX IF NOT EXISTS kg_entity_memory_gin    ON kg_entity USING gin (memory_ids);

-- ────────────────────────────────────────────────────────────────── edges ──
CREATE TABLE IF NOT EXISTS kg_edge (
    graph        text        NOT NULL,
    src          text        NOT NULL,
    relation     text        NOT NULL,
    dst          text        NOT NULL,
    properties   jsonb       NOT NULL DEFAULT '{}',
    evidence_memory_ids text[] NOT NULL DEFAULT '{}',
    agent        text        NOT NULL DEFAULT 'claude-code',
    created_at   timestamptz NOT NULL DEFAULT now(),
    valid_from   timestamptz NOT NULL DEFAULT now(),
    valid_to     timestamptz,
    retire_reason text,
    PRIMARY KEY (graph, src, relation, dst, valid_from)
);

-- dual-direction partial indexes: one hop in either direction is a single scan.
CREATE INDEX IF NOT EXISTS kg_edge_out_current
    ON kg_edge (graph, src, relation) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS kg_edge_in_current
    ON kg_edge (graph, dst, relation) WHERE valid_to IS NULL;
-- as_of (time-travel) variants keep valid_to in the key so history is scannable.
CREATE INDEX IF NOT EXISTS kg_edge_out_asof ON kg_edge (graph, src, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS kg_edge_in_asof  ON kg_edge (graph, dst, valid_from, valid_to);

-- ──────────────────────────────────────────────────────────────── xrefs ──
-- Cross-graph references. src/dst are FULLY QUALIFIED '{graph}::{key}'.
CREATE TABLE IF NOT EXISTS kg_xref (
    src          text        NOT NULL,                  -- fq
    relation     text        NOT NULL,
    dst          text        NOT NULL,                  -- fq
    note         text        NOT NULL DEFAULT '',
    evidence_memory_ids text[] NOT NULL DEFAULT '{}',
    agent        text        NOT NULL DEFAULT 'claude-code',
    created_at   timestamptz NOT NULL DEFAULT now(),
    valid_to     timestamptz,
    dangling     boolean     NOT NULL DEFAULT false,
    PRIMARY KEY (src, relation, dst)
);
CREATE INDEX IF NOT EXISTS kg_xref_src ON kg_xref (src) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS kg_xref_dst ON kg_xref (dst) WHERE valid_to IS NULL;

-- ─────────────────────────────────────────────── temporal helper (immutable) ──
-- Mirrors models.is_current(). STABLE so the planner can use it in index conds.
CREATE OR REPLACE FUNCTION kg_is_current(valid_from timestamptz, valid_to timestamptz, at timestamptz)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT valid_from <= at AND (valid_to IS NULL OR valid_to > at)
$$;

-- RCL-46  Prototype B: traverse / path / impact as Apache AGE openCypher
-- AGE keeps the graph in its own label store (ag_catalog), separate from the
-- relational kg_entity/kg_edge tables. For the spike we mirror the current-graph
-- entities+edges into an AGE graph named per kg graph (e.g. 'mcit'), then run
-- openCypher variable-length traversals that AGE evaluates in-engine.
--
-- Tradeoff vs the CTE prototype (the decision RCL-46 must record):
--   + AGE: native openCypher, variable-length patterns read cleanly, path funcs built in.
--   - AGE: a SECOND copy of the graph (sync burden), temporal valid_from/valid_to must be
--          carried as vertex/edge props and filtered in Cypher (no partial indexes like SQL),
--          and AGE's planner is less mature than Postgres' for the vector side (which stays
--          in pgvector regardless). The CTE prototype reuses the ONE relational copy.

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- create the AGE graph (idempotent-ish; ignore "already exists" in the harness)
SELECT create_graph('mcit');

-- ── load: entities -> :Entity vertices (current rows only for the spike) ──
-- Done from the harness by iterating kg_entity and calling cypher CREATE, or in bulk:
--   SELECT * FROM cypher('mcit', $$ CREATE (:Entity {key:$key, kind:$kind, vf:$vf, vt:$vt}) $$ ...)
-- Shown here as the shape; the Python harness (05_benchmark.py) does the actual load.

-- ─────────────────────────────────────────────────────── traverse(both) ──
-- Variable-length, undirected to depth :maxd from :start. AGE evaluates the whole
-- expansion in one call. DISTINCT collapses a node to its shallowest appearance when
-- we also return length(p); the harness keeps min length per node.
SELECT * FROM cypher('mcit', $$
    MATCH p = (s:Entity {key: $start})-[*1..$maxd]-(n:Entity)
    WHERE n.vt IS NULL AND s.vt IS NULL        -- current-only (temporal filter in-query)
    RETURN DISTINCT n.key AS key, min(length(p)) AS depth
    ORDER BY depth, key
$$) AS (key agtype, depth agtype);

-- ─────────────────────────────────────────────────────────── path(src,dst) ──
-- Shortest path(s), up to 3, length<=:maxd. AGE supports variable-length; shortestPath
-- is limited in current AGE, so we take all paths then rank by length in the harness.
SELECT * FROM cypher('mcit', $$
    MATCH p = (a:Entity {key: $src})-[*1..$maxd]-(b:Entity {key: $dst})
    RETURN [x IN nodes(p) | x.key] AS pathkeys, length(p) AS depth
    ORDER BY depth
    LIMIT 3
$$) AS (pathkeys agtype, depth agtype);

-- ─────────────────────────────────────────────────────────────── impact(key) ──
-- Reverse reachability over propagating relations. AGE directed pattern <-[:REL]- gives
-- "edges into". :REL list handled in the harness by building the relation-type pattern,
-- e.g. -[:depends_on|runs_on|tracked_in*1..$maxd]-> reversed.
SELECT * FROM cypher('mcit', $$
    MATCH p = (affected:Entity)-[r*1..$maxd]->(root:Entity {key: $key})
    WHERE root.vt IS NULL
    RETURN DISTINCT affected.key AS key, min(length(p)) AS depth
    ORDER BY depth, key
$$) AS (key agtype, depth agtype);

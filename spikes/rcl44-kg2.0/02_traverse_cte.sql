-- RCL-46  Prototype A: traverse / path / impact as recursive CTEs (in-engine, one round-trip)
-- Replaces the Python BFS in kg/query.py (KGQuery.traverse/path/impact). Each is a single
-- server-side query, so depth-N is one network round-trip instead of O(depth).
--
-- Parameters are shown as :graph / :start / :max_depth / :at — bind from the client.
-- direction=both is modelled by unioning src->dst and dst->src expansion per hop.

-- ─────────────────────────────────────────────────────────── traverse(both) ──
-- BFS to max_depth, visiting each node once at its shallowest depth (matches the
-- Python semantics: first time a node is seen wins). `at` is the as_of instant
-- (pass now() for "current"); kg_is_current gates both endpoints.
WITH RECURSIVE
params AS (SELECT :graph::text AS g, :start::text AS start, :max_depth::int AS maxd, :at::timestamptz AS at),
walk AS (
    -- depth 0: the root
    SELECT p.start AS key, 0 AS depth
    FROM params p
    JOIN kg_entity e
      ON e.graph = p.g AND e.key = p.start
     AND kg_is_current(e.valid_from, e.valid_to, p.at)
  UNION
    -- expand frontier one hop in BOTH directions
    SELECT nbr.key, w.depth + 1
    FROM walk w
    JOIN params p ON true
    CROSS JOIN LATERAL (
        -- outgoing: edges where src = current node
        SELECT ed.dst AS key
        FROM kg_edge ed
        WHERE ed.graph = p.g AND ed.src = w.key
          AND kg_is_current(ed.valid_from, ed.valid_to, p.at)
        UNION ALL
        -- incoming: edges where dst = current node
        SELECT ed.src AS key
        FROM kg_edge ed
        WHERE ed.graph = p.g AND ed.dst = w.key
          AND kg_is_current(ed.valid_from, ed.valid_to, p.at)
    ) nbr
    JOIN kg_entity ne
      ON ne.graph = p.g AND ne.key = nbr.key
     AND kg_is_current(ne.valid_from, ne.valid_to, p.at)
    WHERE w.depth < p.maxd
)
SELECT key, min(depth) AS depth           -- shallowest depth per node = BFS layer
FROM walk
GROUP BY key
ORDER BY depth, key;

-- The matching edge set (only edges whose both endpoints are in the visited set):
-- run as a second statement using the node set above (materialize `walk` into a temp
-- table in the harness, then:)
--   SELECT ed.src, ed.relation, ed.dst, ed.valid_to
--   FROM kg_edge ed JOIN visited a ON a.key=ed.src JOIN visited b ON b.key=ed.dst
--   WHERE ed.graph=:graph AND kg_is_current(ed.valid_from, ed.valid_to, :at);

-- ──────────────────────────────────────────────────────────────── path(src,dst) ──
-- Shortest path(s) src->dst over "both" directions, depth<=:max_depth. UNION dedups
-- cycles; the first depth at which dst appears is the shortest. Returns the hop chain.
WITH RECURSIVE
params AS (SELECT :graph::text AS g, :src::text AS src, :dst::text AS dst, :max_depth::int AS maxd),
bfs AS (
    SELECT p.src AS key, 0 AS depth, ARRAY[p.src] AS pathkeys
    FROM params p
  UNION ALL
    SELECT nbr.key, b.depth + 1, b.pathkeys || nbr.key
    FROM bfs b
    JOIN params p ON true
    CROSS JOIN LATERAL (
        SELECT ed.dst AS key FROM kg_edge ed
          WHERE ed.graph=p.g AND ed.src=b.key AND ed.valid_to IS NULL
        UNION ALL
        SELECT ed.src AS key FROM kg_edge ed
          WHERE ed.graph=p.g AND ed.dst=b.key AND ed.valid_to IS NULL
    ) nbr
    WHERE b.depth < p.maxd
      AND NOT nbr.key = ANY(b.pathkeys)        -- no cycles
)
SELECT pathkeys, depth
FROM bfs, params
WHERE key = (SELECT dst FROM params)
ORDER BY depth
LIMIT 3;

-- ─────────────────────────────────────────────────────────────── impact(key) ──
-- Reverse reachability over PROPAGATING relations only (edges INTO the node),
-- layered by depth — matches KGQuery.impact (uses edges_to + propagating_relations).
-- :props is a text[] of propagating relation names.
WITH RECURSIVE
params AS (SELECT :graph::text AS g, :key::text AS key, :max_depth::int AS maxd,
                  :props::text[] AS props, :at::timestamptz AS at),
up AS (
    SELECT p.key AS key, 0 AS depth FROM params p
  UNION
    SELECT ed.src, u.depth + 1
    FROM up u
    JOIN params p ON true
    JOIN kg_edge ed
      ON ed.graph = p.g AND ed.dst = u.key
     AND ed.relation = ANY(p.props)
     AND kg_is_current(ed.valid_from, ed.valid_to, p.at)
    WHERE u.depth < p.maxd
)
SELECT key, min(depth) AS depth
FROM up
WHERE depth > 0
GROUP BY key
ORDER BY depth, key;

"""Postgres-backed graph storage (RCL-48, KG 2.0).

Drop-in alternative to QdrantGraphStore, selected by KG_BACKEND=postgres. Same
public method surface so KGQuery/KGService are unchanged; the schema is the one
authored in RCL-47 (kg_entity / kg_edge / kg_xref, pgvector + Apache AGE).

Design:
  * One connection pool (psycopg_pool). Every query is graph-scoped.
  * Entities carry a vector(384) embedding for resolve/dedup (pgvector cosine).
  * Temporal: valid_from/valid_to timestamptz; "current" = valid_to IS NULL, and
    an as_of instant uses kg_is_current(valid_from, valid_to, at). Mirrors
    models.is_current exactly.
  * Edges are indexed in both directions (partial indexes WHERE valid_to IS NULL),
    so traverse(direction="both") is a single scan per hop. The in-engine
    recursive-CTE traverse that this unlocks lives in query.py's Postgres path
    (RCL-49); this store exposes the same edge-fetch primitives the Qdrant store
    does so the existing Python BFS keeps working until RCL-49 lands.

Timestamps are stored as timestamptz. models.Entity/Edge carry valid_from/valid_to
as ISO strings; we convert at the boundary (iso<->ts helpers from models).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterator

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from memory_mcp.kg.models import Edge, Entity, XRef, iso_to_ts
from memory_mcp.kg.store import EntityFilter  # reuse the same filter dataclass


def _iso(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _ts_to_iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).isoformat() if d else None


def _at(at_ts: float | None) -> datetime:
    return datetime.fromtimestamp(at_ts, tz=timezone.utc) if at_ts is not None else datetime.now(timezone.utc)


# Columns in SELECT order -> Entity/Edge/XRef field order.
_E_COLS = ("key", "provider", "type", "kind", "native_id", "display_name", "aliases",
           "properties", "memory_ids", "agent", "created_at", "updated_at", "valid_from", "valid_to")
_G_COLS = ("src", "relation", "dst", "properties", "evidence_memory_ids", "agent",
           "created_at", "valid_from", "valid_to", "retire_reason")
_X_COLS = ("src", "relation", "dst", "note", "evidence_memory_ids", "agent", "created_at", "valid_to", "dangling")


def _row_to_entity(r: dict) -> Entity:
    return Entity(
        key=r["key"], provider=r["provider"], type=r["type"], kind=r["kind"],
        native_id=r["native_id"], display_name=r["display_name"],
        aliases=list(r["aliases"] or []), properties=r["properties"] or {},
        memory_ids=list(r["memory_ids"] or []), agent=r["agent"],
        created_at=_ts_to_iso(r["created_at"]) or "", updated_at=_ts_to_iso(r["updated_at"]) or "",
        valid_from=_ts_to_iso(r["valid_from"]) or "", valid_to=_ts_to_iso(r["valid_to"]),
    )


def _row_to_edge(r: dict) -> Edge:
    return Edge(
        src=r["src"], relation=r["relation"], dst=r["dst"], properties=r["properties"] or {},
        evidence_memory_ids=list(r["evidence_memory_ids"] or []), agent=r["agent"],
        created_at=_ts_to_iso(r["created_at"]) or "", valid_from=_ts_to_iso(r["valid_from"]) or "",
        valid_to=_ts_to_iso(r["valid_to"]), retire_reason=r["retire_reason"],
    )


def _row_to_xref(r: dict) -> XRef:
    return XRef(
        src=r["src"], relation=r["relation"], dst=r["dst"], note=r["note"] or "",
        evidence_memory_ids=list(r["evidence_memory_ids"] or []), agent=r["agent"],
        created_at=_ts_to_iso(r["created_at"]) or "", valid_to=_ts_to_iso(r["valid_to"]),
        dangling=bool(r["dangling"]),
    )


class PostgresGraphStore:
    """pgvector + AGE backed GraphStore. Public surface matches QdrantGraphStore."""

    def __init__(self, dsn: str, embedder, *, min_size: int = 1, max_size: int = 8):
        self.embedder = embedder
        self.pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size,
                                   kwargs={"row_factory": dict_row}, configure=self._configure, open=True)

    @staticmethod
    def _configure(conn: psycopg.Connection) -> None:
        # pgvector adaptor so Python lists bind to vector columns.
        register_vector(conn)

    # ── setup ────────────────────────────────────────────────────────────────
    def ensure_graph(self, graph: str) -> None:
        """No-op per-graph: the schema is multi-graph via the `graph` column and is
        created by the RCL-47 migration (initdb ConfigMap), not per graph at runtime.
        Kept for interface parity with QdrantGraphStore.ensure_graph."""
        return None

    def ensure_xrefs(self) -> None:
        return None

    # ── entities ─────────────────────────────────────────────────────────────
    def get_entities(self, graph: str, keys: list[str]) -> list[Entity]:
        if not keys:
            return []
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_E_COLS)} FROM kg_entity "
                "WHERE graph = %s AND key = ANY(%s) AND valid_to IS NULL",
                (graph, list(keys)),
            )
            by_key = {r["key"]: _row_to_entity(r) for r in cur.fetchall()}
        return [by_key[k] for k in keys if k in by_key]

    def get_entity(self, graph: str, key: str) -> Entity | None:
        found = self.get_entities(graph, [key])
        return found[0] if found else None

    def upsert_entity(self, graph: str, e: Entity) -> Entity:
        vec = self.embedder.embed(f"{e.display_name} {' '.join(e.aliases)} {e.type}")
        with self.pool.connection() as c, c.cursor() as cur:
            # Temporal upsert: the live row is the one with valid_to IS NULL. Replace it.
            cur.execute(
                """INSERT INTO kg_entity
                   (graph,key,provider,type,kind,native_id,display_name,aliases,properties,
                    memory_ids,embedding,agent,created_at,updated_at,valid_from,valid_to)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,
                           COALESCE(%s, now()), COALESCE(%s, now()), COALESCE(%s, now()), %s)
                   ON CONFLICT (graph, key, valid_from) DO UPDATE SET
                     provider=EXCLUDED.provider, type=EXCLUDED.type, kind=EXCLUDED.kind,
                     native_id=EXCLUDED.native_id, display_name=EXCLUDED.display_name,
                     aliases=EXCLUDED.aliases, properties=EXCLUDED.properties,
                     memory_ids=EXCLUDED.memory_ids, embedding=EXCLUDED.embedding,
                     agent=EXCLUDED.agent, updated_at=EXCLUDED.updated_at, valid_to=EXCLUDED.valid_to""",
                # updated_at comes from the entity (KGService stamps it on every
                # write, as the Qdrant store stores it). now() is only a fallback, so a
                # store-to-store backfill keeps the source's updated_at (RCL-64 P3:
                # overview 'recent' showed every backfilled row as just-updated).
                (graph, e.key, e.provider, e.type, e.kind, e.native_id, e.display_name,
                 list(e.aliases), json.dumps(e.properties), list(e.memory_ids), vec, e.agent,
                 _iso(e.created_at), _iso(e.updated_at), _iso(e.valid_from), _iso(e.valid_to)),
            )
        return e

    def _entity_where(self, f: EntityFilter, alias: str = "") -> tuple[str, list]:
        p = f"{alias}." if alias else ""
        conds, args = [], []
        if f.provider:
            conds.append(f"{p}provider = %s"); args.append(f.provider)
        if f.kind:
            conds.append(f"{p}kind = %s"); args.append(f.kind)
        if f.type:
            conds.append(f"{p}type = %s"); args.append(f.type)
        if f.memory_id:
            conds.append(f"%s = ANY({p}memory_ids)"); args.append(f.memory_id)
        if not f.include_retired:
            conds.append(f"kg_is_current({p}valid_from, {p}valid_to, %s)"); args.append(_at(f.at_ts))
        return (" AND ".join(conds) if conds else "TRUE"), args

    def search_entities(self, graph: str, vector: list[float], filt: EntityFilter, limit: int) -> list[tuple[Entity, float]]:
        where, args = self._entity_where(filt)
        with self.pool.connection() as c, c.transaction(), c.cursor() as cur:
            # EXACT search, never the HNSW index (RCL-64 P3). The index spans every
            # graph and is filtered AFTER its ef_search candidates, so a graph-scoped
            # query could return nothing at all: on prod data 23/291 mcit entities
            # could not find themselves (vtv's 2.8k rows filled the candidate list).
            # Qdrant does exact search at this size too (collections are under its
            # indexing threshold), so this is the parity behaviour. A filtered seq
            # scan of a few thousand 384-d vectors is ~20ms p95. SET LOCAL ends with
            # this transaction, so other queries on the pooled connection keep
            # their index scans (traversal relies on them).
            cur.execute("SET LOCAL enable_indexscan = off")
            cur.execute(
                f"SELECT {', '.join(_E_COLS)}, 1 - (embedding <=> %s::vector) AS score "
                f"FROM kg_entity WHERE graph = %s AND {where} AND embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                [vector, graph, *args, vector, limit],
            )
            return [(_row_to_entity(r), float(r["score"])) for r in cur.fetchall()]

    def exact_matches(self, graph: str, text: str, filt: EntityFilter, limit: int = 5) -> list[Entity]:
        where, args = self._entity_where(filt)
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_E_COLS)} FROM kg_entity "
                f"WHERE graph = %s AND {where} "
                "AND (%s = ANY(aliases) OR native_id = %s OR display_name = %s) LIMIT %s",
                [graph, *args, text, text, text, limit],
            )
            return [_row_to_entity(r) for r in cur.fetchall()]

    def scroll_entities(self, graph: str, filt: EntityFilter, limit: int, cursor: str | None = None):
        where, args = self._entity_where(filt)
        offset = int(cursor) if cursor else 0
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_E_COLS)} FROM kg_entity WHERE graph = %s AND {where} "
                "ORDER BY key LIMIT %s OFFSET %s",
                [graph, *args, limit + 1, offset],
            )
            rows = cur.fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        nxt = str(offset + limit) if more else None
        return [_row_to_entity(r) for r in rows], nxt

    def resume_cursor(self, cursor: str | None, consumed: int, key: str) -> str:
        """Cursor that resumes a scroll_entities page at the item at index `consumed`
        of the page fetched with `cursor`. Postgres scroll cursors are row offsets
        (ORDER BY key), NOT Qdrant point ids — handing back a point id made the next
        page raise ValueError on int(cursor) (RCL-64 P3)."""
        return str((int(cursor) if cursor else 0) + consumed)

    def count_entities(self, graph: str, filt: EntityFilter) -> int:
        where, args = self._entity_where(filt)
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(f"SELECT count(*) AS n FROM kg_entity WHERE graph = %s AND {where}", [graph, *args])
            return int(cur.fetchone()["n"])

    def iter_entities(self, graph: str) -> Iterator[Entity]:
        with self.pool.connection() as c, c.cursor(name="iter_ent") as cur:
            cur.execute(f"SELECT {', '.join(_E_COLS)} FROM kg_entity WHERE graph = %s", (graph,))
            for r in cur:
                yield _row_to_entity(r)

    def delete_entity_hard(self, graph: str, key: str) -> int:
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM kg_edge WHERE graph = %s AND (src = %s OR dst = %s)", (graph, key, key))
            n = cur.rowcount
            cur.execute("DELETE FROM kg_entity WHERE graph = %s AND key = %s", (graph, key))
        return n

    # ── edges ────────────────────────────────────────────────────────────────
    def _edge_rows(self, graph, where, args) -> list[Edge]:
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_G_COLS)} FROM kg_edge WHERE graph = %s AND {where}", [graph, *args])
            return [_row_to_edge(r) for r in cur.fetchall()]

    def _time_cond(self, at_ts, include_retired, args):
        if include_retired:
            return "TRUE"
        args.append(_at(at_ts))
        return "kg_is_current(valid_from, valid_to, %s)"

    def _by(self, field, graph, keys, relations, at_ts, include_retired):
        keys = list(dict.fromkeys(keys))
        if not keys:
            return []
        args = [keys]
        where = f"{field} = ANY(%s)"
        if relations:
            where += " AND relation = ANY(%s)"; args.append(list(relations))
        where += " AND " + self._time_cond(at_ts, include_retired, args)
        return self._edge_rows(graph, where, args)

    def edges_from(self, graph, keys, relations=None, at_ts=None, include_retired=False) -> list[Edge]:
        return self._by("src", graph, keys, relations, at_ts, include_retired)

    def edges_to(self, graph, keys, relations=None, at_ts=None, include_retired=False) -> list[Edge]:
        return self._by("dst", graph, keys, relations, at_ts, include_retired)

    def edges_touching(self, graph, keys, relations=None, at_ts=None, include_retired=False) -> list[Edge]:
        keys = list(dict.fromkeys(keys))
        if not keys:
            return []
        args = [keys, keys]
        where = "(src = ANY(%s) OR dst = ANY(%s))"
        if relations:
            where += " AND relation = ANY(%s)"; args.append(list(relations))
        where += " AND " + self._time_cond(at_ts, include_retired, args)
        return self._edge_rows(graph, where, args)

    def current_edge(self, graph: str, src: str, relation: str, dst: str) -> Edge | None:
        rows = self._edge_rows(graph, "src = %s AND relation = %s AND dst = %s AND valid_to IS NULL",
                               [src, relation, dst])
        return rows[0] if rows else None

    def put_edge(self, graph: str, e: Edge) -> Edge:
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO kg_edge
                   (graph,src,relation,dst,properties,evidence_memory_ids,agent,created_at,valid_from,valid_to,retire_reason)
                   VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,COALESCE(%s, now()),COALESCE(%s, now()),%s,%s)
                   ON CONFLICT (graph, src, relation, dst, valid_from) DO UPDATE SET
                     properties=EXCLUDED.properties, evidence_memory_ids=EXCLUDED.evidence_memory_ids,
                     agent=EXCLUDED.agent, valid_to=EXCLUDED.valid_to, retire_reason=EXCLUDED.retire_reason""",
                (graph, e.src, e.relation, e.dst, json.dumps(e.properties), list(e.evidence_memory_ids),
                 e.agent, _iso(e.created_at), _iso(e.valid_from), _iso(e.valid_to), e.retire_reason),
            )
        return e

    def all_current_edges(self, graph: str) -> Iterator[Edge]:
        with self.pool.connection() as c, c.cursor(name="all_cur_edges") as cur:
            cur.execute(f"SELECT {', '.join(_G_COLS)} FROM kg_edge WHERE graph = %s AND valid_to IS NULL", (graph,))
            for r in cur:
                yield _row_to_edge(r)

    def edges_with_memory(self, graph: str, memory_id: str) -> list[Edge]:
        return self._edge_rows(graph, "%s = ANY(evidence_memory_ids)", [memory_id])

    def iter_edges(self, graph: str) -> Iterator[Edge]:
        with self.pool.connection() as c, c.cursor(name="iter_edges") as cur:
            cur.execute(f"SELECT {', '.join(_G_COLS)} FROM kg_edge WHERE graph = %s", (graph,))
            for r in cur:
                yield _row_to_edge(r)

    # ── cross-graph references ───────────────────────────────────────────────
    def put_xref(self, x: XRef) -> XRef:
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO kg_xref (src,relation,dst,note,evidence_memory_ids,agent,created_at,valid_to,dangling)
                   VALUES (%s,%s,%s,%s,%s,%s,COALESCE(%s, now()),%s,%s)
                   ON CONFLICT (src, relation, dst) DO UPDATE SET
                     note=EXCLUDED.note, evidence_memory_ids=EXCLUDED.evidence_memory_ids,
                     agent=EXCLUDED.agent, valid_to=EXCLUDED.valid_to, dangling=EXCLUDED.dangling""",
                (x.src, x.relation, x.dst, x.note, list(x.evidence_memory_ids), x.agent,
                 _iso(x.created_at), _iso(x.valid_to), x.dangling),
            )
        return x

    def get_xref(self, src: str, relation: str, dst: str) -> XRef | None:
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_X_COLS)} FROM kg_xref WHERE src=%s AND relation=%s AND dst=%s",
                        (src, relation, dst))
            r = cur.fetchone()
        return _row_to_xref(r) if r else None

    def xrefs_touching(self, fq: str) -> list[XRef]:
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_X_COLS)} FROM kg_xref WHERE src=%s OR dst=%s", (fq, fq))
            return [_row_to_xref(r) for r in cur.fetchall()]

    def xrefs_for_graph(self, graph: str) -> list[XRef]:
        like = f"{graph}::%"
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_X_COLS)} FROM kg_xref WHERE src LIKE %s OR dst LIKE %s", (like, like))
            return [_row_to_xref(r) for r in cur.fetchall()]

    def mark_xrefs_dangling(self, fq: str) -> int:
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute("UPDATE kg_xref SET dangling = TRUE WHERE src=%s OR dst=%s", (fq, fq))
            return cur.rowcount

    def delete_xref(self, src: str, relation: str, dst: str) -> None:
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM kg_xref WHERE src=%s AND relation=%s AND dst=%s", (src, relation, dst))

    def delete_xrefs_touching(self, fq: str) -> int:
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM kg_xref WHERE src=%s OR dst=%s", (fq, fq))
            return cur.rowcount


    # ── in-engine graph traversal (RCL-49) ──────────────────────────────────
    # These run the walk server-side in ONE round-trip via recursive CTEs, instead
    # of the app-side BFS (the O(depth) round-trip problem RCL-43 hit on Qdrant).
    # They return the SAME shapes the Python BFS in KGQuery produces, so KGQuery
    # just delegates when the store exposes them. The spike (RCL-46) proved depth-3
    # p95 ~4ms with this approach vs ~217ms for the Qdrant round-trip BFS.

    # Marker so KGQuery can feature-detect the in-engine path.
    supports_sql_traversal = True

    def traverse_keys(self, graph: str, start: str, direction: str, relations,
                      max_depth: int, at_ts: float | None) -> list[tuple[str, int]]:
        """BFS from `start` to max_depth; return (key, shallowest_depth) pairs.
        direction: out|in|both. relations: optional allow-list. Current/as_of gated
        by kg_is_current. The caller hydrates entities + applies kinds/limit, exactly
        as it does for the Python path, so semantics stay identical."""
        at = _at(at_ts)
        rel_sql, rel_args = "", []
        if relations:
            rel_sql = " AND ed.relation = ANY(%s)"
            rel_args = [list(relations)]
        # neighbour expansion per direction
        if direction == "out":
            nbr = f"SELECT ed.dst k FROM kg_edge ed WHERE ed.graph=p.g AND ed.src=w.key AND kg_is_current(ed.valid_from,ed.valid_to,p.at){rel_sql}"
        elif direction == "in":
            nbr = f"SELECT ed.src k FROM kg_edge ed WHERE ed.graph=p.g AND ed.dst=w.key AND kg_is_current(ed.valid_from,ed.valid_to,p.at){rel_sql}"
        else:  # both
            nbr = (f"SELECT ed.dst k FROM kg_edge ed WHERE ed.graph=p.g AND ed.src=w.key AND kg_is_current(ed.valid_from,ed.valid_to,p.at){rel_sql} "
                   f"UNION ALL SELECT ed.src k FROM kg_edge ed WHERE ed.graph=p.g AND ed.dst=w.key AND kg_is_current(ed.valid_from,ed.valid_to,p.at){rel_sql}")
        sql = f"""
        WITH RECURSIVE params AS (SELECT %s::text g, %s::text start, %s::int maxd, %s::timestamptz at),
        walk AS (
            -- Anchor: take the start node as given, NOT as_of-gated on its own
            -- valid_from. Matches KGQuery._must_get (current row, ungated) and the
            -- Qdrant BFS, which seed the walk from `start` regardless of `at` and
            -- time-filter only the EDGES and discovered neighbours. Gating the
            -- anchor here made an as_of traverse return empty whenever the start
            -- entity's valid_from was later than `at` (RCL-51 parity: check 3).
            SELECT p.start key, 0 depth FROM params p
            JOIN kg_entity e ON e.graph=p.g AND e.key=p.start
          UNION
            SELECT nb.k, w.depth+1
            FROM walk w JOIN params p ON true
            CROSS JOIN LATERAL ({nbr}) nb
            -- Neighbour existence only; NOT as_of-gated on the neighbour's own
            -- valid_from (same reason as the anchor). The EDGE is time-filtered
            -- inside {nbr} via kg_is_current, which is what defines the as_of
            -- topology; entities are taken at their current row, matching Qdrant.
            JOIN kg_entity ne ON ne.graph=p.g AND ne.key=nb.k
            WHERE w.depth < p.maxd
        )
        SELECT key, min(depth) depth FROM walk GROUP BY key ORDER BY depth, key
        """
        args = [graph, start, max_depth, at] + rel_args * (3 if direction == "both" else 1)
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(sql, args)
            return [(r["key"], int(r["depth"])) for r in cur.fetchall()]

    def path_keys(self, graph: str, src: str, dst: str, relations, max_depth: int) -> list[list[str]]:
        """Up to 3 shortest paths src->dst (both directions), each a list of keys.
        Matches KGQuery.path's 'first depth dst appears = shortest' + <=3 paths."""
        rel_sql, rel_args = "", []
        if relations:
            rel_sql = " AND ed.relation = ANY(%s)"
            rel_args = [list(relations)]
        sql = f"""
        WITH RECURSIVE params AS (SELECT %s::text g, %s::text src, %s::text dst, %s::int maxd),
        bfs AS (
            SELECT p.src key, 0 depth, ARRAY[p.src] pathkeys FROM params p
          UNION ALL
            SELECT nb.k, b.depth+1, b.pathkeys||nb.k
            FROM bfs b JOIN params p ON true
            CROSS JOIN LATERAL (
              SELECT ed.dst k FROM kg_edge ed WHERE ed.graph=p.g AND ed.src=b.key AND ed.valid_to IS NULL{rel_sql}
              UNION ALL
              SELECT ed.src k FROM kg_edge ed WHERE ed.graph=p.g AND ed.dst=b.key AND ed.valid_to IS NULL{rel_sql}
            ) nb
            WHERE b.depth < p.maxd AND NOT nb.k = ANY(b.pathkeys)
        )
        SELECT pathkeys FROM bfs, params WHERE key=(SELECT dst FROM params)
        ORDER BY depth LIMIT 3
        """
        args = [graph, src, dst, max_depth] + rel_args * 2
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(sql, args)
            return [list(r["pathkeys"]) for r in cur.fetchall()]

    def impact_keys(self, graph: str, key: str, propagating, max_depth: int, at_ts: float | None) -> list[tuple[str, int]]:
        """Reverse reachability over propagating relations (edges INTO), layered by
        depth. Matches KGQuery.impact (edges_to + propagating_relations)."""
        at = _at(at_ts)
        sql = """
        WITH RECURSIVE params AS (SELECT %s::text g, %s::text key, %s::int maxd, %s::text[] props, %s::timestamptz at),
        up AS (
            SELECT p.key key, 0 depth FROM params p
          UNION
            SELECT ed.src, u.depth+1
            FROM up u JOIN params p ON true
            JOIN kg_edge ed ON ed.graph=p.g AND ed.dst=u.key AND ed.relation=ANY(p.props)
                 AND kg_is_current(ed.valid_from,ed.valid_to,p.at)
            WHERE u.depth < p.maxd
        )
        SELECT key, min(depth) depth FROM up WHERE depth>0 GROUP BY key ORDER BY depth, key
        """
        with self.pool.connection() as c, c.cursor() as cur:
            cur.execute(sql, [graph, key, max_depth, list(propagating), at])
            return [(r["key"], int(r["depth"])) for r in cur.fetchall()]

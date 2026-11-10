"""RCL-64 P3: regressions found by the prod Qdrant-vs-Postgres parity run.

No live Postgres needed:
  - traverse: the SQL branch (supports_sql_traversal) must return the SAME edges
    as the BFS branch, i.e. not edges between two outer-ring (depth == max_depth)
    nodes. Driven with a store wrapper whose traverse_keys replays the BFS depths.
  - find: when fit() trims a page, the next cursor is the STORE's format
    (Postgres: row offset), not a Qdrant point id.
  - pg_store.upsert_entity keeps the entity's updated_at; search_entities runs
    exactly (SET LOCAL enable_indexscan = off). Checked on the SQL it sends.
"""
import importlib.util

import pytest

from memory_mcp.kg.query import KGQuery

K = "kubernetes:mcit-k8s"
S = f"{K}/digital-twin/apps/Deployment/hub"
X = f"{K}/digital-twin/apps/Deployment/x"
Y = f"{K}/digital-twin/apps/Deployment/y"
Z = f"{K}/digital-twin/apps/Deployment/z"

needs_psycopg = pytest.mark.skipif(importlib.util.find_spec("psycopg") is None,
                                   reason="postgres extra not installed")


def ent(key, name):
    provider, nid = key.split(":", 1)
    return {"provider": provider, "type": "apps/Deployment", "native_id": nid, "display_name": name}


@pytest.fixture
def ring(svc):
    # S -> X, S -> Y, X -> Y (ring-to-ring at depth 1), Y -> Z
    ents = [ent(S, "hub"), ent(X, "x"), ent(Y, "y"), ent(Z, "z")]
    edges = [{"src": S, "relation": "depends_on", "dst": X},
             {"src": S, "relation": "depends_on", "dst": Y},
             {"src": X, "relation": "depends_on", "dst": Y},
             {"src": Y, "relation": "depends_on", "dst": Z}]
    assert svc.batch("mcit", ents, edges)["status"] == "ok"
    return svc


class SQLTraversalStore:
    """Wraps the Qdrant test store and advertises SQL traversal. traverse_keys
    replays the BFS node depths, so any difference vs the BFS result is purely
    the SQL branch's edge-collection rule (what the bug was)."""

    supports_sql_traversal = True

    def __init__(self, inner, reg):
        self._inner = inner
        self._bfs = KGQuery(reg, inner)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def traverse_keys(self, graph, start, direction, relations, max_depth, at_ts):
        r = self._bfs.traverse(graph, start, direction=direction, relations=relations,
                               max_depth=max_depth, limit=1000, max_chars=10**9)
        return [(n["key"], n["depth"]) for n in r["nodes"]]


def _edges(r):
    return sorted((e["src"], e["relation"], e["dst"]) for e in r["edges"])


@pytest.mark.parametrize("direction", ["out", "in", "both"])
@pytest.mark.parametrize("depth", [1, 2, 3])
def test_sql_traverse_edges_match_bfs(ring, kg_registry, gstore, direction, depth):
    bfs = KGQuery(kg_registry, gstore)
    sql = KGQuery(kg_registry, SQLTraversalStore(gstore, kg_registry))
    start = S if direction != "in" else Z
    a = bfs.traverse("mcit", start, direction=direction, max_depth=depth)
    b = sql.traverse("mcit", start, direction=direction, max_depth=depth)
    assert sorted(n["key"] for n in a["nodes"]) == sorted(n["key"] for n in b["nodes"])
    assert _edges(a) == _edges(b)


def test_sql_traverse_drops_ring_to_ring_edge(ring, kg_registry, gstore):
    sql = KGQuery(kg_registry, SQLTraversalStore(gstore, kg_registry))
    d1 = _edges(sql.traverse("mcit", S, direction="both", max_depth=1))
    assert (X, "depends_on", Y) not in d1          # X and Y are both on the depth-1 ring
    d2 = _edges(sql.traverse("mcit", S, direction="both", max_depth=2))
    assert (X, "depends_on", Y) in d2              # X is expanded at depth 2


class OffsetCursorStore:
    """Qdrant test store, but with Postgres-style row-offset cursors."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def scroll_entities(self, graph, filt, limit, cursor=None):
        everything, nxt = [], None
        while True:
            page, nxt = self._inner.scroll_entities(graph, filt, 1000, nxt)
            everything += page
            if nxt is None:
                break
        everything.sort(key=lambda e: e.key)
        off = int(cursor) if cursor else 0
        rows = everything[off:off + limit + 1]
        more = len(rows) > limit
        return rows[:limit], (str(off + limit) if more else None)

    def resume_cursor(self, cursor, consumed, key):
        from memory_mcp.kg.pg_store import PostgresGraphStore
        return PostgresGraphStore.resume_cursor(self, cursor, consumed, key)


@needs_psycopg
def test_find_trimmed_page_cursor_is_store_format(svc, kg_registry, gstore):
    ents = [{"provider": "logical", "type": "jira_issue", "native_id": f"MCIT-{900 + i}",
             "display_name": f"ticket {i} " + "x" * 200} for i in range(30)]
    assert svc.batch("mcit", ents, [])["status"] == "ok"
    q = KGQuery(kg_registry, OffsetCursorStore(gstore), max_chars=2000)
    seen, cursor, pages = [], None, 0
    while True:
        r = q.find("mcit", kind="work", limit=100, cursor=cursor)
        pages += 1
        seen += [i["key"] for i in r["items"]]
        if not r["has_more"]:
            break
        assert r["cursor"].isdigit()                 # an offset, not a point id
        cursor = r["cursor"]
        assert pages < 100
    assert pages > 1                                 # fit() really trimmed
    assert seen == sorted(set(seen)) and len(seen) == 30   # every item once, in order


class _Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.log.append((sql, args))

    def fetchall(self):
        return []


class _Conn(_Cur):
    def cursor(self, *a, **k):
        return _Cur(self.log)

    def transaction(self):
        return _Cur(self.log)


class _Pool:
    def __init__(self):
        self.log = []

    def connection(self):
        return _Conn(self.log)


def _pg(stub_embedder):
    from memory_mcp.kg.pg_store import PostgresGraphStore
    s = PostgresGraphStore.__new__(PostgresGraphStore)  # no __init__ -> no DB
    s.pool, s.embedder = _Pool(), stub_embedder
    return s


@needs_psycopg
def test_search_entities_is_exact(stub_embedder):
    from memory_mcp.kg.store import EntityFilter
    s = _pg(stub_embedder)
    s.search_entities("mcit", [0.0] * 384, EntityFilter(), 3)
    sqls = [q for q, _ in s.pool.log]
    assert sqls[0] == "SET LOCAL enable_indexscan = off"
    assert "ORDER BY embedding <=>" in sqls[1]


@needs_psycopg
def test_upsert_entity_keeps_updated_at(stub_embedder):
    from datetime import datetime, timezone

    from memory_mcp.kg.models import Entity
    s = _pg(stub_embedder)
    e = Entity(key="logical:T-1", provider="logical", type="jira_issue", kind="work", native_id="T-1",
               display_name="t", created_at="2026-01-01T00:00:00+00:00",
               updated_at="2026-02-02T00:00:00+00:00", valid_from="2026-01-01T00:00:00+00:00")
    s.upsert_entity("mcit", e)
    sql, args = s.pool.log[-1]
    assert "updated_at=EXCLUDED.updated_at" in sql and "updated_at=now()" not in sql
    assert datetime(2026, 2, 2, tzinfo=timezone.utc) in args


@needs_psycopg
def test_pg_resume_cursor_offsets():
    from memory_mcp.kg.pg_store import PostgresGraphStore
    assert PostgresGraphStore.resume_cursor(None, None, 7, "k") == "7"
    assert PostgresGraphStore.resume_cursor(None, "100", 3, "k") == "103"

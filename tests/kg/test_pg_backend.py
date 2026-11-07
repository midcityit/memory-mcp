"""RCL-48: KG_BACKEND selection in build_kg + PostgresGraphStore SQL shaping.

These tests do NOT need a live Postgres. The factory test stubs
PostgresGraphStore construction; the SQL tests drive the private WHERE/arg
builders which are pure string/arg assembly.
"""
import pytest

from memory_mcp.kg import build_kg
from memory_mcp.kg.store import EntityFilter, QdrantGraphStore


def test_backend_default_is_qdrant(qdrant, stub_embedder):
    kg = build_kg(qdrant, stub_embedder)
    assert isinstance(kg.store, QdrantGraphStore)


def test_backend_postgres_requires_dsn(qdrant, stub_embedder):
    with pytest.raises(ValueError, match="KG_PG_DSN"):
        build_kg(qdrant, stub_embedder, backend="postgres", pg_dsn=None)


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("psycopg") is None,
    reason="postgres extra not installed",
)
def test_backend_postgres_selects_pg_store(qdrant, stub_embedder, monkeypatch):
    created = {}

    class FakePG:
        def __init__(self, dsn, embedder, **kw):
            created["dsn"] = dsn
            created["embedder"] = embedder

        def ensure_graph(self, g):
            created.setdefault("graphs", []).append(g)

        def ensure_xrefs(self):
            created["xrefs"] = True

        def count_entities(self, g, f):  # for the observable gauge callback
            return 0

    import memory_mcp.kg.pg_store as pg_mod
    monkeypatch.setattr(pg_mod, "PostgresGraphStore", FakePG)

    kg = build_kg(qdrant, stub_embedder, backend="postgres",
                  pg_dsn="postgresql://kg:kg@pg-spike.rcl44-spike.svc:5432/kg")
    assert isinstance(kg.store, FakePG)
    assert created["dsn"].endswith("/kg")
    assert created["xrefs"] is True
    # every registry graph got ensure_graph (no-op on PG, but called for parity)
    assert set(created["graphs"]) == set(kg.registry.graphs)


# ── pure SQL-shaping checks (no DB) ──────────────────────────────────────────
def _pg_store_cls():
    # import lazily so the module's psycopg import only runs when deps are present
    from memory_mcp.kg.pg_store import PostgresGraphStore
    return PostgresGraphStore


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("psycopg") is None,
    reason="postgres extra not installed",
)
def test_entity_where_current_filter():
    PostgresGraphStore = _pg_store_cls()
    s = PostgresGraphStore.__new__(PostgresGraphStore)  # no __init__ -> no pool/DB
    where, args = s._entity_where(EntityFilter(provider="azure", kind="work"))
    assert "provider = %s" in where and "kind = %s" in where
    assert "kg_is_current(valid_from, valid_to, %s)" in where  # current-only by default
    assert args[0] == "azure" and args[1] == "work"


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("psycopg") is None,
    reason="postgres extra not installed",
)
def test_entity_where_include_retired_drops_time_cond():
    PostgresGraphStore = _pg_store_cls()
    s = PostgresGraphStore.__new__(PostgresGraphStore)
    where, args = s._entity_where(EntityFilter(include_retired=True))
    assert "kg_is_current" not in where

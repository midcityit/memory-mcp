"""RCL-50: DualWriteGraphStore fans writes to both stores, reads from primary."""
from memory_mcp.kg.dual_write import DualWriteGraphStore


class FakeStore:
    def __init__(self, name, embedder="E", sql=False):
        self.name = name
        self.embedder = embedder
        self.supports_sql_traversal = sql
        self.calls = []

    # a write method
    def upsert_entity(self, graph, e):
        self.calls.append(("upsert_entity", graph, e))
        return e

    # another write
    def put_edge(self, graph, e):
        self.calls.append(("put_edge", graph, e))
        return e

    # a read method
    def get_entity(self, graph, key):
        self.calls.append(("get_entity", graph, key))
        return f"{self.name}:{key}"


def test_write_fans_out_to_both():
    p, s = FakeStore("primary"), FakeStore("secondary")
    dw = DualWriteGraphStore(p, s)
    dw.upsert_entity("mcit", "E1")
    dw.put_edge("mcit", "edge1")
    assert ("upsert_entity", "mcit", "E1") in p.calls
    assert ("upsert_entity", "mcit", "E1") in s.calls
    assert ("put_edge", "mcit", "edge1") in p.calls
    assert ("put_edge", "mcit", "edge1") in s.calls


def test_read_hits_primary_only():
    p, s = FakeStore("primary"), FakeStore("secondary")
    dw = DualWriteGraphStore(p, s)
    assert dw.get_entity("mcit", "k") == "primary:k"
    assert ("get_entity", "mcit", "k") in p.calls
    assert s.calls == []  # secondary untouched by reads


def test_secondary_failure_swallowed_by_default():
    class Boom(FakeStore):
        def put_edge(self, graph, e):
            raise RuntimeError("secondary down")

    p, s = FakeStore("primary"), Boom("secondary")
    dw = DualWriteGraphStore(p, s)
    # primary still succeeds; secondary error is logged, not raised
    dw.put_edge("mcit", "edge1")
    assert ("put_edge", "mcit", "edge1") in p.calls


def test_secondary_failure_raised_when_strict():
    import pytest

    class Boom(FakeStore):
        def put_edge(self, graph, e):
            raise RuntimeError("secondary down")

    p, s = FakeStore("primary"), Boom("secondary")
    dw = DualWriteGraphStore(p, s, strict=True)
    with pytest.raises(RuntimeError, match="secondary down"):
        dw.put_edge("mcit", "edge1")


def test_embedder_and_sql_marker_from_primary():
    p = FakeStore("primary", embedder="PRIM", sql=True)
    s = FakeStore("secondary", embedder="SEC", sql=False)
    dw = DualWriteGraphStore(p, s)
    assert dw.embedder == "PRIM"
    assert dw.supports_sql_traversal is True  # reads come from primary

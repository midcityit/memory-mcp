import pytest
from memory_mcp.kg.models import Edge, Entity, XRef, iso_to_ts, now_iso
from memory_mcp.kg.store import EntityFilter, QdrantGraphStore, entities_collection, edges_collection


def ent(key, kind="container", typ="apps/Deployment", name=None, provider="kubernetes", vf="2026-01-01T00:00:00+00:00", vt=None, mem=None):
    return Entity(key=key, provider=provider, type=typ, kind=kind, native_id=key.split(":", 1)[1],
                  display_name=name or key, memory_ids=mem or [], valid_from=vf, valid_to=vt,
                  created_at=vf, updated_at=vf)


def edge(src, rel, dst, vf="2026-01-01T00:00:00+00:00", vt=None):
    return Edge(src=src, relation=rel, dst=dst, valid_from=vf, valid_to=vt, created_at=vf)


@pytest.fixture
def gs(qdrant, stub_embedder):
    s = QdrantGraphStore(qdrant, stub_embedder)
    s.ensure_graph("mcit")
    s.ensure_xrefs()
    return s


def test_ensure_is_idempotent_and_creates_collections(gs, qdrant):
    gs.ensure_graph("mcit")
    names = {c.name for c in qdrant.get_collections().collections}
    assert {entities_collection("mcit"), edges_collection("mcit"), "kg_xrefs"} <= names


def test_upsert_get_and_overwrite(gs):
    gs.upsert_entity("mcit", ent("kubernetes:a", name="alpha"))
    gs.upsert_entity("mcit", ent("kubernetes:a", name="alpha2"))
    assert gs.get_entity("mcit", "kubernetes:a").display_name == "alpha2"
    assert gs.get_entity("mcit", "kubernetes:missing") is None
    assert [e.key for e in gs.get_entities("mcit", ["kubernetes:a", "kubernetes:missing"])] == ["kubernetes:a"]


def test_search_with_kind_filter_and_retired_excluded(gs):
    gs.upsert_entity("mcit", ent("kubernetes:memory-mcp", name="memory mcp server"))
    gs.upsert_entity("mcit", ent("kubernetes:memory-svc", kind="network", typ="core/Service", name="memory mcp service"))
    gs.upsert_entity("mcit", ent("kubernetes:old", name="memory mcp old", vt="2026-02-01T00:00:00+00:00"))
    hits = gs.search_entities("mcit", gs.embedder.embed("memory mcp"), EntityFilter(kind="container"), 10)
    assert [e.key for e, _ in hits] == ["kubernetes:memory-mcp"]
    hits = gs.search_entities("mcit", gs.embedder.embed("memory mcp"), EntityFilter(kind="container", include_retired=True), 10)
    assert {e.key for e, _ in hits} == {"kubernetes:memory-mcp", "kubernetes:old"}


def test_scroll_paging_and_count(gs):
    for i in range(5):
        gs.upsert_entity("mcit", ent(f"kubernetes:e{i}"))
    page1, cur = gs.scroll_entities("mcit", EntityFilter(), 3)
    page2, cur2 = gs.scroll_entities("mcit", EntityFilter(), 3, cur)
    assert len(page1) == 3 and len(page2) == 2 and cur2 is None
    assert {e.key for e in page1 + page2} == {f"kubernetes:e{i}" for i in range(5)}
    assert gs.count_entities("mcit", EntityFilter()) == 5


def test_memory_id_filter(gs):
    gs.upsert_entity("mcit", ent("kubernetes:a", mem=["m1", "m2"]))
    gs.upsert_entity("mcit", ent("kubernetes:b", mem=["m3"]))
    page, _ = gs.scroll_entities("mcit", EntityFilter(memory_id="m2"), 10)
    assert [e.key for e in page] == ["kubernetes:a"]


def test_edges_time_filter(gs):
    gs.put_edge("mcit", edge("a", "runs_on", "ms01", vf="2026-05-01T00:00:00+00:00", vt="2026-09-27T00:00:00+00:00"))
    gs.put_edge("mcit", edge("a", "runs_on", "mcit", vf="2026-09-27T00:00:00+00:00"))
    now = [e.dst for e in gs.edges_from("mcit", ["a"])]
    past = [e.dst for e in gs.edges_from("mcit", ["a"], at_ts=iso_to_ts("2026-09-20T00:00:00+00:00"))]
    every = sorted(e.dst for e in gs.edges_from("mcit", ["a"], include_retired=True))
    assert now == ["mcit"] and past == ["ms01"] and every == ["mcit", "ms01"]
    assert [e.src for e in gs.edges_to("mcit", ["mcit"])] == ["a"]


def test_edges_relation_filter_and_large_frontier(gs):
    for i in range(620):
        gs.put_edge("mcit", edge(f"s{i}", "runs_on", "hub"))
    gs.put_edge("mcit", edge("s1", "tracked_in", "MCIT-1"))
    out = gs.edges_from("mcit", [f"s{i}" for i in range(620)], relations=["runs_on"])
    assert len(out) == 620  # frontier is chunked by FRONTIER_CHUNK
    assert len(gs.edges_to("mcit", ["hub"])) == 620


def test_current_edge_and_all_current(gs):
    gs.put_edge("mcit", edge("a", "runs_on", "b", vt="2026-02-01T00:00:00+00:00"))
    assert gs.current_edge("mcit", "a", "runs_on", "b") is None
    gs.put_edge("mcit", edge("a", "runs_on", "b", vf="2026-03-01T00:00:00+00:00"))
    assert gs.current_edge("mcit", "a", "runs_on", "b").valid_from == "2026-03-01T00:00:00+00:00"
    assert len(list(gs.all_current_edges("mcit"))) == 1
    assert len(list(gs.iter_edges("mcit"))) == 2


def test_delete_entity_hard(gs):
    gs.upsert_entity("mcit", ent("kubernetes:a"))
    gs.put_edge("mcit", edge("kubernetes:a", "runs_on", "x"))
    gs.put_edge("mcit", edge("y", "depends_on", "kubernetes:a"))
    assert gs.delete_entity_hard("mcit", "kubernetes:a") == 2
    assert gs.get_entity("mcit", "kubernetes:a") is None and list(gs.iter_edges("mcit")) == []


def test_xrefs(gs):
    x = XRef(src="vtv::logical:VTV-238", relation="pattern_from", dst="mcit::logical:MCIT-184", note="KV CSI", created_at=now_iso())
    gs.put_xref(x)
    gs.put_xref(x)  # idempotent
    assert gs.get_xref(x.src, x.relation, x.dst).note == "KV CSI"
    assert len(gs.xrefs_touching("mcit::logical:MCIT-184")) == 1
    assert len(gs.xrefs_for_graph("vtv")) == 1
    assert gs.mark_xrefs_dangling("mcit::logical:MCIT-184") == 1
    assert gs.xrefs_touching("mcit::logical:MCIT-184")[0].dangling is True

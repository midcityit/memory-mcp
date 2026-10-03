from types import SimpleNamespace

from memory_mcp.kg import build_kg
from memory_mcp.kg.store import edges_collection, entities_collection


def test_build_kg_creates_collections_and_memory_lookup(qdrant, stub_embedder):
    rec = SimpleNamespace(id="m1", name="runbook-x", type="runbook")
    mem = SimpleNamespace(get=lambda i: rec if i == "m1" else None)
    kg = build_kg(qdrant, stub_embedder, memory_store=mem)
    names = {c.name for c in qdrant.get_collections().collections}
    assert {entities_collection("mcit"), edges_collection("vtv"), "kg_xrefs"} <= names
    assert kg.query.memory_lookup("m1") == {"id": "m1", "name": "runbook-x", "type": "runbook"}
    assert kg.query.memory_lookup("missing") is None


def test_build_kg_config_dir_override(qdrant, stub_embedder, tmp_path):
    (tmp_path / "kg_graphs.yaml").write_text("graphs:\n  lab: {name: Lab}\n")
    (tmp_path / "kg_relations.yaml").write_text("relations:\n  runs_on: {inverse: runs, impact: propagates}\n")
    kg = build_kg(qdrant, stub_embedder, config_dir=str(tmp_path))
    assert list(kg.registry.graphs) == ["lab"]
    assert "apps/Deployment" in kg.registry.types["kubernetes"]  # packaged catalogs used when no catalog/ subdir

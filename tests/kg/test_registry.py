import pytest
from memory_mcp.kg.models import KGError
from memory_mcp.kg.registry import Registry, PROVIDERS


@pytest.fixture
def reg():
    return Registry.load()  # packaged config; catalogs may be empty at this point


def test_packaged_graphs(reg):
    assert set(reg.graphs) == {"mcit", "vtv"}
    assert reg.graph_ids("*") == ["mcit", "vtv"]
    assert reg.graph_ids("vtv") == ["vtv"]
    assert reg.graph_ids(["mcit"]) == ["mcit"]


def test_unknown_graph_suggests(reg):
    with pytest.raises(KGError) as e:
        reg.require_graph("mcti")
    assert e.value.code == "unknown_graph" and "mcit" in e.value.suggestions


def test_relations_loaded_with_defaults(reg):
    r = reg.relation("runs_on")
    assert (r.inverse, r.cls, r.impact) == ("runs", "topology", "propagates")
    assert "container" in r.src_kinds
    assert reg.relation("member_of").impact == "none" and reg.relation("member_of").src_kinds is None
    assert reg.relation("pattern_from").cls == "reference"
    assert reg.inverse_label("tracked_in") == "tracks"


def test_propagating_relations(reg):
    p = set(reg.propagating_relations())
    assert {"runs_on", "hosted_on", "depends_on", "exposed_via", "resolves_to",
            "queries", "reads_from", "notifies", "served_by"} <= p
    assert not {"member_of", "tracked_in", "monitors", "visualizes", "recorded_in", "sends_metrics_to"} & p


def test_unknown_relation_and_provider(reg):
    with pytest.raises(KGError) as e:
        reg.relation("run_on")
    assert e.value.code == "unknown_relation" and "runs_on" in e.value.suggestions
    with pytest.raises(KGError) as e:
        reg.require_provider("azrue")
    assert e.value.code == "unknown_provider" and "azure" in e.value.suggestions
    assert {"cloudflare", "grafana", "prometheus", "netbox"} <= set(PROVIDERS)


def test_bad_graph_id_in_config_rejected(tmp_path):
    (tmp_path / "kg_graphs.yaml").write_text("graphs:\n  Bad-ID: {name: x}\n")
    (tmp_path / "kg_relations.yaml").write_text("relations: {}\n")
    with pytest.raises(ValueError):
        Registry.load(config_dir=tmp_path, catalog_dir=tmp_path)

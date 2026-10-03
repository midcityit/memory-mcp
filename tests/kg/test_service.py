import pytest
from memory_mcp.kg.models import KGError

SUB = "/subscriptions/00000000-0000-0000-0000-000000000001"
AKS_ID = f"{SUB}/resourceGroups/rg-aks/providers/Microsoft.ContainerService/managedClusters/aks-vtv-prod"


def dep(svc, name, ns="digital-twin", graph="mcit", **kw):
    return svc.upsert_entity(graph, "kubernetes", "apps/Deployment", f"mcit-k8s/{ns}/apps/Deployment/{name}",
                             display_name=name, **kw)


def node(svc, name="opi-5", graph="mcit"):
    return svc.upsert_entity(graph, "kubernetes", "core/Node", f"mcit-k8s/_cluster/core/Node/{name}", display_name=name)


def jira(svc, key, graph="mcit"):
    return svc.upsert_entity(graph, "logical", "jira_issue", key)


def test_upsert_create_then_update_merges(svc, gstore):
    r = dep(svc, "memory-mcp", aliases=["memory twin"], properties={"replicas": 1, "tier": "prod"}, memory_ids=["m1"])
    assert r["status"] == "created" and r["kind"] == "container"
    r2 = dep(svc, "memory-mcp", aliases=["memory-twin prod"], properties={"replicas": 2, "tier": None}, memory_ids=["m2"])
    assert r2["status"] == "updated" and r2["key"] == r["key"]
    e = gstore.get_entity("mcit", r["key"])
    assert e.aliases == ["memory twin", "memory-twin prod"]
    assert e.properties == {"replicas": 2}
    assert e.memory_ids == ["m1", "m2"]


def test_upsert_same_arm_id_different_case_updates(svc):
    a = svc.upsert_entity("vtv", "azure", "azurerm_kubernetes_cluster", AKS_ID, display_name="aks-vtv-prod")
    b = svc.upsert_entity("vtv", "azure", "Microsoft.ContainerService/managedClusters", AKS_ID.upper() + "/",
                          display_name="aks-vtv-prod")
    assert a["status"] == "created" and b["status"] == "updated" and a["key"] == b["key"]


def test_upsert_errors(svc):
    with pytest.raises(KGError) as e:
        svc.upsert_entity("nope", "kubernetes", "apps/Deployment", "c/n/apps/Deployment/x")
    assert e.value.code == "unknown_graph"
    with pytest.raises(KGError) as e:
        svc.upsert_entity("mcit", "kubernetes", "apps/Deploymnet", "c/n/apps/Deployment/x")
    assert e.value.code == "unknown_type"
    with pytest.raises(KGError) as e:
        svc.upsert_entity("vtv", "azure", "Microsoft.Compute/virtualMachines",
                          f"{SUB}/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic1")
    assert e.value.code == "type_id_mismatch"


def test_duplicate_hint_on_create(svc):
    dep(svc, "memory-mcp")
    r = dep(svc, "memory-mcp", ns="digital-twin-dev")
    assert r["status"] == "created"
    assert [d["key"] for d in r["possible_duplicates"]] == ["kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"]


def test_link_create_update_and_constraints(svc, gstore):
    d, n, j = dep(svc, "memory-mcp")["key"], node(svc)["key"], jira(svc, "MCIT-193")["key"]
    assert svc.link("mcit", d, "runs_on", n)["status"] == "created"
    assert svc.link("mcit", d, "runs_on", n, evidence_memory_ids=["m9"])["status"] == "updated"
    assert len(list(gstore.iter_edges("mcit"))) == 1
    assert gstore.current_edge("mcit", d, "runs_on", n).evidence_memory_ids == ["m9"]
    svc.link("mcit", d, "tracked_in", j)
    with pytest.raises(KGError) as e:
        svc.link("mcit", d, "tracked_in", n)  # tracked_in target must be kind 'work'
    assert e.value.code == "kind_constraint"
    with pytest.raises(KGError) as e:
        svc.link("mcit", d, "pattern_from", j)
    assert e.value.code == "relation_class_mismatch"
    with pytest.raises(KGError) as e:
        svc.link("mcit", d, "runs_on", "vtv::" + n)
    assert e.value.code == "cross_graph_edge"
    with pytest.raises(KGError) as e:
        svc.link("mcit", d, "runs_on", "kubernetes:missing")
    assert e.value.code == "endpoint_not_found" and e.value.field == "dst"


def test_link_to_retired_entity_rejected(svc):
    d, n = dep(svc, "memory-mcp")["key"], node(svc, "ms01-k8s-wkr-01")["key"]
    svc.retire_entity("mcit", n, "decommissioned MCIT-251")
    with pytest.raises(KGError) as e:
        svc.link("mcit", d, "runs_on", n)
    assert e.value.code == "endpoint_not_found"


def test_unlink_then_relink_keeps_history(svc, gstore):
    d, n = dep(svc, "memory-mcp")["key"], node(svc)["key"]
    svc.link("mcit", d, "runs_on", n, valid_from="2026-09-01T00:00:00+00:00")
    assert svc.unlink("mcit", d, "runs_on", n, "moved")["status"] == "retired"
    svc.link("mcit", d, "runs_on", n)
    edges = list(gstore.iter_edges("mcit"))
    assert len(edges) == 2 and sum(e.valid_to is None for e in edges) == 1
    assert [e.retire_reason for e in edges if e.valid_to] == ["moved"]
    with pytest.raises(KGError) as e:
        svc.unlink("mcit", d, "depends_on", n, "x")
    assert e.value.code == "not_found"


def test_retire_entity_cascades_both_directions(svc, gstore):
    d, n, j = dep(svc, "memory-mcp")["key"], node(svc)["key"], jira(svc, "MCIT-193")["key"]
    svc.link("mcit", d, "runs_on", n)
    svc.link("mcit", d, "tracked_in", j)
    r = svc.retire_entity("mcit", n, "node rebuilt")
    assert r == {"key": n, "status": "retired", "edges_retired": 1}
    assert gstore.get_entity("mcit", n).valid_to is not None
    assert [e.relation for e in gstore.all_current_edges("mcit")] == ["tracked_in"]
    with pytest.raises(KGError) as e:
        svc.retire_entity("mcit", n, "again")
    assert e.value.code == "not_found"


def test_revive_retired_entity(svc, gstore):
    n = node(svc)["key"]
    svc.retire_entity("mcit", n, "x")
    assert node(svc)["status"] == "revived"
    assert gstore.get_entity("mcit", n).valid_to is None


BATCH_ENTS = [
    {"provider": "kubernetes", "type": "apps/Deployment", "native_id": "mcit-k8s/digital-twin/apps/Deployment/memory-mcp", "display_name": "memory-mcp"},
    {"provider": "kubernetes", "type": "core/Node", "native_id": "mcit-k8s/_cluster/core/Node/opi-5", "display_name": "opi-5"},
    {"provider": "kubernetes", "type": "core/Node", "native_id": "ms01-k8s/_cluster/core/Node/ms01-k8s-wkr-01",
     "display_name": "ms01-k8s-wkr-01", "valid_from": "2026-04-01T00:00:00+00:00", "valid_to": "2026-09-27T07:00:00+00:00"},
]
DEP = "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"
BATCH_EDGES = [
    {"src": DEP, "relation": "runs_on", "dst": "kubernetes:mcit-k8s/_cluster/core/Node/opi-5", "valid_from": "2026-09-27T07:00:00+00:00"},
    {"src": DEP, "relation": "runs_on", "dst": "kubernetes:ms01-k8s/_cluster/core/Node/ms01-k8s-wkr-01",
     "valid_from": "2026-04-01T00:00:00+00:00", "valid_to": "2026-09-27T07:00:00+00:00", "retire_reason": "MCIT-193 cutover"},
]


def test_batch_same_batch_endpoints_and_idempotent(svc, gstore):
    r = svc.batch("mcit", BATCH_ENTS, BATCH_EDGES)
    assert r["status"] == "ok"
    assert [w["status"] for w in r["written"]] == ["created", "created", "retired", "created", "recorded"]
    n_edges = len(list(gstore.iter_edges("mcit")))
    r2 = svc.batch("mcit", BATCH_ENTS, BATCH_EDGES)
    assert [w["status"] for w in r2["written"]] == ["updated", "updated", "retired", "updated", "recorded"]
    assert len(list(gstore.iter_edges("mcit"))) == n_edges == 2


def test_batch_rejects_everything_on_any_error(svc, gstore):
    bad_ents = BATCH_ENTS[:2] + [{"provider": "kubernetes", "type": "apps/Deploymnet", "native_id": "c/n/apps/Deployment/x"}]
    bad_edges = [{"src": DEP, "relation": "run_on", "dst": "kubernetes:mcit-k8s/_cluster/core/Node/opi-5"}]
    r = svc.batch("mcit", bad_ents, bad_edges)
    assert r["status"] == "rejected" and r["written"] == []
    assert {(e["item"], e["error"]) for e in r["errors"]} == {("entities[2]", "unknown_type"), ("edges[0]", "unknown_relation")}
    assert list(gstore.iter_entities("mcit")) == []


def test_xref(svc):
    jira(svc, "MCIT-184")
    svc.upsert_entity("vtv", "logical", "jira_issue", "VTV-238")
    r = svc.xref("vtv::logical:VTV-238", "pattern_from", "mcit::logical:MCIT-184", "Key Vault CSI pattern")
    assert r["status"] == "created"
    assert svc.xref("vtv::logical:VTV-238", "pattern_from", "mcit::logical:MCIT-184", "updated note")["status"] == "updated"
    with pytest.raises(KGError) as e:
        svc.xref("vtv::logical:VTV-238", "runs_on", "mcit::logical:MCIT-184", "x")
    assert e.value.code == "relation_class_mismatch"
    with pytest.raises(KGError) as e:
        svc.xref("vtv::logical:VTV-999", "pattern_from", "mcit::logical:MCIT-184", "x")
    assert e.value.code == "endpoint_not_found"

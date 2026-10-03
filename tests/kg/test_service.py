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


def test_batch_collects_shape_errors_for_all_items(svc, gstore):
    bad_ents = [
        "oops",  # non-dict item
        {"provider": "kubernetes", "type": "core/Node", "native_id": 5, "display_name": "bad-id"},  # native_id is int
        {"provider": "kubernetes", "type": "core/Node", "native_id": "mcit-k8s/_cluster/core/Node/ok", "display_name": "ok"},  # valid
    ]
    bad_edges = [
        {"src": None, "relation": "runs_on", "dst": "kubernetes:mcit-k8s/_cluster/core/Node/opi-5"},  # src is None
        {"src": "kubernetes:mcit-k8s/_cluster/core/Node/opi-5", "relation": 7, "dst": "kubernetes:mcit-k8s/_cluster/core/Node/opi-6"},  # relation is int
    ]
    r = svc.batch("mcit", bad_ents, bad_edges)
    assert r["status"] == "rejected" and r["written"] == []
    assert len(r["errors"]) == 4
    # Check all errors are present with correct codes
    error_items = {e["item"]: e["error"] for e in r["errors"]}
    assert error_items == {
        "entities[0]": "invalid_native_id",
        "entities[1]": "invalid_native_id",
        "edges[0]": "endpoint_not_found",
        "edges[1]": "unknown_relation",
    }
    # Verify no entities were written
    assert list(gstore.iter_entities("mcit")) == []


# ── final-review fixes ──────────────────────────────────────────────────────
def _hist_edge(**kw):
    return {"src": "kubernetes:mcit-k8s/digital-twin/apps/Deployment/a", "relation": "runs_on",
            "dst": "kubernetes:mcit-k8s/_cluster/core/Node/n", **kw}


def _hist_setup(svc):
    dep(svc, "a")
    node(svc, "n")


def test_historical_edge_requires_valid_from(svc):
    _hist_setup(svc)
    r = svc.batch("mcit", [], [_hist_edge(valid_to="2026-05-01T00:00:00+00:00")])
    assert r["status"] == "rejected" and r["errors"][0]["field"] == "valid_from"


def test_inverted_interval_rejected(svc):
    _hist_setup(svc)
    r = svc.batch("mcit", [], [_hist_edge(valid_from="2026-06-01T00:00:00+00:00", valid_to="2026-05-01T00:00:00+00:00")])
    assert r["status"] == "rejected" and r["errors"][0]["field"] in ("valid_from", "valid_to")
    r = svc.batch("mcit", [{"provider": "logical", "type": "jira_issue", "native_id": "MCIT-7",
                            "valid_to": "2026-05-01T00:00:00+00:00"}], [])
    assert r["status"] == "rejected" and r["errors"][0]["field"] == "valid_from"
    r = svc.batch("mcit", [{"provider": "logical", "type": "jira_issue", "native_id": "MCIT-7",
                            "valid_from": "2026-06-01T00:00:00+00:00", "valid_to": "2026-05-01T00:00:00+00:00"}], [])
    assert r["status"] == "rejected"
    with pytest.raises(KGError) as e:
        svc.upsert_entity("mcit", "logical", "jira_issue", "MCIT-8", valid_to="2026-05-01T00:00:00+00:00")
    assert e.value.field == "valid_from"


def test_link_accepts_valid_from_alone(svc):
    _hist_setup(svc)
    assert svc.link("mcit", _hist_edge()["src"], "runs_on", _hist_edge()["dst"],
                    valid_from="2026-01-01T00:00:00+00:00")["status"] == "created"


def test_historical_batch_is_idempotent(svc, gstore):
    _hist_setup(svc)
    e = _hist_edge(valid_from="2026-01-01T00:00:00+00:00", valid_to="2026-05-01T00:00:00+00:00")
    for _ in range(2):
        assert svc.batch("mcit", [], [e])["status"] == "ok"
    assert len(gstore.edges_from("mcit", [e["src"]], include_retired=True)) == 1


@pytest.mark.parametrize("kw,field", [
    ({"display_name": "d" * 257}, "display_name"),
    ({"aliases": ["a"] * 33}, "aliases"),
    ({"aliases": ["a" * 257]}, "aliases"),
])
def test_field_caps(svc, kw, field):
    with pytest.raises(KGError) as e:
        svc.upsert_entity("mcit", "logical", "jira_issue", "MCIT-9", **kw)
    assert e.value.code == "invalid_native_id" and e.value.field == field
    r = svc.batch("mcit", [{"provider": "logical", "type": "jira_issue", "native_id": "MCIT-9", **kw}], [])
    assert r["status"] == "rejected" and r["errors"][0]["field"] == field


def test_native_id_cap(svc):
    with pytest.raises(KGError) as e:
        svc.upsert_entity("mcit", "logical", "jira_issue", "MCIT-" + "9" * 1030)
    assert e.value.code == "invalid_native_id" and e.value.field == "native_id"


def test_caps_allow_boundary(svc):
    assert svc.upsert_entity("mcit", "logical", "jira_issue", "MCIT-10", display_name="d" * 256,
                             aliases=["a" * 256] * 32)["status"] == "created"


def test_batch_rejects_unknown_keys(svc):
    _hist_setup(svc)
    r = svc.batch("mcit", [{"provider": "logical", "type": "jira_issue", "native_id": "MCIT-11", "valid_form": "x"}], [])
    assert r["status"] == "rejected" and r["errors"][0]["field"] == "valid_form"
    assert r["errors"][0]["error"] == "invalid_native_id" and "allowed" in r["errors"][0]["message"].lower()
    r = svc.batch("mcit", [], [_hist_edge(relaton="x")])
    assert r["status"] == "rejected" and r["errors"][0]["field"] == "relaton"
    assert r["errors"][0]["error"] == "invalid_native_id"
    assert svc.batch("mcit", [], [_hist_edge(agent="me")])["status"] == "ok"

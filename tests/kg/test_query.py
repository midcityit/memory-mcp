import json

import pytest
from memory_mcp.kg.models import KGError
from memory_mcp.kg.query import KGQuery, fit

ACCT = "0123456789abcdef0123456789abcdef"
K = "kubernetes:mcit-k8s"
DNS = f"cloudflare:{ACCT}/chriscastrotech.com/dns_record/memory-mcp"
TUN = f"cloudflare:{ACCT}/-/zero_trust_tunnel_cloudflared/homelab"
GW = f"{K}/envoy-gateway-system/gateway.networking.k8s.io/Gateway/eg"
RT = f"{K}/digital-twin/gateway.networking.k8s.io/HTTPRoute/memory-mcp"
SVC = f"{K}/digital-twin/core/Service/memory-mcp"
DEP = f"{K}/digital-twin/apps/Deployment/memory-mcp"
OPI = f"{K}/_cluster/core/Node/opi-5"
MS01 = "kubernetes:ms01-k8s/_cluster/core/Node/ms01-k8s-wkr-01"
JIRA = "logical:MCIT-193"
CUT = "2026-09-27T07:00:00+00:00"


def ent(key, typ, name, **kw):
    provider, nid = key.split(":", 1)
    return {"provider": provider, "type": typ, "native_id": nid, "display_name": name, **kw}


@pytest.fixture
def topo(svc):
    ents = [
        ent(DNS, "dns_record", "memory-mcp.chriscastrotech.com"),
        ent(TUN, "zero_trust_tunnel_cloudflared", "homelab tunnel"),
        ent(GW, "gateway.networking.k8s.io/Gateway", "envoy gateway eg"),
        ent(RT, "gateway.networking.k8s.io/HTTPRoute", "memory-mcp route"),
        ent(SVC, "core/Service", "memory-mcp service"),
        ent(DEP, "apps/Deployment", "memory-mcp", aliases=["memory twin prod"], memory_ids=["mem-1"]),
        ent(OPI, "core/Node", "opi-5"),
        ent(MS01, "core/Node", "ms01-k8s-wkr-01", valid_from="2026-04-01T00:00:00+00:00", valid_to=CUT),
        ent(JIRA, "jira_issue", "MCIT-193"),
    ]
    edges = [
        {"src": DNS, "relation": "resolves_to", "dst": TUN},
        {"src": TUN, "relation": "routes_to", "dst": GW},
        {"src": GW, "relation": "routes_to", "dst": RT},
        {"src": RT, "relation": "routes_to", "dst": SVC},
        {"src": SVC, "relation": "routes_to", "dst": DEP},
        {"src": DEP, "relation": "runs_on", "dst": OPI, "valid_from": CUT, "evidence_memory_ids": ["mem-2"]},
        {"src": DEP, "relation": "runs_on", "dst": MS01, "valid_from": "2026-04-01T00:00:00+00:00", "valid_to": CUT,
         "retire_reason": "MCIT-193 cutover"},
        {"src": DEP, "relation": "tracked_in", "dst": JIRA},
    ]
    assert svc.batch("mcit", ents, edges)["status"] == "ok"
    return svc


MEMS = {"mem-1": {"id": "mem-1", "name": "memory-mcp-deployment", "type": "runbook"},
        "mem-2": {"id": "mem-2", "name": "MCIT-193 cutover", "type": "decision"}}


@pytest.fixture
def q(kg_registry, gstore, topo):
    return KGQuery(kg_registry, gstore, memory_lookup=MEMS.get)


def keys(nodes):
    return {n["key"] for n in nodes}


def test_resolve_exact_alias_first_and_graph_label(q):
    r = q.resolve("memory twin prod")["results"]
    assert r[0]["key"] == DEP and r[0]["score"] == 1.0 and r[0]["graph"] == "mcit"
    assert q.resolve(DEP, graphs="mcit")["results"][0]["key"] == DEP


def test_resolve_excludes_retired_unless_asked(q):
    assert MS01 not in keys(q.resolve("ms01-k8s-wkr-01", kind="host")["results"])
    hit = [x for x in q.resolve("ms01-k8s-wkr-01", kind="host", include_retired=True)["results"] if x["key"] == MS01]
    assert hit and hit[0]["retired"] is True


def test_get_entity_groups_edges_with_inverse_labels(q):
    r = q.get_entity("mcit", DEP)
    assert r["entity"]["key"] == DEP
    assert keys(r["out"]["runs_on"]) == {OPI} and keys(r["out"]["tracked_in"]) == {JIRA}
    assert keys(r["in"]["routed_from"]) == {SVC}
    assert r["memories"] == [MEMS["mem-1"]]
    past = q.get_entity("mcit", DEP, as_of="2026-09-20T00:00:00+00:00")
    assert keys(past["out"]["runs_on"]) == {MS01}
    hist = q.get_entity("mcit", DEP, include_history=True)["history"]
    assert any(h["event"] == "close" and h["dst"] == MS01 and h["reason"] == "MCIT-193 cutover" for h in hist)
    with pytest.raises(KGError) as e:
        q.get_entity("mcit", "kubernetes:nope")
    assert e.value.code == "not_found"


def test_find_paging_and_orphans(q):
    first = q.find("mcit", provider="kubernetes", limit=2)
    assert len(first["items"]) == 2 and first["has_more"] is True
    rest = q.find("mcit", provider="kubernetes", limit=50, cursor=first["cursor"])
    assert rest["has_more"] is False
    assert len(first["items"]) + len(rest["items"]) == 5  # GW, RT, SVC, DEP, OPI (MS01 retired)
    orphans = q.find("mcit", provider="kubernetes", kind="network", missing_relation="tracked_in")
    assert keys(orphans["items"]) == {GW, RT, SVC}
    assert q.find("mcit", kind="container", missing_relation="tracked_in")["items"] == []  # DEP is tracked


def test_traverse_depth_kinds_and_as_of(q):
    r = q.traverse("mcit", DEP, max_depth=1)
    assert keys(r["nodes"]) == {DEP, SVC, OPI, JIRA}
    assert {n["depth"] for n in r["nodes"] if n["key"] == DEP} == {0}
    only_net = q.traverse("mcit", DEP, max_depth=6, kinds=["network", "dns"])
    assert keys(only_net["nodes"]) == {DEP, SVC, RT, GW, TUN, DNS}
    past = q.traverse("mcit", DEP, max_depth=1, direction="out", as_of="2026-09-20T00:00:00+00:00")
    assert MS01 in keys(past["nodes"]) and OPI not in keys(past["nodes"])


def test_traverse_cycle_terminates_without_duplicates(svc, kg_registry, gstore):
    a = svc.upsert_entity("mcit", "logical", "workload", "wa", display_name="a")["key"]
    b = svc.upsert_entity("mcit", "logical", "workload", "wb", display_name="b")["key"]
    c = svc.upsert_entity("mcit", "logical", "workload", "wc", display_name="c")["key"]
    svc.link("mcit", a, "depends_on", b); svc.link("mcit", b, "depends_on", c); svc.link("mcit", c, "depends_on", a)
    svc.link("mcit", a, "peers_with", b); svc.link("mcit", b, "peers_with", a)
    r = KGQuery(kg_registry, gstore).traverse("mcit", a, max_depth=6)
    assert sorted(n["key"] for n in r["nodes"]) == sorted([a, b, c])
    assert len(r["edges"]) == len({(e["src"], e["relation"], e["dst"]) for e in r["edges"]}) == 5


def test_traverse_truncates_to_budget_valid_json(svc, kg_registry, gstore):
    hub = svc.upsert_entity("mcit", "logical", "workload", "hub", display_name="hub")["key"]
    ents = [{"provider": "logical", "type": "workload", "native_id": f"w{i:03d}", "display_name": f"worker {i:03d} with a long name"} for i in range(300)]
    edges = [{"src": f"logical:w{i:03d}", "relation": "depends_on", "dst": hub} for i in range(300)]
    assert svc.batch("mcit", ents, edges)["status"] == "ok"
    r = KGQuery(kg_registry, gstore).traverse("mcit", hub, max_depth=1, max_chars=2000)
    text = json.dumps(r)
    assert len(text) <= 2000 and json.loads(text)["truncated"] is True
    assert r["nodes"][0]["key"] == hub
    node_keys = keys(r["nodes"])
    assert all(e["src"] in node_keys and e["dst"] in node_keys for e in r["edges"])


def test_path(q):
    r = q.path("mcit", DNS, OPI)
    assert r["found"] and [n["key"] for n in r["paths"][0]["nodes"]] == [DNS, TUN, GW, RT, SVC, DEP, OPI]
    assert len(r["paths"]) <= 3
    assert q.path("mcit", DNS, OPI, max_depth=3)["found"] is False


def test_impact_reaches_public_hostname_and_reports_tickets(q):
    r = q.impact("mcit", OPI)
    affected = {n["key"]: d["depth"] for d in r["affected"] for n in d["nodes"]}
    assert affected[DEP] == 1 and affected[DNS] == 6
    assert keys(r["tracked_in"]) == {JIRA}
    assert q.impact("mcit", MS01)["affected"] == []  # only retired edges point at it


def test_impact_cycle_terminates(svc, kg_registry, gstore):
    a = svc.upsert_entity("mcit", "logical", "workload", "ca", display_name="a")["key"]
    b = svc.upsert_entity("mcit", "logical", "workload", "cb", display_name="b")["key"]
    svc.link("mcit", a, "depends_on", b); svc.link("mcit", b, "depends_on", a)
    r = KGQuery(kg_registry, gstore).impact("mcit", a)
    assert [n["key"] for d in r["affected"] for n in d["nodes"]] == [b]


def test_overview(q):
    g = q.overview("mcit")["graphs"][0]
    assert g["graph"] == "mcit" and g["entities"] == 8
    assert g["by_kind"] == {"dns": 1, "network": 4, "container": 1, "host": 1, "work": 1}
    assert g["by_provider"]["cloudflare"] == 2
    assert g["hubs"][0]["key"] == DEP and g["hubs"][0]["degree"] == 3
    assert DEP in keys(g["recent"]) and g["dangling_xrefs"] == 0


def test_related_across_and_for_memory(svc, q):
    svc.upsert_entity("mcit", "logical", "jira_issue", "MCIT-184")
    svc.upsert_entity("vtv", "logical", "jira_issue", "VTV-238")
    svc.upsert_entity("vtv", "kubernetes", "apps/Deployment", "aks-vtv-prod/cst/apps/Deployment/memory-mcp",
                      display_name="memory-mcp")
    svc.xref("vtv::logical:VTV-238", "pattern_from", "mcit::logical:MCIT-184", "Key Vault CSI pattern")
    r = q.related_across("vtv", "logical:VTV-238")
    assert r["xrefs"][0]["dst"] == "mcit::logical:MCIT-184"
    sim = q.related_across("vtv", "kubernetes:aks-vtv-prod/cst/apps/Deployment/memory-mcp")["similar"]
    assert sim[0]["graph"] == "mcit" and sim[0]["key"] == DEP and sim[0]["memories"] == [MEMS["mem-1"]]
    fm = q.for_memory("mem-2")
    assert fm["entities"] == [] and fm["edges"][0]["dst"] == OPI and fm["edges"][0]["graph"] == "mcit"
    assert keys(q.for_memory("mem-1")["entities"]) == {DEP}


def test_fit_trims_lists_and_stays_valid():
    big = {"items": [{"k": "x" * 50} for _ in range(100)]}
    out = fit(big, 500, ("items",))
    assert out["truncated"] is True and len(json.dumps(out)) <= 500


def test_find_truncation_keeps_paging(svc, kg_registry, gstore):
    """When fit() trims items, has_more and cursor are updated so paging works."""
    # Create ~120 entities with large compact forms
    ents = [{"provider": "logical", "type": "workload", "native_id": f"w{i:04d}",
             "display_name": f"worker {i:04d} with a moderately long description to increase size"}
            for i in range(120)]
    assert svc.batch("mcit", ents, [])["status"] == "ok"

    q = KGQuery(kg_registry, gstore)
    # Find with limit=100 and a budget that will trim some items
    first = q.find("mcit", limit=100)
    assert first["truncated"] is True and first["has_more"] is True
    assert len(first["items"]) < 100

    # Page through remaining items
    seen = {n["key"] for n in first["items"]}
    page_num = 1
    while first["has_more"]:
        first = q.find("mcit", limit=100, cursor=first["cursor"])
        page_num += 1
        new_items = {n["key"] for n in first["items"]}
        assert len(new_items & seen) == 0, "Pages should not overlap"
        seen.update(new_items)

    # Should have retrieved all 120 entities
    assert len(seen) == 120


def test_resolve_exact_key_respects_filters(q):
    """resolve(key, kind=X) filters the result by kind."""
    # DEP is a container; should be found with kind="container"
    result = q.resolve(DEP, kind="container")["results"]
    assert any(r["key"] == DEP for r in result)

    # DEP should NOT be found with kind="host"
    result = q.resolve(DEP, kind="host")["results"]
    assert not any(r["key"] == DEP for r in result)


def test_resolve_exact_alias_and_native_id_rank_first(svc, kg_registry, gstore):
    """Exact match on alias and native_id score 1.0 and rank first."""
    # Create entities with distinct names
    a_key = svc.upsert_entity("mcit", "logical", "workload", "unique-a",
                              display_name="unique-a", aliases=["my-alias"])["key"]
    b_key = svc.upsert_entity("mcit", "logical", "workload", "unique-b",
                              display_name="unique-b", aliases=["my-alias-variant"])["key"]

    q = KGQuery(kg_registry, gstore)

    # Query by alias
    result = q.resolve("my-alias")["results"]
    assert result[0]["key"] == a_key and result[0]["score"] == 1.0

    # Query by native_id
    result = q.resolve("unique-a")["results"]
    assert result[0]["key"] == a_key and result[0]["score"] == 1.0

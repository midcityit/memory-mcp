import json
from urllib.parse import quote

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from qdrant_client import QdrantClient

from memory_mcp.kg.query import KGQuery
from memory_mcp.kg.rest import build_router
from memory_mcp.kg.service import KGService
from memory_mcp.kg.store import QdrantGraphStore

SUB = "/subscriptions/00000000-0000-0000-0000-000000000001"
AKS = f"azure:{SUB}/resourcegroups/rg-aks/providers/microsoft.containerservice/managedclusters/aks-vtv-prod"
DEP = "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"
OPI = "kubernetes:mcit-k8s/_cluster/core/Node/opi-5"
OLD = "kubernetes:ms01-k8s/_cluster/core/Node/ms01-k8s-wkr-01"


def no_auth():
    return None


def make_app(svc, kg_registry, gstore, dep=no_auth):
    app = FastAPI()
    app.include_router(build_router(svc, KGQuery(kg_registry, gstore), dep))
    return app


def client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def seeded(svc):
    svc.upsert_entity("vtv", "azure", "azurerm_kubernetes_cluster", AKS.split(":", 1)[1], display_name="aks-vtv-prod")
    svc.upsert_entity("vtv", "logical", "jira_issue", "VTV-238")
    svc.batch("mcit", [
        {"provider": "kubernetes", "type": "apps/Deployment", "native_id": DEP.split(":", 1)[1], "display_name": "memory-mcp"},
        {"provider": "kubernetes", "type": "core/Node", "native_id": OPI.split(":", 1)[1], "display_name": "opi-5"},
        {"provider": "kubernetes", "type": "core/Node", "native_id": OLD.split(":", 1)[1], "display_name": "ms01-k8s-wkr-01",
         "valid_from": "2026-04-01T00:00:00+00:00", "valid_to": "2026-09-27T07:00:00+00:00"},
        {"provider": "logical", "type": "jira_issue", "native_id": "MCIT-184"},
    ], [
        {"src": DEP, "relation": "runs_on", "dst": OPI},
        {"src": DEP, "relation": "runs_on", "dst": OLD, "valid_from": "2026-04-01T00:00:00+00:00",
         "valid_to": "2026-09-27T07:00:00+00:00", "retire_reason": "MCIT-193"},
    ])
    svc.xref("vtv::logical:VTV-238", "pattern_from", "mcit::logical:MCIT-184", "Key Vault CSI pattern")
    return svc


async def test_graphs_and_overview(seeded, kg_registry, gstore):
    async with client(make_app(seeded, kg_registry, gstore)) as c:
        assert set((await c.get("/kg/graphs")).json()["graphs"]) == {"mcit", "vtv"}
        ov = (await c.get("/kg/overview", params={"graphs": "mcit"})).json()
        assert ov["graphs"][0]["entities"] == 3


async def test_get_entity_with_slashes_in_key(seeded, kg_registry, gstore):
    async with client(make_app(seeded, kg_registry, gstore)) as c:
        raw = await c.get(f"/kg/vtv/entities/{AKS}")
        enc = await c.get(f"/kg/vtv/entities/{quote(AKS, safe='')}")
        k8s = await c.get(f"/kg/mcit/entities/{quote(DEP, safe='')}")
    assert raw.status_code == enc.status_code == k8s.status_code == 200
    assert raw.json()["entity"]["key"] == enc.json()["entity"]["key"] == AKS
    assert k8s.json()["out"]["runs_on"][0]["key"] == OPI


async def test_traverse_path_impact_resolve(seeded, kg_registry, gstore):
    async with client(make_app(seeded, kg_registry, gstore)) as c:
        t = (await c.post("/kg/mcit/traverse", json={"start": DEP, "max_depth": 1, "as_of": "2026-09-20"})).json()
        assert {n["key"] for n in t["nodes"]} == {DEP, OLD}
        p = (await c.post("/kg/mcit/path", json={"src": DEP, "dst": OPI})).json()
        assert p["found"] is True
        i = (await c.post("/kg/mcit/impact", json={"key": OPI})).json()
        assert i["affected"][0]["nodes"][0]["key"] == DEP
        r = (await c.post("/kg/resolve", json={"query": "aks-vtv-prod"})).json()
        assert r["results"][0]["key"] == AKS and r["results"][0]["graph"] == "vtv"


async def test_errors_map_to_http(seeded, kg_registry, gstore):
    async with client(make_app(seeded, kg_registry, gstore)) as c:
        nf = await c.get("/kg/mcit/entities/kubernetes:nope")
        ug = await c.post("/kg/nope/traverse", json={"start": DEP})
    assert nf.status_code == 404 and nf.json()["detail"]["error"] == "not_found"
    assert ug.status_code == 404 and ug.json()["detail"]["error"] == "unknown_graph"


async def test_auth_dependency_applies(seeded, kg_registry, gstore):
    def deny():
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    async with client(make_app(seeded, kg_registry, gstore, dep=deny)) as c:
        assert (await c.get("/kg/graphs")).status_code == 401


async def test_export_import_round_trip(seeded, kg_registry, gstore, stub_embedder):
    async with client(make_app(seeded, kg_registry, gstore)) as c:
        mcit = (await c.get("/kg/mcit/export")).text
        vtv = (await c.get("/kg/vtv/export")).text
    lines = [json.loads(x) for x in mcit.splitlines() if x]
    assert sum(x["record"] == "edge" for x in lines) == 2  # retired edge included
    # import into a brand-new instance
    fresh = QdrantGraphStore(QdrantClient(":memory:"), stub_embedder)
    for g in kg_registry.graphs:
        fresh.ensure_graph(g)
    fresh.ensure_xrefs()
    svc2 = KGService(kg_registry, fresh)
    async with client(make_app(svc2, kg_registry, fresh)) as c:
        r1 = (await c.post("/kg/mcit/import", content=mcit)).json()
        r2 = (await c.post("/kg/vtv/import", content=vtv)).json()
        again = (await c.post("/kg/mcit/import", content=mcit)).json()
    assert r1["status"] == "ok" and r2["status"] == "ok"
    assert r2["xrefs"] == {"imported": 1, "errors": []}
    assert {e.key for e in fresh.iter_entities("mcit")} == {e.key for e in gstore.iter_entities("mcit")}
    assert len(list(fresh.iter_edges("mcit"))) == 2
    assert fresh.get_entity("mcit", OLD).valid_to == "2026-09-27T07:00:00+00:00"
    assert all(w["status"] in ("updated", "retired", "recorded") for w in again["written"])


async def test_hard_delete_requires_flag_and_marks_xrefs(seeded, kg_registry, gstore):
    async with client(make_app(seeded, kg_registry, gstore)) as c:
        soft = await c.delete("/kg/mcit/entities/logical:MCIT-184")
        hard = await c.delete("/kg/mcit/entities/logical:MCIT-184", params={"hard": "true"})
    assert soft.status_code == 400
    assert hard.status_code == 200 and hard.json()["deleted"] == "logical:MCIT-184"
    assert gstore.xrefs_touching("mcit::logical:MCIT-184")[0].dangling is True

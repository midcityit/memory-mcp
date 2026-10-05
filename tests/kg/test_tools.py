import asyncio
import json

import pytest
from mcp.server.mcpserver import MCPServer

from memory_mcp.kg.query import KGQuery
from memory_mcp.kg.tools import register

TOOLS = {"kg_upsert_entity", "kg_link", "kg_unlink", "kg_retire_entity", "kg_delete_entity", "kg_delete_xref",
         "kg_catalog", "kg_batch", "kg_xref",
         "kg_resolve", "kg_get_entity", "kg_find", "kg_traverse", "kg_path", "kg_impact", "kg_overview",
         "kg_related_across", "kg_for_memory"}


@pytest.fixture
def server(svc, kg_registry, gstore):
    s = MCPServer("test")
    register(s, svc, KGQuery(kg_registry, gstore))
    return s


def call(s, name, **args):
    r = asyncio.run(s.call_tool(name, args))
    return json.loads(r.content[0].text)


def test_registers_exactly_18_tools(server):
    assert {t.name for t in asyncio.run(server.list_tools())} == TOOLS


def test_delete_and_catalog_tools(server):
    # catalog surfaces providers/types without guessing
    cat = call(server, "kg_catalog", provider="logical", search="principal")
    assert [t["type"] for t in cat["types"]["logical"]] == ["service_principal"]
    # hard delete requires confirm, then purges
    call(server, "kg_upsert_entity", graph="mcit", provider="logical", type="jira_issue", native_id="MCIT-7")
    guard = call(server, "kg_delete_entity", graph="mcit", key="logical:MCIT-7")
    assert guard["error"] == "confirm_required"
    done = call(server, "kg_delete_entity", graph="mcit", key="logical:MCIT-7", confirm=True)
    assert done["status"] == "deleted"
    # xref delete
    call(server, "kg_upsert_entity", graph="mcit", provider="logical", type="jira_issue", native_id="MCIT-8")
    call(server, "kg_upsert_entity", graph="vtv", provider="logical", type="jira_issue", native_id="VTV-8")
    call(server, "kg_xref", src="vtv::logical:VTV-8", relation="pattern_from", dst="mcit::logical:MCIT-8", note="p")
    assert call(server, "kg_delete_xref", src="vtv::logical:VTV-8", relation="pattern_from",
                dst="mcit::logical:MCIT-8")["status"] == "deleted"


def test_write_then_read_round_trip(server):
    d = call(server, "kg_upsert_entity", graph="mcit", provider="kubernetes", type="kubernetes_deployment_v1",
             native_id="mcit-k8s/digital-twin/apps/Deployment/memory-mcp", display_name="memory-mcp")
    n = call(server, "kg_upsert_entity", graph="mcit", provider="kubernetes", type="core/Node",
             native_id="mcit-k8s/_cluster/core/Node/opi-5", display_name="opi-5")
    assert call(server, "kg_link", graph="mcit", src=d["key"], relation="runs_on", dst=n["key"])["status"] == "created"
    t = call(server, "kg_traverse", graph="mcit", start=d["key"], max_depth=1)
    assert {x["key"] for x in t["nodes"]} == {d["key"], n["key"]}
    assert call(server, "kg_resolve", query="opi-5", graphs=["mcit"])["results"][0]["key"] == n["key"]
    assert call(server, "kg_impact", graph="mcit", key=n["key"])["affected"][0]["nodes"][0]["key"] == d["key"]


def test_errors_are_returned_not_raised(server):
    r = call(server, "kg_upsert_entity", graph="mcit", provider="kubernetes", type="apps/Deploymnet",
             native_id="c/n/apps/Deployment/x")
    assert r["error"] == "unknown_type" and "apps/Deployment" in r["suggestions"]
    assert call(server, "kg_get_entity", graph="mcit", key="kubernetes:none")["error"] == "not_found"


def test_batch_and_overview(server):
    r = call(server, "kg_batch", graph="vtv", entities=[
        {"provider": "logical", "type": "jira_issue", "native_id": "VTV-238"}], edges=[])
    assert r["status"] == "ok"
    ov = call(server, "kg_overview", graphs="vtv")
    assert ov["graphs"][0]["entities"] == 1

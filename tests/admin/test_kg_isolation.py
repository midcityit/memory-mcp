"""Knowledge-graph authorization (RCL-73 task 7): every KG call is authorized per graph.

A graph id is a tenant id. Cases run over real MCP tool calls and over the /kg REST routes
of the full app, with tenants `alpha` (bootstrap) and `beta`.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from qdrant_client import QdrantClient

from memory_mcp import mcp_tools
from memory_mcp.admin.authz import READ_ACTIONS, WRITE_ACTIONS
from memory_mcp.admin.keys import generate_key
from memory_mcp.admin.models import ApiKey, RoleMapping
from memory_mcp.admin.store import InMemoryAdminStore
from memory_mcp.kg.rest import ROUTE_ACTION, build_router
from memory_mcp.store import COLLECTION, MemoryRecord, MemoryStore
from tests.admin.test_memory_isolation import (
    ACCEPT, G_EDIT, G_VIEW, LEGACY, T0, RecordingAudit, _cfg, ui_headers,
)
from tests.kg.conftest import StubEmbedder

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect")

_PKG_CONFIG = Path(__file__).resolve().parents[2] / "src" / "memory_mcp" / "kg" / "config"
A1, A2 = "logical:ALPHA-1", "logical:ALPHA-2"
B1, B2 = "logical:BETA-1", "logical:BETA-2"
XREF = {"src": f"alpha::{A1}", "relation": "pattern_from", "dst": f"beta::{B1}"}
NO_ACCESS = "No access to tenant"


# ── app under test ──────────────────────────────────────────────────────────────
@dataclass
class Env:
    app: Any
    admin: InMemoryAdminStore | None
    mstore: MemoryStore
    audit: RecordingAudit
    client: AsyncClient = None

    def key(self, tenants=("alpha",), access="write", denied=(), agent="agent-a") -> str:
        full, key_id, secret_hash = generate_key()
        self.admin.insert_key(ApiKey(
            id=key_id, agent=agent, secret_hash=secret_hash, tenants=tuple(tenants), access=access,
            denied_tools=tuple(denied), source_repo_patterns=(), expires_at=None,
            created_at=T0, created_by="admin", revoked_at=None, last_used_at=None, rotated_from=None,
        ))
        return full

    def memory(self, tenant: str, name: str, type: str = "runbook") -> str:
        coll = COLLECTION if tenant == "alpha" else f"memories_{tenant}"
        now = MemoryStore.now_iso()
        rid = MemoryStore.new_id()
        self.mstore.upsert(MemoryRecord(id=rid, type=type, name=name, content=f"{name} content",
                                        source_repo="global", agent="seed", tags=[],
                                        created_at=now, updated_at=now), collection=coll)
        return rid


def _config_dir(tmp: Path, graphs: str) -> str:
    """KG config with only the given graphs; relations and type catalogs are the packaged ones."""
    shutil.copy(_PKG_CONFIG / "kg_relations.yaml", tmp / "kg_relations.yaml")
    (tmp / "kg_graphs.yaml").write_text(graphs)
    return str(tmp)


def _build(tmp: Path, multitenant: bool = True) -> Env:
    from memory_mcp.server import create_app
    qc = QdrantClient(":memory:")
    mstore = MemoryStore("unused", embedder=StubEmbedder(), client=qc)
    admin = InMemoryAdminStore() if multitenant else None
    audit = RecordingAudit()
    if multitenant:
        cfg = _cfg(kg_enabled=True, kg_config_dir=_config_dir(tmp, "graphs: {}\n"))
    else:
        cfg = _cfg(ui_token=None, kg_enabled=True, kg_config_dir=_config_dir(
            tmp, "graphs:\n  alpha: {name: Alpha}\n  beta: {name: Beta}\n"))
    with patch("memory_mcp.server.load_config", return_value=cfg), \
         patch("memory_mcp.server.NullAudit", return_value=audit), \
         patch("memory_mcp.server.Audit", return_value=audit):
        app = create_app(admin_store=admin, memory_store=mstore)
    if multitenant:
        app.state.tenants.create("beta", "Beta", "", by="test")
        admin.replace_mappings([RoleMapping(G_VIEW, "alpha", "viewer"),
                                RoleMapping(G_EDIT, "alpha", "editor")], by="test")
    return Env(app, admin, mstore, audit)


async def _serving(e: Env):
    ready, stop = asyncio.Event(), asyncio.Event()

    async def run():
        async with mcp_tools.mcp.session_manager.run():
            ready.set()
            await stop.wait()

    task = asyncio.create_task(run())
    await ready.wait()
    async with AsyncClient(transport=ASGITransport(app=e.app), base_url="http://localhost") as c:
        e.client = c
        yield e
    stop.set()
    await task


@pytest.fixture
async def env(tmp_path):
    async for e in _serving(_build(tmp_path)):
        yield e


@pytest.fixture
async def legacy_env(tmp_path):
    async for e in _serving(_build(tmp_path, multitenant=False)):
        yield e


# ── calls ───────────────────────────────────────────────────────────────────────
@dataclass
class Result:
    ok: bool
    data: Any = None
    message: str = ""
    status: int | None = None  # REST only
    code: str | None = None  # REST only


def _auth(who) -> dict:
    return dict(who) if isinstance(who, dict) else {"Authorization": f"Bearer {who}"}


async def mcp(e: Env, who, tool: str, **args) -> Result:
    h = _auth(who) | {"Accept": ACCEPT, "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
    resp = await e.client.post("/mcp/", headers=h, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    assert resp.status_code == 200, resp.text
    if resp.headers["content-type"].startswith("text/event-stream"):
        data = [ln[5:].strip() for ln in resp.text.splitlines() if ln.startswith("data:")]
        body = json.loads(data[-1])
    else:
        body = resp.json()
    assert "error" not in body, body
    result = body["result"]
    if result.get("isError"):
        return Result(False, message=result["content"][0]["text"])
    return Result(True, data=json.loads(result["content"][0]["text"]))


async def rest(e: Env, who, method: str, path: str, params=None, body=None, content=None) -> Result:
    resp = await e.client.request(method, path, headers=_auth(who), params=params, json=body, content=content)
    if resp.status_code >= 400:
        j = resp.json()
        detail = j.get("detail")
        return Result(False, message=detail if isinstance(detail, str) else json.dumps(detail),
                      status=resp.status_code, code=j.get("code"))
    ctype = resp.headers.get("content-type", "")
    data = resp.text if "ndjson" in ctype else resp.json()
    return Result(True, data=data, status=resp.status_code)


def ok(r: Result) -> Any:
    assert r.ok, r
    assert not (isinstance(r.data, dict) and "error" in r.data), r.data
    return r.data


def denied(r: Result, code: str, fragment: str) -> None:
    assert not r.ok, r
    assert fragment in r.message, r.message
    if r.status is not None:
        assert (r.status, r.code) == (403, code), r


async def upsert(e: Env, who, graph: str, native_id: str, **kw) -> Any:
    return ok(await mcp(e, who, "kg_upsert_entity", graph=graph, provider="logical", type="jira_issue",
                        native_id=native_id, **kw))


async def seed_xref(e: Env) -> str:
    """alpha::ALPHA-1 -pattern_from-> beta::BETA-1, written by a key holding both; returns that key."""
    both = e.key(tenants=("alpha", "beta"))
    await upsert(e, both, "alpha", "ALPHA-1", display_name="shared widget")
    await upsert(e, both, "beta", "BETA-1", display_name="shared widget")
    ok(await mcp(e, both, "kg_xref", note="reuse", **XREF))
    return both


# ── every graph-taking tool and route refuses an unheld graph ──────────────────
def tool_cases(g: str) -> dict[str, dict]:
    """Arguments naming graph `g` for every graph-taking kg tool."""
    return {
        "kg_upsert_entity": dict(graph=g, provider="logical", type="jira_issue", native_id="X-1"),
        "kg_link": dict(graph=g, src="a", relation="depends_on", dst="b"),
        "kg_unlink": dict(graph=g, src="a", relation="depends_on", dst="b", reason="r"),
        "kg_retire_entity": dict(graph=g, key="k", reason="r"),
        "kg_delete_entity": dict(graph=g, key="k", confirm=True),
        "kg_delete_xref": dict(src=f"alpha::{A1}", relation="pattern_from", dst=f"{g}::k"),
        "kg_batch": dict(graph=g, entities=[{"provider": "logical", "type": "jira_issue", "native_id": "X-2"}]),
        "kg_xref": dict(src=f"alpha::{A1}", relation="pattern_from", dst=f"{g}::k", note="n"),
        "kg_resolve": dict(query="widget", graphs=g),
        "kg_get_entity": dict(graph=g, key="k"),
        "kg_find": dict(graph=g),
        "kg_traverse": dict(graph=g, start="k"),
        "kg_path": dict(graph=g, src="a", dst="b"),
        "kg_impact": dict(graph=g, key="k"),
        "kg_overview": dict(graphs=[g]),
        "kg_related_across": dict(graph=g, key="k"),
        "kg_for_memory": dict(memory_id="m", graphs=g),
    }


GRAPH_TOOLS = sorted(tool_cases("x"))
EXTRA_TOOL_CASES = {  # second way to name a foreign graph
    "kg_related_across(target_graphs)": ("kg_related_across", lambda g: dict(graph="alpha", key=A1,
                                                                             target_graphs=[g])),
    "kg_xref(src)": ("kg_xref", lambda g: dict(src=f"{g}::k", relation="pattern_from", dst=f"alpha::{A1}",
                                               note="n")),
}


def route_cases(g: str) -> dict[tuple[str, str], dict]:
    """A request per /kg route; graph-taking routes name `g`."""
    return {
        ("GET", "/kg/graphs"): dict(path="/kg/graphs"),
        ("GET", "/kg/catalog"): dict(path="/kg/catalog"),
        ("GET", "/kg/overview"): dict(path="/kg/overview", params={"graphs": g}),
        ("POST", "/kg/resolve"): dict(path="/kg/resolve", body={"query": "widget", "graphs": [g]}),
        ("GET", "/kg/{graph}/entities/{key:path}"): dict(path=f"/kg/{g}/entities/k"),
        ("POST", "/kg/{graph}/traverse"): dict(path=f"/kg/{g}/traverse", body={"start": "k"}),
        ("POST", "/kg/{graph}/path"): dict(path=f"/kg/{g}/path", body={"src": "a", "dst": "b"}),
        ("POST", "/kg/{graph}/impact"): dict(path=f"/kg/{g}/impact", body={"key": "k"}),
        ("GET", "/kg/{graph}/export"): dict(path=f"/kg/{g}/export"),
        ("POST", "/kg/{graph}/import"): dict(path=f"/kg/{g}/import", content=b""),
        ("DELETE", "/kg/{graph}/entities/{key:path}"): dict(path=f"/kg/{g}/entities/k", params={"hard": "true"}),
        ("DELETE", "/kg/xref"): dict(path="/kg/xref", params={"src": f"alpha::{A1}", "relation": "pattern_from",
                                                               "dst": f"{g}::k"}),
    }


GRAPH_ROUTES = [r for r in ROUTE_ACTION if r not in {("GET", "/kg/graphs"), ("GET", "/kg/catalog")}]


async def call_route(e: Env, who, route: tuple[str, str], g: str) -> Result:
    case = route_cases(g)[route]
    return await rest(e, who, route[0], case["path"], params=case.get("params"), body=case.get("body"),
                      content=case.get("content"))


@pytest.mark.parametrize("tool", GRAPH_TOOLS + sorted(EXTRA_TOOL_CASES))
async def test_mcp_alpha_key_refused_on_beta_and_unknown_graph_alike(env, tool):
    k = env.key()
    name, make = EXTRA_TOOL_CASES.get(tool) or (tool, lambda g: tool_cases(g)[tool])
    beta = await mcp(env, k, name, **make("beta"))
    gamma = await mcp(env, k, name, **make("gamma"))
    denied(beta, "tenant_forbidden", f"{NO_ACCESS} 'beta'")
    denied(gamma, "tenant_forbidden", f"{NO_ACCESS} 'gamma'")
    assert beta.message.replace("beta", "X") == gamma.message.replace("gamma", "X")


@pytest.mark.parametrize("route", GRAPH_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
async def test_rest_alpha_key_refused_on_beta_and_unknown_graph_alike(env, route):
    k = env.key()
    beta = await call_route(env, k, route, "beta")
    gamma = await call_route(env, k, route, "gamma")
    denied(beta, "tenant_forbidden", f"{NO_ACCESS} 'beta'")
    denied(gamma, "tenant_forbidden", f"{NO_ACCESS} 'gamma'")
    assert beta.message.replace("beta", "X") == gamma.message.replace("gamma", "X")


async def dump(e: Env, who, graph: str) -> dict[str, list]:
    """A graph's export grouped by record type (entities, edges, xrefs it is the source of)."""
    out = {"entity": [], "edge": [], "xref": []}
    for ln in ok(await rest(e, who, "GET", f"/kg/{graph}/export")).splitlines():
        rec = json.loads(ln)
        out[rec["record"]].append(rec)
    return out


def ndjson(*records: dict) -> bytes:
    return "\n".join(json.dumps(r) for r in records).encode()


async def test_refusals_write_nothing_into_beta(env):
    both = env.key(tenants=("alpha", "beta"))
    await upsert(env, both, "alpha", "ALPHA-1")
    await upsert(env, both, "beta", "BETA-1")
    await upsert(env, both, "beta", "BETA-2")
    before = await dump(env, both, "beta")
    k = env.key()
    for tool, args in tool_cases("beta").items():
        await mcp(env, k, tool, **args)
    for route in GRAPH_ROUTES:
        await call_route(env, k, route, "beta")
    # The same refusals with real endpoints, so only authorization stands in the way.
    denied(await mcp(env, k, "kg_link", graph="beta", src=B1, relation="depends_on", dst=B2),
           "tenant_forbidden", NO_ACCESS)
    denied(await mcp(env, k, "kg_xref", src=f"alpha::{A1}", relation="pattern_from", dst=f"beta::{B1}", note="n"),
           "tenant_forbidden", NO_ACCESS)
    denied(await rest(env, k, "POST", "/kg/alpha/import", content=ndjson(
        {"record": "xref", "src": f"alpha::{A1}", "relation": "pattern_from", "dst": f"beta::{B1}", "note": "n"})),
        "tenant_forbidden", NO_ACCESS)
    denied(await rest(env, k, "POST", "/kg/beta/import", content=ndjson(
        {"record": "edge", "src": B1, "relation": "depends_on", "dst": B2})), "tenant_forbidden", NO_ACCESS)
    after = await dump(env, both, "beta")
    assert after == before and after["edge"] == [] and after["xref"] == []
    assert (await dump(env, both, "alpha"))["xref"] == []
    assert ok(await mcp(env, both, "kg_get_entity", graph="beta", key=B1))["xrefs"] == []


async def test_listed_graph_that_is_not_a_tenant_is_refused_like_an_unheld_one(env):
    k = env.key(tenants=("alpha", "alpah"))
    for tool in ("kg_find", "kg_upsert_entity"):
        args = tool_cases("alpah")[tool]
        typo = await mcp(env, k, tool, **args)
        unheld = await mcp(env, k, tool, **tool_cases("gamma")[tool])
        denied(typo, "tenant_forbidden", f"{NO_ACCESS} 'alpah'")
        assert typo.message.replace("alpah", "X") == unheld.message.replace("gamma", "X")
        assert "suggestions" not in typo.message and "'alpha'" not in typo.message
    typo = await env.client.get("/kg/alpah/entities/k", headers=_auth(k))
    unheld = await env.client.get("/kg/gamma/entities/k", headers=_auth(k))
    assert typo.status_code == unheld.status_code == 403
    assert typo.json() == {"detail": f"{NO_ACCESS} 'alpah'", "code": "tenant_forbidden"}
    assert unheld.json() == {"detail": f"{NO_ACCESS} 'gamma'", "code": "tenant_forbidden"}


async def test_suggestions_never_name_graphs_outside_the_visible_set(env):
    from memory_mcp.admin.audit import NullAudit
    from memory_mcp.admin.models import Principal
    from memory_mcp.kg.authz import KGGuard
    guard = KGGuard(env.app.state.tenants, NullAudit())
    p = Principal(kind="key", id="k", agent="a", roles={"beta": "editor"})
    err = {"error": "unknown_graph", "suggestions": ["alpha", "beta", "kg_resolve to find the right key"],
           "errors": [{"suggestions": ["alpha"]}]}
    assert guard.scrub(p, err) == {"error": "unknown_graph",
                                   "suggestions": ["beta", "kg_resolve to find the right key"],
                                   "errors": [{"suggestions": []}]}


async def test_xref_key_that_is_not_fully_qualified_is_400(env):
    k = env.key()
    await upsert(env, k, "alpha", "ALPHA-1")
    r = await mcp(env, k, "kg_xref", src=A1, relation="pattern_from", dst=f"alpha::{A1}", note="n")
    assert not r.ok and "is not a fully qualified key" in r.message
    r = await rest(env, k, "DELETE", "/kg/xref", params={"src": f"alpha::{A1}", "relation": "pattern_from",
                                                          "dst": "nope"})
    assert (r.status, r.code) == (400, "invalid_key")


# ── coverage: every route and tool is guarded by its mapped action ──────────────
def _kg_routes() -> set[tuple[str, str]]:
    """Every (method, path) of the router create_app mounts at /kg (building it checks ROUTE_ACTION too)."""
    router = build_router(SimpleNamespace(store=None), None, lambda: None)
    return {(m, r.path) for r in router.routes for m in r.methods if m != "HEAD"}


async def test_every_route_and_tool_has_a_guard_mapping(env):
    routes = _kg_routes()
    mounted = {(m.upper(), path) for path, ops in env.app.openapi()["paths"].items() if path.startswith("/kg")
               for m in ops}
    assert len(mounted) == len(routes)  # the app serves exactly this router's routes
    assert routes == set(ROUTE_ACTION), routes ^ set(ROUTE_ACTION)
    assert set(route_cases("alpha")) == routes
    assert set(ROUTE_ACTION.values()) <= READ_ACTIONS | WRITE_ACTIONS
    tools = {t.name for t in await mcp_tools.mcp.list_tools() if t.name.startswith("kg_")}
    assert tools == {t for t in READ_ACTIONS | WRITE_ACTIONS if t.startswith("kg_")}
    assert tools == set(GRAPH_TOOLS) | {"kg_catalog"}


@pytest.mark.parametrize("tool", GRAPH_TOOLS + ["kg_catalog"])
async def test_every_tool_checks_its_own_action(env, tool):
    k = env.key(denied=(tool,))
    args = tool_cases("alpha").get(tool, {})
    denied(await mcp(env, k, tool, **args), "tool_denied", f"{tool} is denied")


@pytest.mark.parametrize("route", sorted(ROUTE_ACTION), ids=lambda r: f"{r[0]} {r[1]}")
async def test_every_route_checks_its_mapped_action(env, route):
    action = ROUTE_ACTION[route]
    k = env.key(denied=(action,))
    denied(await call_route(env, k, route, "alpha"), "tool_denied", f"{action} is denied")


# ── multi-graph reads list only what the caller holds ───────────────────────────
async def test_wildcard_reads_return_only_alpha(env):
    await seed_xref(env)
    k = env.key()
    hits = ok(await mcp(env, k, "kg_resolve", query="shared widget"))["results"]
    assert {h["graph"] for h in hits} == {"alpha"}
    assert {g["graph"] for g in ok(await mcp(env, k, "kg_overview"))["graphs"]} == {"alpha"}
    assert ok(await mcp(env, k, "kg_catalog"))["graphs"] == ["alpha"]
    assert set(ok(await rest(env, k, "GET", "/kg/graphs"))["graphs"]) == {"alpha"}
    assert ok(await rest(env, k, "GET", "/kg/catalog"))["graphs"] == ["alpha"]
    hits = ok(await rest(env, k, "POST", "/kg/resolve", body={"query": "shared widget"}))["results"]
    assert {h["graph"] for h in hits} == {"alpha"}
    assert {g["graph"] for g in ok(await rest(env, k, "GET", "/kg/overview"))["graphs"]} == {"alpha"}


async def test_key_holding_both_sees_both(env):
    both = await seed_xref(env)
    hits = ok(await mcp(env, both, "kg_resolve", query="shared widget"))["results"]
    assert {h["graph"] for h in hits} == {"alpha", "beta"}
    assert ok(await mcp(env, both, "kg_catalog"))["graphs"] == ["alpha", "beta"]
    assert set(ok(await rest(env, both, "GET", "/kg/graphs"))["graphs"]) == {"alpha", "beta"}


async def test_related_across_searches_only_visible_targets(env):
    await seed_xref(env)
    k = env.key()
    await upsert(env, k, "alpha", "ALPHA-9", display_name="shared widget")
    r = ok(await mcp(env, k, "kg_related_across", graph="alpha", key="logical:ALPHA-9"))
    assert r["similar"] == [] and r["xrefs"] == []


# ── cross-graph references ──────────────────────────────────────────────────────
async def test_xref_to_unheld_graph_is_invisible_in_every_read(env):
    both = await seed_xref(env)
    k = env.key()
    for who, expect in ((k, 0), (both, 1)):
        ent = ok(await mcp(env, who, "kg_get_entity", graph="alpha", key=A1))
        assert len(ent["xrefs"]) == expect
        trav = ok(await mcp(env, who, "kg_traverse", graph="alpha", start=A1, follow_xrefs=True))
        assert len(trav["xrefs"]) == expect
        assert {n["key"] for n in trav["nodes"]} == {A1}
        rel = ok(await mcp(env, who, "kg_related_across", graph="alpha", key=A1))
        assert len(rel["xrefs"]) == expect
        ent = ok(await rest(env, who, "GET", f"/kg/alpha/entities/{A1}"))
        assert len(ent["xrefs"]) == expect
        trav = ok(await rest(env, who, "POST", "/kg/alpha/traverse", body={"start": A1, "follow_xrefs": True}))
        assert len(trav["xrefs"]) == expect
        lines = [json.loads(ln) for ln in ok(await rest(env, who, "GET", "/kg/alpha/export")).splitlines()]
        assert sum(1 for ln in lines if ln["record"] == "xref") == expect
    assert "beta" not in json.dumps(ok(await mcp(env, k, "kg_get_entity", graph="alpha", key=A1)))


async def test_dangling_counts_are_computed_after_filtering(env):
    both = await seed_xref(env)
    await upsert(env, both, "alpha", "ALPHA-2")
    await upsert(env, both, "beta", "BETA-2")
    ok(await mcp(env, both, "kg_xref", src=f"alpha::{A2}", relation="pattern_from", dst=f"beta::{B2}", note="n"))
    beta_only = env.key(tenants=("beta",))
    # A beta-only key may not delete an xref touching alpha: the entity goes, the xref is kept dangling.
    gone = ok(await mcp(env, beta_only, "kg_delete_entity", graph="beta", key=B2, confirm=True))
    assert gone["xrefs_removed"] == 0
    k = env.key()

    def dangling(r, graph):
        return next(g["dangling_xrefs"] for g in r["graphs"] if g["graph"] == graph)
    assert dangling(ok(await mcp(env, k, "kg_overview")), "alpha") == 0
    assert dangling(ok(await rest(env, k, "GET", "/kg/overview")), "alpha") == 0
    assert dangling(ok(await mcp(env, beta_only, "kg_overview")), "beta") == 0
    assert dangling(ok(await mcp(env, both, "kg_overview")), "alpha") == 1
    assert len(ok(await mcp(env, both, "kg_get_entity", graph="alpha", key=A2))["xrefs"]) == 1


async def test_hard_delete_by_key_holding_both_removes_xref(env):
    both = await seed_xref(env)
    gone = ok(await mcp(env, both, "kg_delete_entity", graph="beta", key=B1, confirm=True))
    assert gone["xrefs_removed"] == 1
    assert ok(await mcp(env, both, "kg_get_entity", graph="alpha", key=A1))["xrefs"] == []


async def test_alpha_key_cannot_create_or_delete_xref_touching_beta(env):
    both = await seed_xref(env)
    k = env.key()
    await upsert(env, k, "alpha", "ALPHA-3")
    denied(await mcp(env, k, "kg_xref", src=f"alpha::logical:ALPHA-3", relation="pattern_from",
                     dst=f"beta::{B1}", note="n"), "tenant_forbidden", f"{NO_ACCESS} 'beta'")
    denied(await mcp(env, k, "kg_delete_xref", **XREF), "tenant_forbidden", f"{NO_ACCESS} 'beta'")
    denied(await rest(env, k, "DELETE", "/kg/xref", params=XREF), "tenant_forbidden", f"{NO_ACCESS} 'beta'")
    line = json.dumps({"record": "xref", "src": f"alpha::{A1}", "relation": "similar_to",
                       "dst": f"beta::{B1}", "note": "n"})
    denied(await rest(env, k, "POST", "/kg/alpha/import", content=line.encode()),
           "tenant_forbidden", f"{NO_ACCESS} 'beta'")
    assert len(ok(await mcp(env, both, "kg_get_entity", graph="alpha", key=A1))["xrefs"]) == 1


async def test_read_only_key_with_both_graphs_cannot_write_xref(env):
    await seed_xref(env)
    ro = env.key(tenants=("alpha", "beta"), access="read")
    denied(await mcp(env, ro, "kg_delete_xref", **XREF), "key_read_only", "read-only")
    assert len(ok(await mcp(env, ro, "kg_get_entity", graph="alpha", key=A1))["xrefs"]) == 1


# ── access level, denied tools, disabled tenants, UI roles ─────────────────────
@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_read_only_key_reads_but_cannot_write(env, transport):
    rw = env.key()
    await upsert(env, rw, "alpha", "ALPHA-1")
    ro = env.key(access="read")
    if transport == "mcp":
        assert ok(await mcp(env, ro, "kg_get_entity", graph="alpha", key=A1))["entity"]["key"] == A1
        denied(await mcp(env, ro, "kg_upsert_entity", graph="alpha", provider="logical", type="jira_issue",
                         native_id="ALPHA-5"), "key_read_only", "read-only")
    else:
        assert ok(await rest(env, ro, "GET", f"/kg/alpha/entities/{A1}"))["entity"]["key"] == A1
        denied(await rest(env, ro, "DELETE", f"/kg/alpha/entities/{A1}", params={"hard": "true"}),
               "key_read_only", "read-only")


@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_denied_delete_entity_is_403(env, transport):
    k = env.key(denied=("kg_delete_entity",))
    await upsert(env, k, "alpha", "ALPHA-1")
    if transport == "mcp":
        r = await mcp(env, k, "kg_delete_entity", graph="alpha", key=A1, confirm=True)
    else:
        r = await rest(env, k, "DELETE", f"/kg/alpha/entities/{A1}", params={"hard": "true"})
    denied(r, "tool_denied", "kg_delete_entity is denied")
    assert ok(await mcp(env, k, "kg_get_entity", graph="alpha", key=A1))["entity"]["key"] == A1


async def test_disabled_tenant_refuses_everything(env):
    both = await seed_xref(env)
    env.app.state.tenants.set_status("beta", "disabled")
    denied(await mcp(env, both, "kg_get_entity", graph="beta", key=B1), "tenant_disabled", "disabled")
    denied(await mcp(env, both, "kg_upsert_entity", graph="beta", provider="logical", type="jira_issue",
                     native_id="BETA-7"), "tenant_disabled", "disabled")
    denied(await rest(env, both, "GET", f"/kg/beta/entities/{B1}"), "tenant_disabled", "disabled")
    denied(await mcp(env, both, "kg_resolve", query="widget", graphs=["beta"]), "tenant_disabled", "disabled")
    assert {g["graph"] for g in ok(await mcp(env, both, "kg_overview"))["graphs"]} == {"alpha"}
    assert ok(await mcp(env, both, "kg_catalog"))["graphs"] == ["alpha"]
    assert ok(await mcp(env, both, "kg_get_entity", graph="alpha", key=A1))["xrefs"] == []
    # A key not holding the disabled tenant still sees "no access", not "disabled".
    denied(await mcp(env, env.key(), "kg_find", graph="beta"), "tenant_forbidden", f"{NO_ACCESS} 'beta'")


@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_viewer_reads_editor_writes(env, transport):
    viewer, editor = ui_headers(groups=(G_VIEW,)), ui_headers(groups=(G_EDIT,))
    await upsert(env, editor, "alpha", "ALPHA-1")
    if transport == "mcp":
        assert ok(await mcp(env, viewer, "kg_get_entity", graph="alpha", key=A1))["entity"]["key"] == A1
        denied(await mcp(env, viewer, "kg_retire_entity", graph="alpha", key=A1, reason="r"),
               "role_insufficient", "Role 'viewer'")
        assert ok(await mcp(env, editor, "kg_retire_entity", graph="alpha", key=A1, reason="r"))["status"] == "retired"
    else:
        assert ok(await rest(env, viewer, "GET", f"/kg/alpha/entities/{A1}"))["entity"]["key"] == A1
        denied(await rest(env, viewer, "DELETE", f"/kg/alpha/entities/{A1}", params={"hard": "true"}),
               "role_insufficient", "Role 'viewer'")
        assert ok(await rest(env, editor, "DELETE", f"/kg/alpha/entities/{A1}",
                             params={"hard": "true"}))["status"] == "deleted"
    denied(await mcp(env, editor, "kg_find", graph="beta"), "tenant_forbidden", f"{NO_ACCESS} 'beta'")


async def test_tenant_created_at_runtime_is_a_graph(env):
    env.app.state.tenants.create("delta", "Delta", "", by="test")
    k = env.key(tenants=("delta",))
    assert (await upsert(env, k, "delta", "DELTA-1"))["status"] == "created"
    assert set(ok(await rest(env, k, "GET", "/kg/graphs"))["graphs"]) == {"delta"}


# ── memories linked from the graph stay in their tenant ─────────────────────────
async def test_for_memory_does_not_reach_beta(env):
    beta_mem = env.memory("beta", "beta runbook")
    both = env.key(tenants=("alpha", "beta"))
    await upsert(env, both, "beta", "BETA-1", memory_ids=[beta_mem])
    k = env.key()
    assert ok(await mcp(env, k, "kg_for_memory", memory_id=beta_mem)) == {"entities": [], "edges": []}
    denied(await mcp(env, k, "kg_for_memory", memory_id=beta_mem, graphs=["beta"]),
           "tenant_forbidden", f"{NO_ACCESS} 'beta'")
    assert [e["key"] for e in ok(await mcp(env, both, "kg_for_memory", memory_id=beta_mem))["entities"]] == [B1]


async def test_linked_memory_names_come_only_from_the_entity_tenant(env):
    alpha_mem = env.memory("alpha", "alpha runbook")
    beta_mem = env.memory("beta", "beta runbook")
    both = env.key(tenants=("alpha", "beta"))
    await upsert(env, both, "alpha", "ALPHA-1", memory_ids=[alpha_mem, beta_mem])
    for who in (env.key(), both):
        ent = ok(await mcp(env, who, "kg_get_entity", graph="alpha", key=A1))
        assert [m["name"] for m in ent["memories"]] == ["alpha runbook"]
        ent = ok(await rest(env, who, "GET", f"/kg/alpha/entities/{A1}"))
        assert [m["name"] for m in ent["memories"]] == ["alpha runbook"]


async def test_related_across_memories_come_from_the_similar_entity_tenant(env):
    beta_mem = env.memory("beta", "beta lesson")
    both = env.key(tenants=("alpha", "beta"))
    await upsert(env, both, "alpha", "ALPHA-1", display_name="shared widget")
    await upsert(env, both, "beta", "BETA-1", display_name="shared widget", memory_ids=[beta_mem])
    rel = ok(await mcp(env, both, "kg_related_across", graph="alpha", key=A1))
    assert [(s["graph"], [m["name"] for m in s["memories"]]) for s in rel["similar"]] == [("beta", ["beta lesson"])]


# ── audit ───────────────────────────────────────────────────────────────────────
async def test_mutations_and_refusals_are_audited(env):
    k = env.key()
    key_id = k.split("_")[1]
    await upsert(env, k, "alpha", "ALPHA-1")
    await mcp(env, k, "kg_get_entity", graph="alpha", key=A1)  # allowed read: not audited
    await mcp(env, k, "kg_upsert_entity", graph="beta", provider="logical", type="jira_issue", native_id="BETA-9")
    await rest(env, k, "DELETE", "/kg/xref", params=XREF)
    rows = [r for r in env.audit.rows if r[0] == key_id]
    assert rows == [
        (key_id, "kg_upsert_entity", "alpha", A1, "allowed", None),
        (key_id, "kg_upsert_entity", "beta", "BETA-9", "denied", {"rule": "tenant_forbidden"}),
        (key_id, "kg_delete_xref", "beta", f"alpha::{A1} -pattern_from-> beta::{B1}", "denied",
         {"rule": "tenant_forbidden"}),
    ]


# ── wiring ──────────────────────────────────────────────────────────────────────
async def test_second_app_uses_its_own_graph(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    async for e in _serving(_build(tmp_path / "a")):
        await upsert(e, e.key(), "alpha", "ALPHA-1")
    async for e in _serving(_build(tmp_path / "b")):
        assert ok(await mcp(e, e.key(), "kg_find", graph="alpha"))["items"] == []


async def test_legacy_mode_sees_every_graph_and_xref(legacy_env):
    e = legacy_env
    await upsert(e, LEGACY, "alpha", "ALPHA-1", display_name="shared widget")
    await upsert(e, LEGACY, "beta", "BETA-1", display_name="shared widget")
    ok(await mcp(e, LEGACY, "kg_xref", note="n", **XREF))
    assert ok(await mcp(e, LEGACY, "kg_catalog"))["graphs"] == ["alpha", "beta"]
    assert set(ok(await rest(e, LEGACY, "GET", "/kg/graphs"))["graphs"]) == {"alpha", "beta"}
    hits = ok(await mcp(e, LEGACY, "kg_resolve", query="shared widget"))["results"]
    assert {h["graph"] for h in hits} == {"alpha", "beta"}
    assert len(ok(await mcp(e, LEGACY, "kg_get_entity", graph="alpha", key=A1))["xrefs"]) == 1
    assert (await mcp(e, LEGACY, "kg_find", graph="gamma")).data["error"] == "unknown_graph"
    assert e.audit.rows == []


# ── import: xrefs are authorized as kg_xref and must touch the import's graph ──
async def test_import_xref_needs_kg_xref(env):
    both = await seed_xref(env)
    k = env.key(tenants=("alpha", "beta"), denied=("kg_xref",))
    body = ndjson({"record": "entity", "provider": "logical", "type": "jira_issue", "native_id": "ALPHA-8"},
                  {"record": "xref", "src": f"alpha::{A1}", "relation": "similar_to", "dst": f"beta::{B1}",
                   "note": "n"})
    denied(await rest(env, k, "POST", "/kg/alpha/import", content=body), "tool_denied", "kg_xref is denied")
    assert (await mcp(env, both, "kg_get_entity", graph="alpha", key="logical:ALPHA-8")).data["error"] == "not_found"
    assert len(ok(await mcp(env, both, "kg_get_entity", graph="alpha", key=A1))["xrefs"]) == 1


async def test_import_xref_between_two_other_graphs_is_refused(env):
    env.app.state.tenants.create("delta", "Delta", "", by="test")
    k = env.key(tenants=("alpha", "beta", "delta"))
    await upsert(env, k, "beta", "BETA-1")
    await upsert(env, k, "delta", "DELTA-1")
    body = ndjson({"record": "entity", "provider": "logical", "type": "jira_issue", "native_id": "ALPHA-8"},
                  {"record": "xref", "src": f"beta::{B1}", "relation": "similar_to", "dst": "delta::logical:DELTA-1",
                   "note": "n"})
    denied(await rest(env, k, "POST", "/kg/alpha/import", content=body), "xref_outside_graph",
           "must have an endpoint in it")
    assert (await dump(env, k, "alpha"))["entity"] == []
    assert (await dump(env, k, "beta"))["xref"] == []
    assert ok(await mcp(env, k, "kg_get_entity", graph="delta", key="logical:DELTA-1"))["xrefs"] == []


# ── audit of cross-graph writes: one row per endpoint graph ─────────────────────
def rows_for(e: Env, who: str, action: str) -> list[tuple]:
    key_id = who.split("_")[1]
    return [r[2:] for r in e.audit.rows if r[0] == key_id and r[1] == action]


async def test_xref_create_and_delete_are_audited_under_both_graphs(env):
    both = await seed_xref(env)
    target = f"alpha::{A1} -pattern_from-> beta::{B1}"
    assert rows_for(env, both, "kg_xref") == [("alpha", target, "allowed", None), ("beta", target, "allowed", None)]
    ok(await mcp(env, both, "kg_delete_xref", **XREF))
    assert rows_for(env, both, "kg_delete_xref") == [("alpha", target, "allowed", None),
                                                     ("beta", target, "allowed", None)]


async def test_imported_xrefs_are_audited_per_xref(env):
    both = await seed_xref(env)
    await upsert(env, both, "beta", "BETA-2")
    body = ndjson({"record": "xref", "src": f"alpha::{A1}", "relation": "similar_to", "dst": f"beta::{B1}",
                   "note": "n"},
                  {"record": "xref", "src": f"beta::{B2}", "relation": "similar_to", "dst": f"alpha::{A1}",
                   "note": "n"})
    r = ok(await rest(env, both, "POST", "/kg/alpha/import", content=body))
    assert r["xrefs"] == {"imported": 2, "errors": []}
    assert rows_for(env, both, "kg_batch") == [("alpha", None, "allowed", None)]
    t1, t2 = f"alpha::{A1} -similar_to-> beta::{B1}", f"beta::{B2} -similar_to-> alpha::{A1}"
    assert rows_for(env, both, "kg_xref")[2:] == [  # [0:2] are seed_xref's own rows
        ("alpha", t1, "allowed", None), ("beta", t1, "allowed", None),
        ("beta", t2, "allowed", None), ("alpha", t2, "allowed", None)]


@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_hard_delete_audits_each_graph_whose_xrefs_it_touched(env, transport):
    both = await seed_xref(env)
    beta_only = env.key(tenants=("beta",))
    if transport == "mcp":
        ok(await mcp(env, beta_only, "kg_delete_entity", graph="beta", key=B1, confirm=True))
    else:
        ok(await rest(env, beta_only, "DELETE", f"/kg/beta/entities/{B1}", params={"hard": "true"}))
    assert rows_for(env, beta_only, "kg_delete_entity") == [("beta", B1, "allowed", None),
                                                            ("alpha", B1, "allowed", {"xrefs_dangling": 1})]
    await upsert(env, both, "alpha", "ALPHA-2")
    ok(await mcp(env, both, "kg_delete_entity", graph="alpha", key=A1, confirm=True))
    assert rows_for(env, both, "kg_delete_entity") == [("alpha", A1, "allowed", None),
                                                       ("beta", A1, "allowed", {"xrefs_removed": 1})]


# ── agent stamping: KG writes record the key's agent / UI user, not a declared one ──
def _kg_store():
    return mcp_tools._kg.store


def _edge(graph: str, src: str, dst: str, relation: str = "depends_on"):
    return _kg_store().current_edge(graph, src, relation, dst)


async def test_mcp_kg_writes_stamp_the_key_agent(env):
    k = env.key(tenants=("alpha", "beta"), agent="agent-a")
    await upsert(env, k, "alpha", "ALPHA-1", agent="spoofed")
    await upsert(env, k, "alpha", "ALPHA-2")
    ok(await mcp(env, k, "kg_link", graph="alpha", src=A1, relation="depends_on", dst=A2, agent="spoofed"))
    ok(await mcp(env, k, "kg_batch", graph="beta", agent="spoofed", entities=[
        {"provider": "logical", "type": "jira_issue", "native_id": "BETA-1", "agent": "item-spoof"},
        {"provider": "logical", "type": "jira_issue", "native_id": "BETA-2"}],
        edges=[{"src": B1, "relation": "depends_on", "dst": B2, "agent": "item-spoof"}]))
    ok(await mcp(env, k, "kg_xref", note="n", agent="spoofed", **XREF))
    store = _kg_store()
    assert store.get_entity("alpha", A1).agent == "agent-a"
    assert _edge("alpha", A1, A2).agent == "agent-a"
    assert store.get_entity("beta", B1).agent == "agent-a" and store.get_entity("beta", B2).agent == "agent-a"
    assert _edge("beta", B1, B2).agent == "agent-a"
    assert store.get_xref(XREF["src"], XREF["relation"], XREF["dst"]).agent == "agent-a"


async def test_mcp_kg_unlink_and_retire_stamp_the_ui_user(env):
    k = env.key(agent="agent-a")
    await upsert(env, k, "alpha", "ALPHA-1")
    await upsert(env, k, "alpha", "ALPHA-2")
    ok(await mcp(env, k, "kg_link", graph="alpha", src=A1, relation="depends_on", dst=A2))
    ok(await mcp(env, k, "kg_link", graph="alpha", src=A2, relation="depends_on", dst=A1))
    editor = ui_headers(name="Ada Example", groups=(G_EDIT,))
    ok(await mcp(env, editor, "kg_unlink", graph="alpha", src=A1, relation="depends_on", dst=A2, reason="moved"))
    unlinked = [e for e in _kg_store().iter_edges("alpha") if (e.src, e.dst) == (A1, A2) and e.valid_to]
    assert unlinked and unlinked[-1].agent == "ui:Ada Example"
    ok(await mcp(env, editor, "kg_retire_entity", graph="alpha", key=A2, reason="gone"))
    assert _kg_store().get_entity("alpha", A2).agent == "ui:Ada Example"
    retired_edges = [e for e in _kg_store().iter_edges("alpha") if (e.src, e.dst) == (A2, A1)]
    assert retired_edges and all(e.agent == "ui:Ada Example" for e in retired_edges)


async def test_rest_import_stamps_the_key_agent(env):
    k = env.key(tenants=("alpha", "beta"), agent="agent-a")
    await upsert(env, k, "beta", "BETA-1")
    body = ndjson(
        {"record": "entity", "provider": "logical", "type": "jira_issue", "native_id": "ALPHA-1",
         "agent": "spoofed"},
        {"record": "entity", "provider": "logical", "type": "jira_issue", "native_id": "ALPHA-2"},
        {"record": "edge", "src": A1, "relation": "depends_on", "dst": A2, "agent": "spoofed"},
        {"record": "xref", "note": "n", "agent": "spoofed", **XREF})
    r = ok(await rest(env, k, "POST", "/kg/alpha/import", content=body))
    assert r["status"] == "ok" and r["xrefs"] == {"imported": 1, "errors": []}
    store = _kg_store()
    assert store.get_entity("alpha", A1).agent == "agent-a"
    assert _edge("alpha", A1, A2).agent == "agent-a"
    assert store.get_xref(XREF["src"], XREF["relation"], XREF["dst"]).agent == "agent-a"


async def test_legacy_mode_kg_writes_keep_the_declared_agent(legacy_env):
    await upsert(legacy_env, LEGACY, "alpha", "ALPHA-1", agent="declared-agent")
    await upsert(legacy_env, LEGACY, "alpha", "ALPHA-2")
    ok(await mcp(legacy_env, LEGACY, "kg_link", graph="alpha", src=A1, relation="depends_on", dst=A2,
                 agent="declared-agent"))
    ok(await mcp(legacy_env, LEGACY, "kg_unlink", graph="alpha", src=A1, relation="depends_on", dst=A2,
                 reason="r"))
    store = _kg_store()
    assert store.get_entity("alpha", A1).agent == "declared-agent"
    assert store.get_entity("alpha", A2).agent == "claude-code"
    unlinked = [e for e in store.iter_edges("alpha") if (e.src, e.dst) == (A1, A2)]
    assert unlinked and all(e.agent == "declared-agent" for e in unlinked)
    body = ndjson({"record": "xref", "note": "n", "agent": "declared-agent", "src": f"alpha::{A1}",
                   "relation": "similar_to", "dst": f"alpha::{A2}"})
    ok(await rest(legacy_env, LEGACY, "POST", "/kg/alpha/import", content=body))
    assert store.get_xref(f"alpha::{A1}", "similar_to", f"alpha::{A2}").agent == "declared-agent"


async def test_rest_import_runs_store_work_off_the_event_loop(env, monkeypatch):
    import threading
    k = env.key(tenants=("alpha", "beta"))
    await upsert(env, k, "beta", "BETA-1")
    service, loop_thread, seen = mcp_tools._kg.service, threading.current_thread(), []

    def spy(name):
        real = getattr(service, name)

        def wrapper(*a, **kw):
            seen.append((name, threading.current_thread() is loop_thread))
            return real(*a, **kw)
        return wrapper
    for name in ("batch", "xref"):
        monkeypatch.setattr(service, name, spy(name))
    real_require = service.reg.require_graph
    monkeypatch.setattr(service.reg, "require_graph", lambda g: (
        seen.append(("require_graph", threading.current_thread() is loop_thread)), real_require(g))[1])
    body = ndjson({"record": "entity", "provider": "logical", "type": "jira_issue", "native_id": "ALPHA-1"},
                  {"record": "xref", "note": "n", **XREF})
    r = ok(await rest(env, k, "POST", "/kg/alpha/import", content=body))
    assert r["status"] == "ok" and r["xrefs"]["imported"] == 1
    called = {name for name, _ in seen}
    assert {"require_graph", "batch", "xref"} <= called
    assert not any(on_loop for _, on_loop in seen), seen


@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_kg_denial_audit_keeps_only_well_formed_graph_names(env, transport):
    k = env.key()
    for g in ("Not A Graph!", "beta"):
        if transport == "mcp":
            assert not (await mcp(env, k, "kg_find", graph=g)).ok
        else:
            assert not (await rest(env, k, "POST", f"/kg/{g}/path", body={"src": "a", "dst": "b"})).ok
    graphs = [r[2] for r in env.audit.rows if r[4] == "denied"]
    assert graphs == [None, "beta"]

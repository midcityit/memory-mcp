"""FastAPI router for the knowledge graph (bearer-protected via the injected dependency).

Every route is authorized per graph through the same guard as the MCP tools, under the
action of the tool that does the same thing (ROUTE_ACTION).
"""
from __future__ import annotations

import json
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from memory_mcp.admin.audit import NullAudit
from memory_mcp.admin.models import AuthzError
from memory_mcp.kg.authz import KGGuard, fq_graph
from memory_mcp.kg.models import KGError
from memory_mcp.kg.service import EDGE_FIELDS, ENTITY_FIELDS, count_error

_404 = {"not_found", "endpoint_not_found", "unknown_graph"}

# (method, path) -> the kg tool whose authorization the route uses. Every route must be here.
ROUTE_ACTION: dict[tuple[str, str], str] = {
    ("GET", "/kg/graphs"): "kg_catalog",
    ("GET", "/kg/catalog"): "kg_catalog",
    ("GET", "/kg/overview"): "kg_overview",
    ("POST", "/kg/resolve"): "kg_resolve",
    ("GET", "/kg/{graph}/entities/{key:path}"): "kg_get_entity",
    ("POST", "/kg/{graph}/traverse"): "kg_traverse",
    ("POST", "/kg/{graph}/path"): "kg_path",
    ("POST", "/kg/{graph}/impact"): "kg_impact",
    ("GET", "/kg/{graph}/export"): "kg_find",
    ("POST", "/kg/{graph}/import"): "kg_batch",
    ("DELETE", "/kg/{graph}/entities/{key:path}"): "kg_delete_entity",
    ("DELETE", "/kg/xref"): "kg_delete_xref",
}
A_XREF = "kg_xref"  # action for each xref inside an import


def _run(scrub: Callable[[dict], dict], fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KGError as e:
        count_error(e.code)
        raise HTTPException(status_code=404 if e.code in _404 else 422, detail=scrub(e.to_dict()))


def _xref_target(x: dict) -> str:
    return f"{x['src']} -{x['relation']}-> {x['dst']}"


def _graphs_param(graphs: str) -> str | list[str]:
    return "*" if graphs in ("", "*") else [g for g in graphs.split(",") if g]


class ResolveReq(BaseModel):
    query: str
    graphs: str | list[str] = "*"
    provider: str | None = None
    kind: str | None = None
    type: str | None = None
    include_retired: bool = False
    limit: int = 10


class TraverseReq(BaseModel):
    start: str
    direction: str = "both"
    relations: list[str] | None = None
    kinds: list[str] | None = None
    max_depth: int = 3
    as_of: str | None = None
    follow_xrefs: bool = False
    limit: int = 200


class PathReq(BaseModel):
    src: str
    dst: str
    relations: list[str] | None = None
    max_depth: int = 6


class ImpactReq(BaseModel):
    key: str
    max_depth: int = 6
    as_of: str | None = None


def build_router(service, query, require_token: Callable, guard: KGGuard | None = None) -> APIRouter:
    """`require_token` resolves the caller (a Principal when `guard` enforces tenants)."""
    r = APIRouter(prefix="/kg", dependencies=[Depends(require_token)])
    store = service.store
    guard = guard or KGGuard(None, NullAudit())

    def act(method: str, path: str) -> str:
        return ROUTE_ACTION[(method, path)]  # KeyError at build time if a route has no mapping

    def run(p, fn, *args, **kwargs):
        return _run(lambda d: guard.scrub(p, d), fn, *args, **kwargs)

    a_graphs = act("GET", "/kg/graphs")

    @r.get("/graphs")
    def graphs(p=Depends(require_token)):
        allowed = guard.graphs(p, a_graphs, "*")
        all_graphs = service.reg.graphs
        if allowed == "*":
            return {"graphs": all_graphs}
        return {"graphs": {g: all_graphs[g] for g in allowed if g in all_graphs}}

    a_catalog = act("GET", "/kg/catalog")

    @r.get("/catalog")
    def catalog(provider: str | None = None, kind: str | None = None, search: str | None = None,
                p=Depends(require_token)):
        return guard.catalog(p, a_catalog, lambda: run(p, service.reg.describe_catalog, provider, kind, search))

    a_overview = act("GET", "/kg/overview")

    @r.get("/overview")
    def overview(graphs: str = "*", p=Depends(require_token)):
        gs = guard.graphs(p, a_overview, _graphs_param(graphs))
        return run(p, query.overview, gs, visible=guard.visible(p))

    a_resolve = act("POST", "/kg/resolve")

    @r.post("/resolve")
    def resolve(req: ResolveReq, p=Depends(require_token)):
        gs = guard.graphs(p, a_resolve, req.graphs)
        return run(p, query.resolve, req.query, gs, req.provider, req.kind, req.type, req.include_retired, req.limit)

    a_get = act("GET", "/kg/{graph}/entities/{key:path}")

    @r.get("/{graph}/entities/{key:path}")
    def get_entity(graph: str, key: str, as_of: str | None = None, include_history: bool = False,
                   p=Depends(require_token)):
        guard.graph(p, a_get, graph, key)
        return run(p, query.get_entity, graph, key, as_of, include_history, visible=guard.visible(p))

    a_traverse = act("POST", "/kg/{graph}/traverse")

    @r.post("/{graph}/traverse")
    def traverse(graph: str, req: TraverseReq, p=Depends(require_token)):
        guard.graph(p, a_traverse, graph, req.start)
        return run(p, query.traverse, graph, req.start, req.direction, req.relations, req.kinds, req.max_depth,
                    req.as_of, req.follow_xrefs, req.limit, visible=guard.visible(p))

    a_path = act("POST", "/kg/{graph}/path")

    @r.post("/{graph}/path")
    def path(graph: str, req: PathReq, p=Depends(require_token)):
        guard.graph(p, a_path, graph)
        return run(p, query.path, graph, req.src, req.dst, req.relations, req.max_depth)

    a_impact = act("POST", "/kg/{graph}/impact")

    @r.post("/{graph}/impact")
    def impact(graph: str, req: ImpactReq, p=Depends(require_token)):
        guard.graph(p, a_impact, graph, req.key)
        return run(p, query.impact, graph, req.key, req.max_depth, req.as_of)

    a_export = act("GET", "/kg/{graph}/export")

    @r.get("/{graph}/export")
    def export(graph: str, p=Depends(require_token)):
        guard.graph(p, a_export, graph)
        run(p, service.reg.require_graph, graph)
        visible = guard.visible(p)

        def lines():
            for e in store.iter_entities(graph):
                yield json.dumps({**e.to_payload(), "record": "entity"}) + "\n"
            for e in store.iter_edges(graph):
                yield json.dumps({**e.to_payload(), "record": "edge"}) + "\n"
            for x in store.xrefs_for_graph(graph):
                if not x.src.startswith(f"{graph}::"):  # each xref is exported once, by its source graph
                    continue
                if visible is not None and fq_graph(x.dst) not in visible:
                    continue
                yield json.dumps({**x.to_payload(), "record": "xref"}) + "\n"
        return StreamingResponse(lines(), media_type="application/x-ndjson")

    a_import = act("POST", "/kg/{graph}/import")

    @r.post("/{graph}/import")
    async def import_(graph: str, request: Request, p=Depends(require_token)):
        guard.graph(p, a_import, graph)
        run(p, service.reg.require_graph, graph)
        ents, edges, xrefs = [], [], []

        def bad(n, msg):
            return HTTPException(422, detail={"error": "invalid_json", "message": f"line {n}: {msg}"})

        try:
            text = (await request.body()).decode("utf-8")
        except UnicodeDecodeError as e:
            raise bad(0, f"body is not valid UTF-8: {e}")
        for n, line in enumerate(text.split("\n"), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise bad(n, str(e))
            if not isinstance(rec, dict):
                raise bad(n, "expected a JSON object")
            record = rec.get("record")
            if record == "entity":
                ents.append({k: rec.get(k) for k in ENTITY_FIELDS})
            elif record == "edge":
                edges.append({k: rec.get(k) for k in EDGE_FIELDS})
            elif record == "xref":
                for f in ("src", "relation", "dst", "note"):
                    if not isinstance(rec.get(f), str):
                        raise bad(n, f"xref field '{f}' missing or not a string")
                xrefs.append(rec)
            else:
                raise bad(n, f"missing or unknown record type: {record!r}")
        # Each xref is authorized as kg_xref on both of its graphs and must touch this graph;
        # everything is checked before anything is written.
        for x in xrefs:
            target = _xref_target(x)
            ends = [guard.fq(p, A_XREF, x["src"], target), guard.fq(p, A_XREF, x["dst"], target)]
            if guard.enforcing and graph not in ends:
                raise guard.deny(p, A_XREF, graph, target, AuthzError(
                    403, "xref_outside_graph", f"An xref imported into '{graph}' must have an endpoint in it"))
        result = guard.scrub(p, run(p, service.batch, graph, ents, edges, "import"))
        guard.written(p, a_import, graph, None, result)
        if result["status"] != "ok":
            raise HTTPException(422, detail=result)
        xr = {"imported": 0, "errors": []}
        for x in xrefs:
            target = _xref_target(x)
            try:
                service.xref(x["src"], x["relation"], x["dst"], x["note"], x.get("evidence_memory_ids"),
                             x.get("agent", "import"))
                xr["imported"] += 1
                guard.written(p, A_XREF, [fq_graph(x["src"]), fq_graph(x["dst"])], target)
            except KGError as e:
                xr["errors"].append({"xref": target, **guard.scrub(p, e.to_dict())})
        return {**result, "xrefs": xr}

    a_delete = act("DELETE", "/kg/{graph}/entities/{key:path}")

    @r.delete("/{graph}/entities/{key:path}")
    def delete_entity(graph: str, key: str, hard: bool = False, p=Depends(require_token)):
        guard.graph(p, a_delete, graph, key)
        if not hard:
            raise HTTPException(400, detail={"error": "hard_required",
                                             "message": "Use kg_retire_entity to decommission; pass hard=true to purge"})
        deletion = guard.xref_deletion(p, graph)
        result = run(p, service.delete_entity, graph, key, True, xref_removable=deletion)
        return guard.written_deletion(p, a_delete, graph, key, result, deletion)

    a_delete_xref = act("DELETE", "/kg/xref")

    @r.delete("/xref")
    def delete_xref(src: str, relation: str, dst: str, p=Depends(require_token)):
        target = _xref_target({"src": src, "relation": relation, "dst": dst})
        graphs = [guard.fq(p, a_delete_xref, src, target), guard.fq(p, a_delete_xref, dst, target)]
        return guard.written(p, a_delete_xref, graphs, target, run(p, service.delete_xref, src, relation, dst))

    return r

"""FastAPI router for the knowledge graph (bearer-protected via the injected dependency)."""
from __future__ import annotations

import json
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from memory_mcp.kg.models import KGError
from memory_mcp.kg.service import EDGE_FIELDS, ENTITY_FIELDS, count_error

_404 = {"not_found", "endpoint_not_found", "unknown_graph"}


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KGError as e:
        count_error(e.code)
        raise HTTPException(status_code=404 if e.code in _404 else 422, detail=e.to_dict())


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


def build_router(service, query, require_token: Callable) -> APIRouter:
    r = APIRouter(prefix="/kg", dependencies=[Depends(require_token)])
    store = service.store

    @r.get("/graphs")
    def graphs():
        return {"graphs": service.reg.graphs}

    @r.get("/catalog")
    def catalog(provider: str | None = None, kind: str | None = None, search: str | None = None):
        return _run(service.reg.describe_catalog, provider, kind, search)

    @r.get("/overview")
    def overview(graphs: str = "*"):
        return _run(query.overview, _graphs_param(graphs))

    @r.post("/resolve")
    def resolve(req: ResolveReq):
        return _run(query.resolve, req.query, req.graphs, req.provider, req.kind, req.type, req.include_retired, req.limit)

    @r.get("/{graph}/entities/{key:path}")
    def get_entity(graph: str, key: str, as_of: str | None = None, include_history: bool = False):
        return _run(query.get_entity, graph, key, as_of, include_history)

    @r.post("/{graph}/traverse")
    def traverse(graph: str, req: TraverseReq):
        return _run(query.traverse, graph, req.start, req.direction, req.relations, req.kinds, req.max_depth,
                    req.as_of, req.follow_xrefs, req.limit)

    @r.post("/{graph}/path")
    def path(graph: str, req: PathReq):
        return _run(query.path, graph, req.src, req.dst, req.relations, req.max_depth)

    @r.post("/{graph}/impact")
    def impact(graph: str, req: ImpactReq):
        return _run(query.impact, graph, req.key, req.max_depth, req.as_of)

    @r.get("/{graph}/export")
    def export(graph: str):
        _run(service.reg.require_graph, graph)

        def lines():
            for e in store.iter_entities(graph):
                yield json.dumps({**e.to_payload(), "record": "entity"}) + "\n"
            for e in store.iter_edges(graph):
                yield json.dumps({**e.to_payload(), "record": "edge"}) + "\n"
            for x in store.xrefs_for_graph(graph):
                if x.src.startswith(f"{graph}::"):  # each xref is exported once, by its source graph
                    yield json.dumps({**x.to_payload(), "record": "xref"}) + "\n"
        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @r.post("/{graph}/import")
    async def import_(graph: str, request: Request):
        _run(service.reg.require_graph, graph)
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
        result = _run(service.batch, graph, ents, edges, "import")
        if result["status"] != "ok":
            raise HTTPException(422, detail=result)
        xr = {"imported": 0, "errors": []}
        for x in xrefs:
            try:
                service.xref(x["src"], x["relation"], x["dst"], x["note"], x.get("evidence_memory_ids"),
                             x.get("agent", "import"))
                xr["imported"] += 1
            except KGError as e:
                xr["errors"].append({"xref": f"{x['src']} -{x['relation']}-> {x['dst']}", **e.to_dict()})
        return {**result, "xrefs": xr}

    @r.delete("/{graph}/entities/{key:path}")
    def delete_entity(graph: str, key: str, hard: bool = False):
        if not hard:
            raise HTTPException(400, detail={"error": "hard_required",
                                             "message": "Use kg_retire_entity to decommission; pass hard=true to purge"})
        return _run(service.delete_entity, graph, key, True)

    @r.delete("/xref")
    def delete_xref(src: str, relation: str, dst: str):
        return _run(service.delete_xref, src, relation, dst)

    return r

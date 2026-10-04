"""Qdrant-backed graph storage: per-graph entity/edge collections + shared xrefs."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, FieldCondition, Filter, IsNullCondition, MatchAny, MatchValue, PayloadField,
    PayloadSchemaType, PointIdsList, PointStruct, Range, VectorParams,
)

from memory_mcp.kg.models import Edge, Entity, XRef, edge_point_id, entity_point_id, xref_point_id

XREFS = "kg_xrefs"
FRONTIER_CHUNK = 500
_SCROLL_PAGE = 1000


def entities_collection(graph: str) -> str:
    return f"kg_{graph}_entities"


def edges_collection(graph: str) -> str:
    return f"kg_{graph}_edges"


@dataclass
class EntityFilter:
    provider: str | None = None
    kind: str | None = None
    type: str | None = None
    memory_id: str | None = None
    include_retired: bool = False
    at_ts: float | None = None


def _time_conditions(at_ts: float | None, include_retired: bool) -> list:
    if include_retired:
        return []
    t = at_ts if at_ts is not None else time.time()
    return [
        FieldCondition(key="valid_from_ts", range=Range(lte=t)),
        Filter(should=[IsNullCondition(is_null=PayloadField(key="valid_to_ts")),
                       FieldCondition(key="valid_to_ts", range=Range(gt=t))]),
    ]


def _eq(key: str, value) -> FieldCondition:
    return FieldCondition(key=key, match=MatchValue(value=value))


class QdrantGraphStore:
    def __init__(self, client: QdrantClient, embedder):
        self.client = client
        self.embedder = embedder

    # ── setup ────────────────────────────────────────────────────────────────
    def _existing(self) -> set[str]:
        return {c.name for c in self.client.get_collections().collections}

    def _index(self, coll: str, keywords: list[str], floats: list[str]) -> None:
        for f in keywords:
            self.client.create_payload_index(coll, f, PayloadSchemaType.KEYWORD)
        for f in floats:
            self.client.create_payload_index(coll, f, PayloadSchemaType.FLOAT)

    def ensure_graph(self, graph: str) -> None:
        existing = self._existing()
        ents, edges = entities_collection(graph), edges_collection(graph)
        if ents not in existing:
            self.client.create_collection(ents, vectors_config=VectorParams(size=self.embedder.dim, distance=Distance.COSINE))
        if edges not in existing:
            self.client.create_collection(edges, vectors_config={})
        self._index(ents, ["key", "provider", "kind", "type", "memory_ids", "aliases", "native_id", "display_name"], ["valid_from_ts", "valid_to_ts"])
        self._index(edges, ["src", "dst", "relation", "evidence_memory_ids"], ["valid_from_ts", "valid_to_ts"])

    def ensure_xrefs(self) -> None:
        if XREFS not in self._existing():
            self.client.create_collection(XREFS, vectors_config={})
        self._index(XREFS, ["src", "dst", "src_graph", "dst_graph"], [])

    # ── helpers ──────────────────────────────────────────────────────────────
    def _scroll_all(self, coll: str, flt: Filter | None) -> Iterator:
        offset = None
        while True:
            pts, offset = self.client.scroll(coll, scroll_filter=flt, limit=_SCROLL_PAGE, offset=offset, with_payload=True)
            yield from pts
            if offset is None:
                return

    def _entity_filter(self, f: EntityFilter) -> Filter:
        must = []
        if f.provider:
            must.append(_eq("provider", f.provider))
        if f.kind:
            must.append(_eq("kind", f.kind))
        if f.type:
            must.append(_eq("type", f.type))
        if f.memory_id:
            must.append(FieldCondition(key="memory_ids", match=MatchAny(any=[f.memory_id])))
        must += _time_conditions(f.at_ts, f.include_retired)
        return Filter(must=must)

    # ── entities ─────────────────────────────────────────────────────────────
    def get_entities(self, graph: str, keys: list[str]) -> list[Entity]:
        if not keys:
            return []
        pts = self.client.retrieve(entities_collection(graph), ids=[entity_point_id(k) for k in keys], with_payload=True)
        by_key = {p.payload["key"]: Entity.from_payload(p.payload) for p in pts}
        return [by_key[k] for k in keys if k in by_key]

    def get_entity(self, graph: str, key: str) -> Entity | None:
        found = self.get_entities(graph, [key])
        return found[0] if found else None

    def upsert_entity(self, graph: str, e: Entity) -> Entity:
        vector = self.embedder.embed(f"{e.display_name} {' '.join(e.aliases)} {e.type}")
        self.client.upsert(entities_collection(graph),
                           points=[PointStruct(id=entity_point_id(e.key), vector=vector, payload=e.to_payload())])
        return e

    def search_entities(self, graph: str, vector: list[float], filt: EntityFilter, limit: int) -> list[tuple[Entity, float]]:
        res = self.client.query_points(entities_collection(graph), query=vector, query_filter=self._entity_filter(filt),
                                       limit=limit, with_payload=True)
        return [(Entity.from_payload(p.payload), p.score) for p in res.points]

    def scroll_entities(self, graph: str, filt: EntityFilter, limit: int, cursor: str | None = None):
        pts, nxt = self.client.scroll(entities_collection(graph), scroll_filter=self._entity_filter(filt),
                                      limit=limit, offset=cursor, with_payload=True)
        return [Entity.from_payload(p.payload) for p in pts], (str(nxt) if nxt is not None else None)

    def count_entities(self, graph: str, filt: EntityFilter) -> int:
        return self.client.count(entities_collection(graph), count_filter=self._entity_filter(filt), exact=True).count

    def exact_matches(self, graph: str, text: str, filt: EntityFilter, limit: int = 5) -> list[Entity]:
        """Find entities matching text exactly on aliases, native_id, or display_name, respecting filters."""
        must = []
        if filt.provider:
            must.append(_eq("provider", filt.provider))
        if filt.kind:
            must.append(_eq("kind", filt.kind))
        if filt.type:
            must.append(_eq("type", filt.type))
        if filt.memory_id:
            must.append(FieldCondition(key="memory_ids", match=MatchAny(any=[filt.memory_id])))
        must += _time_conditions(filt.at_ts, filt.include_retired)
        # Build should clause: aliases contains text OR native_id equals text OR display_name equals text
        exact_filter = Filter(
            must=must,
            should=[
                FieldCondition(key="aliases", match=MatchAny(any=[text])),
                _eq("native_id", text),
                _eq("display_name", text),
            ],
        )
        pts = self.client.query_points(entities_collection(graph), query_filter=exact_filter, limit=limit, with_payload=True).points
        return [Entity.from_payload(p.payload) for p in pts]

    def iter_entities(self, graph: str) -> Iterator[Entity]:
        for p in self._scroll_all(entities_collection(graph), None):
            yield Entity.from_payload(p.payload)

    # ── edges ────────────────────────────────────────────────────────────────
    def _edges_by(self, field: str, graph: str, keys: list[str], relations, at_ts, include_retired) -> list[Edge]:
        out: list[Edge] = []
        keys = list(dict.fromkeys(keys))
        for i in range(0, len(keys), FRONTIER_CHUNK):
            must = [FieldCondition(key=field, match=MatchAny(any=keys[i:i + FRONTIER_CHUNK]))]
            if relations:
                must.append(FieldCondition(key="relation", match=MatchAny(any=list(relations))))
            must += _time_conditions(at_ts, include_retired)
            out += [Edge.from_payload(p.payload) for p in self._scroll_all(edges_collection(graph), Filter(must=must))]
        return out

    def edges_from(self, graph, keys, relations=None, at_ts=None, include_retired=False) -> list[Edge]:
        return self._edges_by("src", graph, keys, relations, at_ts, include_retired)

    def edges_to(self, graph, keys, relations=None, at_ts=None, include_retired=False) -> list[Edge]:
        return self._edges_by("dst", graph, keys, relations, at_ts, include_retired)

    def edges_touching(self, graph, keys, relations=None, at_ts=None, include_retired=False) -> list[Edge]:
        """Edges whose src OR dst is in `keys`, in a single Qdrant query per chunk.

        Used by traverse(direction="both") to halve the edge round-trips vs calling
        edges_from + edges_to separately (one scroll instead of two per hop).
        """
        out: list[Edge] = []
        keys = list(dict.fromkeys(keys))
        for i in range(0, len(keys), FRONTIER_CHUNK):
            chunk = keys[i:i + FRONTIER_CHUNK]
            endpoint = Filter(should=[FieldCondition(key="src", match=MatchAny(any=chunk)),
                                      FieldCondition(key="dst", match=MatchAny(any=chunk))])
            must = [endpoint]
            if relations:
                must.append(FieldCondition(key="relation", match=MatchAny(any=list(relations))))
            must += _time_conditions(at_ts, include_retired)
            out += [Edge.from_payload(p.payload) for p in self._scroll_all(edges_collection(graph), Filter(must=must))]
        return out

    def current_edge(self, graph: str, src: str, relation: str, dst: str) -> Edge | None:
        flt = Filter(must=[_eq("src", src), _eq("relation", relation), _eq("dst", dst),
                           IsNullCondition(is_null=PayloadField(key="valid_to_ts"))])
        pts, _ = self.client.scroll(edges_collection(graph), scroll_filter=flt, limit=1, with_payload=True)
        return Edge.from_payload(pts[0].payload) if pts else None

    def put_edge(self, graph: str, e: Edge) -> Edge:
        pid = edge_point_id(e.src, e.relation, e.dst, e.valid_from)
        self.client.upsert(edges_collection(graph), points=[PointStruct(id=pid, vector={}, payload=e.to_payload())])
        return e

    def all_current_edges(self, graph: str) -> Iterator[Edge]:
        flt = Filter(must=[IsNullCondition(is_null=PayloadField(key="valid_to_ts"))])
        for p in self._scroll_all(edges_collection(graph), flt):
            yield Edge.from_payload(p.payload)

    def edges_with_memory(self, graph: str, memory_id: str) -> list[Edge]:
        flt = Filter(must=[FieldCondition(key="evidence_memory_ids", match=MatchAny(any=[memory_id]))])
        return [Edge.from_payload(p.payload) for p in self._scroll_all(edges_collection(graph), flt)]

    def iter_edges(self, graph: str) -> Iterator[Edge]:
        for p in self._scroll_all(edges_collection(graph), None):
            yield Edge.from_payload(p.payload)

    def delete_entity_hard(self, graph: str, key: str) -> int:
        flt = Filter(should=[_eq("src", key), _eq("dst", key)])
        ids = [p.id for p in self._scroll_all(edges_collection(graph), flt)]
        if ids:
            self.client.delete(edges_collection(graph), points_selector=PointIdsList(points=ids))
        self.client.delete(entities_collection(graph), points_selector=PointIdsList(points=[entity_point_id(key)]))
        return len(ids)

    # ── cross-graph references ───────────────────────────────────────────────
    def put_xref(self, x: XRef) -> XRef:
        self.client.upsert(XREFS, points=[PointStruct(id=xref_point_id(x.src, x.relation, x.dst), vector={}, payload=x.to_payload())])
        return x

    def get_xref(self, src: str, relation: str, dst: str) -> XRef | None:
        pts = self.client.retrieve(XREFS, ids=[xref_point_id(src, relation, dst)], with_payload=True)
        return XRef.from_payload(pts[0].payload) if pts else None

    def xrefs_touching(self, fq: str) -> list[XRef]:
        flt = Filter(should=[_eq("src", fq), _eq("dst", fq)])
        return [XRef.from_payload(p.payload) for p in self._scroll_all(XREFS, flt)]

    def xrefs_for_graph(self, graph: str) -> list[XRef]:
        flt = Filter(should=[_eq("src_graph", graph), _eq("dst_graph", graph)])
        return [XRef.from_payload(p.payload) for p in self._scroll_all(XREFS, flt)]

    def mark_xrefs_dangling(self, fq: str) -> int:
        xs = self.xrefs_touching(fq)
        for x in xs:
            self.client.set_payload(XREFS, payload={"dangling": True},
                                    points=[xref_point_id(x.src, x.relation, x.dst)])
        return len(xs)

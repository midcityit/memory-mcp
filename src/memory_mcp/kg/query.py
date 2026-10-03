"""Read-side knowledge-graph queries and graph algorithms."""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable

from memory_mcp.kg.models import Edge, Entity, KGError, fq_key, iso_to_ts
from memory_mcp.kg.store import EntityFilter

MAX_DEPTH = 6
LESSON_MEMORY_TYPES = ("decision", "troubleshooting", "runbook", "architecture")
_HUB_TTL = 60.0


def _size(obj) -> int:
    return len(json.dumps(obj, default=str))


def fit(result: dict, max_chars: int, list_keys: tuple[str, ...]) -> dict:
    """Trim the named lists from the tail until the JSON fits; mark truncated."""
    if _size(result) <= max_chars:
        return result
    result["truncated"] = True
    for k in list_keys:
        lst = result.get(k)
        while isinstance(lst, list) and lst and _size(result) > max_chars:
            lst.pop()
    return result


def _edge_dict(e: Edge, graph: str | None = None) -> dict:
    d = {"src": e.src, "relation": e.relation, "dst": e.dst}
    if graph:
        d = {"graph": graph, **d}
    if e.valid_to:
        d["valid_to"] = e.valid_to
    return d


class KGQuery:
    def __init__(self, registry, store, memory_lookup: Callable[[str], dict | None] | None = None,
                 max_chars: int = 6000):
        self.reg = registry
        self.store = store
        self.memory_lookup = memory_lookup or (lambda _id: None)
        self.max_chars = max_chars
        self._hubs: dict[str, tuple[float, list[tuple[str, int]]]] = {}

    # ── helpers ──────────────────────────────────────────────────────────────
    def _must_get(self, graph: str, key: str) -> Entity:
        self.reg.require_graph(graph)
        e = self.store.get_entity(graph, key)
        if e is None:
            raise KGError("not_found", f"No entity '{key}' in graph '{graph}'", field="key",
                          suggestions=["kg_resolve to find the right key"])
        return e

    def _compact_map(self, graph: str, keys) -> dict[str, dict]:
        return {e.key: e.compact() for e in self.store.get_entities(graph, list(keys))}

    def _memories(self, ids, types: tuple[str, ...] | None = None) -> list[dict]:
        out = []
        for i in ids:
            m = self.memory_lookup(i)
            if m and (types is None or m.get("type") in types):
                out.append(m)
        return out

    def _neighbors(self, graph, keys, direction, relations, at_ts):
        """Yield (from_key, to_key, edge) for edges touching `keys` in the given direction."""
        if direction in ("out", "both"):
            for e in self.store.edges_from(graph, keys, relations, at_ts):
                yield e.src, e.dst, e
        if direction in ("in", "both"):
            for e in self.store.edges_to(graph, keys, relations, at_ts):
                yield e.dst, e.src, e

    # ── resolve ──────────────────────────────────────────────────────────────
    def resolve(self, query, graphs="*", provider=None, kind=None, type=None, include_retired=False, limit=10) -> dict:
        q = query.strip().lower()
        vec = self.store.embedder.embed(query)
        scored: dict[tuple[str, str], dict] = {}
        for g in self.reg.graph_ids(graphs):
            filt = EntityFilter(provider=provider, kind=kind, type=type, include_retired=include_retired)
            hits = self.store.search_entities(g, vec, filt, limit * 2)
            exact = self.store.get_entity(g, query)
            if exact and (include_retired or exact.valid_to is None):
                hits.append((exact, 1.0))
            for e, s in hits:
                names = {e.key.lower(), e.native_id.lower(), e.display_name.lower(), *(a.lower() for a in e.aliases)}
                score = 1.0 if q in names else round(float(s), 4)
                prev = scored.get((g, e.key))
                if prev is None or score > prev["score"]:
                    scored[(g, e.key)] = {**e.compact(g), "score": score, "retired": e.valid_to is not None}
        results = sorted(scored.values(), key=lambda r: -r["score"])[:limit]
        return {"results": results}

    # ── get ──────────────────────────────────────────────────────────────────
    def get_entity(self, graph, key, as_of=None, include_history=False) -> dict:
        e = self._must_get(graph, key)
        at = iso_to_ts(as_of)
        outs = self.store.edges_from(graph, [key], at_ts=at)
        ins = self.store.edges_to(graph, [key], at_ts=at)
        cm = self._compact_map(graph, {x.dst for x in outs} | {x.src for x in ins})
        out_g: dict[str, list] = {}
        for x in outs:
            out_g.setdefault(x.relation, []).append(cm.get(x.dst, {"key": x.dst}))
        in_g: dict[str, list] = {}
        for x in ins:
            in_g.setdefault(self.reg.inverse_label(x.relation), []).append(cm.get(x.src, {"key": x.src}))
        result = {"entity": {**e.compact(graph), "provider": e.provider, "type": e.type, "native_id": e.native_id,
                             "aliases": e.aliases, "properties": e.properties, "valid_from": e.valid_from,
                             "valid_to": e.valid_to},
                  "out": out_g, "in": in_g, "memories": self._memories(e.memory_ids),
                  "xrefs": [x.to_payload() for x in self.store.xrefs_touching(fq_key(graph, key))]}
        if include_history:
            hist = []
            for x in self.store.edges_from(graph, [key], include_retired=True) + \
                    self.store.edges_to(graph, [key], include_retired=True):
                hist.append({"at": x.valid_from, "event": "open", "src": x.src, "relation": x.relation, "dst": x.dst})
                if x.valid_to:
                    hist.append({"at": x.valid_to, "event": "close", "src": x.src, "relation": x.relation,
                                 "dst": x.dst, "reason": x.retire_reason})
            result["history"] = sorted(hist, key=lambda h: iso_to_ts(h["at"]))
        return fit(result, self.max_chars, ("history",))

    # ── find ─────────────────────────────────────────────────────────────────
    def find(self, graph, provider=None, kind=None, type=None, missing_relation=None, direction="out",
             include_retired=False, limit=100, cursor=None) -> dict:
        self.reg.require_graph(graph)
        if missing_relation:
            self.reg.relation(missing_relation)
        filt = EntityFilter(provider=provider, kind=kind, type=type, include_retired=include_retired)
        page, nxt = self.store.scroll_entities(graph, filt, limit, cursor)
        if missing_relation and page:
            ks = [e.key for e in page]
            edges = (self.store.edges_from if direction == "out" else self.store.edges_to)(graph, ks, [missing_relation])
            have = {x.src if direction == "out" else x.dst for x in edges}
            page = [e for e in page if e.key not in have]
        result = {"items": [e.compact(graph) for e in page], "has_more": nxt is not None, "cursor": nxt}
        return fit(result, self.max_chars, ("items",))

    # ── traverse ─────────────────────────────────────────────────────────────
    def traverse(self, graph, start, direction="both", relations=None, kinds=None, max_depth=3, as_of=None,
                 follow_xrefs=False, limit=200, max_chars=None) -> dict:
        root = self._must_get(graph, start)
        at = iso_to_ts(as_of)
        max_depth = max(0, min(int(max_depth), MAX_DEPTH))
        nodes: dict[str, dict] = {start: {**root.compact(), "depth": 0}}
        edges: dict[tuple, dict] = {}
        frontier, has_more = [start], False
        for depth in range(1, max_depth + 1):
            if not frontier or has_more:
                break
            cand = list(self._neighbors(graph, frontier, direction, relations, at))
            new_keys = {other for _, other, _ in cand if other not in nodes}
            cm = {e.key: e for e in self.store.get_entities(graph, list(new_keys))}
            nxt = []
            for _, other, e in cand:
                if other not in nodes:
                    ent = cm.get(other)
                    if ent is None or (kinds and ent.kind not in kinds):
                        continue
                    if len(nodes) >= limit:
                        has_more = True
                        continue
                    nodes[other] = {**ent.compact(), "depth": depth}
                    nxt.append(other)
                edges[(e.src, e.relation, e.dst, e.valid_from)] = _edge_dict(e)
            frontier = nxt
        result = {"nodes": sorted(nodes.values(), key=lambda n: n["depth"]),
                  "edges": [d for d in edges.values() if d["src"] in nodes and d["dst"] in nodes],
                  "has_more": has_more, "truncated": False}
        if follow_xrefs:
            xs = {}
            for k in nodes:
                for x in self.store.xrefs_touching(fq_key(graph, k)):
                    xs[(x.src, x.relation, x.dst)] = x.to_payload()
            result["xrefs"] = list(xs.values())
        return self._trim_traversal(result, max_chars or self.max_chars)

    def _trim_traversal(self, result: dict, max_chars: int) -> dict:
        if _size(result) <= max_chars:
            return result
        result["truncated"] = True
        nodes = result["nodes"]  # sorted by depth: popping removes the deepest first
        while len(nodes) > 1 and _size(result) > max_chars:
            nodes.pop()
            keep = {n["key"] for n in nodes}
            result["edges"] = [e for e in result["edges"] if e["src"] in keep and e["dst"] in keep]
        for k in ("xrefs", "edges"):
            lst = result.get(k)
            while isinstance(lst, list) and lst and _size(result) > max_chars:
                lst.pop()
        return result

    # ── path ─────────────────────────────────────────────────────────────────
    def path(self, graph, src, dst, relations=None, max_depth=6) -> dict:
        self._must_get(graph, src)
        self._must_get(graph, dst)
        max_depth = max(1, min(int(max_depth), MAX_DEPTH))
        dist, parents, frontier = {src: 0}, {src: []}, [src]
        while frontier and dst not in dist:
            d = dist[frontier[0]]
            if d >= max_depth:
                break
            nxt = []
            for here, other, e in self._neighbors(graph, frontier, "both", relations, None):
                if other not in dist:
                    dist[other] = d + 1
                    parents[other] = [(here, e)]
                    nxt.append(other)
                elif dist[other] == d + 1:
                    parents[other].append((here, e))
            frontier = nxt
        if dst not in dist:
            return {"found": False, "paths": []}
        paths: list[list[tuple[str, Edge | None]]] = []

        def walk(node, acc):
            if len(paths) >= 3:
                return
            if node == src:
                paths.append(list(reversed(acc)))
                return
            for prev, e in parents[node]:
                walk(prev, acc + [(node, e)])

        walk(dst, [])
        cm = self._compact_map(graph, {k for p in paths for k, _ in p} | {src})
        out = []
        for p in paths:
            out.append({"nodes": [cm[src]] + [cm[k] for k, _ in p], "edges": [_edge_dict(e) for _, e in p]})
        return {"found": True, "paths": out}

    # ── impact ───────────────────────────────────────────────────────────────
    def impact(self, graph, key, max_depth=MAX_DEPTH, as_of=None) -> dict:
        root = self._must_get(graph, key)
        at = iso_to_ts(as_of)
        prop = self.reg.propagating_relations()
        seen, frontier, layers = {key}, [key], []
        for depth in range(1, max(1, min(int(max_depth), MAX_DEPTH)) + 1):
            nxt = sorted({e.src for e in self.store.edges_to(graph, frontier, prop, at)} - seen)
            if not nxt:
                break
            seen.update(nxt)
            layers.append((depth, nxt))
            frontier = nxt
        affected_keys = [k for _, ks in layers for k in ks]
        cm = self._compact_map(graph, affected_keys)
        work = self.store.edges_from(graph, affected_keys, ["tracked_in", "documented_in"], at) if affected_keys else []
        wm = self._compact_map(graph, {e.dst for e in work})
        result = {"root": root.compact(graph),
                  "affected": [{"depth": d, "nodes": [cm[k] for k in ks if k in cm]} for d, ks in layers],
                  "tracked_in": list({e.dst: wm[e.dst] for e in work if e.relation == "tracked_in" and e.dst in wm}.values()),
                  "documented_in": list({e.dst: wm[e.dst] for e in work if e.relation == "documented_in" and e.dst in wm}.values())}
        return fit(result, self.max_chars, ("documented_in", "tracked_in", "affected"))

    # ── overview ─────────────────────────────────────────────────────────────
    def _hub_degrees(self, graph: str) -> list[tuple[str, int]]:
        cached = self._hubs.get(graph)
        if cached and time.monotonic() - cached[0] < _HUB_TTL:
            return cached[1]
        deg = Counter()
        for e in self.store.all_current_edges(graph):
            deg[e.src] += 1
            deg[e.dst] += 1
        top = deg.most_common(10)
        self._hubs[graph] = (time.monotonic(), top)
        return top

    def overview(self, graphs="*") -> dict:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        out = []
        for g in self.reg.graph_ids(graphs):
            current = [e for e in self.store.iter_entities(g) if e.valid_to is None]
            hubs = self._hub_degrees(g)
            cm = self._compact_map(g, [k for k, _ in hubs])
            recent = sorted((e for e in current if iso_to_ts(e.updated_at) >= since.timestamp()),
                            key=lambda e: e.updated_at, reverse=True)[:10]
            out.append({"graph": g, "name": self.reg.graphs[g].get("name", g), "entities": len(current),
                        "by_kind": dict(Counter(e.kind for e in current)),
                        "by_provider": dict(Counter(e.provider for e in current)),
                        "hubs": [{**cm[k], "degree": d} for k, d in hubs if k in cm],
                        "recent": [e.compact() for e in recent],
                        "dangling_xrefs": sum(1 for x in self.store.xrefs_for_graph(g) if x.dangling)})
        return fit({"graphs": out}, self.max_chars, ("graphs",))

    # ── cross-graph ──────────────────────────────────────────────────────────
    def related_across(self, graph, key, target_graphs="*", limit=10) -> dict:
        e = self._must_get(graph, key)
        xrefs = [x.to_payload() for x in self.store.xrefs_touching(fq_key(graph, key))]
        vec = self.store.embedder.embed(f"{e.display_name} {' '.join(e.aliases)} {e.type}")
        similar = []
        for g in self.reg.graph_ids(target_graphs):
            if g == graph:
                continue
            for h, s in self.store.search_entities(g, vec, EntityFilter(kind=e.kind), limit):
                similar.append({**h.compact(g), "score": round(float(s), 4),
                                "memories": self._memories(h.memory_ids, LESSON_MEMORY_TYPES)})
        similar.sort(key=lambda r: -r["score"])
        return fit({"xrefs": xrefs, "similar": similar[:limit]}, self.max_chars, ("similar", "xrefs"))

    def for_memory(self, memory_id, graphs="*") -> dict:
        ents, edges = [], []
        for g in self.reg.graph_ids(graphs):
            page, cur = self.store.scroll_entities(g, EntityFilter(memory_id=memory_id, include_retired=True), 100)
            ents += [e.compact(g) for e in page]
            edges += [_edge_dict(x, g) for x in self.store.edges_with_memory(g, memory_id)]
        return fit({"entities": ents, "edges": edges}, self.max_chars, ("edges", "entities"))

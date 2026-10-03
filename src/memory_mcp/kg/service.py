"""Validated writes to the knowledge graph."""
from __future__ import annotations

from dataclasses import replace

from opentelemetry import metrics

from memory_mcp.kg.ids import normalize_native_id
from memory_mcp.kg.models import (
    Edge, Entity, KGError, XRef, iso_to_ts, make_key, now_iso, split_fq,
)
from memory_mcp.kg.store import EntityFilter

ENTITY_FIELDS = ("provider", "type", "native_id", "display_name", "aliases", "properties", "memory_ids",
                 "valid_from", "valid_to")
EDGE_FIELDS = ("src", "relation", "dst", "properties", "evidence_memory_ids", "valid_from", "valid_to", "retire_reason")

MAX_DISPLAY_NAME = 256
MAX_NATIVE_ID = 1024
MAX_ALIASES = 32
MAX_PROPERTIES = 64
MAX_PROPERTY_KEY = 128
MAX_PROPERTY_VALUE = 4096
MAX_MEMORY_IDS = 128
MAX_MEMORY_ID = 256
MAX_NOTE = 4096
MAX_BATCH_ITEMS = 2000

_meter = metrics.get_meter("memory_mcp.kg")
_writes = _meter.create_counter("kg_writes_total", description="Knowledge graph writes")
_errors = _meter.create_counter("kg_validation_errors_total", description="Knowledge graph validation errors")


def count_error(code: str) -> None:
    _errors.add(1, {"code": code})


def _union(a: list, b: list | None) -> list:
    return list(dict.fromkeys([*a, *(b or [])]))


def _check_iso(value: str | None, field: str) -> None:
    if value is None:
        return
    try:
        iso_to_ts(value)
    except (TypeError, ValueError):
        raise KGError("invalid_native_id", f"'{field}' must be an ISO-8601 timestamp, got {value!r}", field=field)


def _check_interval(valid_from: str | None, valid_to: str | None) -> None:
    _check_iso(valid_from, "valid_from")
    _check_iso(valid_to, "valid_to")
    if valid_to is None:
        return
    if valid_from is None:
        raise KGError("invalid_native_id", "'valid_from' is required when 'valid_to' is given", field="valid_from")
    if iso_to_ts(valid_from) >= iso_to_ts(valid_to):
        raise KGError("invalid_native_id", "'valid_from' must be earlier than 'valid_to'", field="valid_to")


def _check_str_list(values, field: str, max_count: int, max_len: int) -> None:
    if not values:
        return
    if len(values) > max_count:
        raise KGError("invalid_native_id", f"at most {max_count} {field} allowed", field=field)
    for v in values:
        if not isinstance(v, str):
            raise KGError("invalid_native_id", f"each {field} entry must be a string, got {type(v).__name__}",
                          field=field)
        if len(v) > max_len:
            raise KGError("invalid_native_id", f"each {field} entry must be at most {max_len} characters", field=field)


def _check_properties(properties, field: str = "properties") -> None:
    if not properties:
        return
    if not isinstance(properties, dict):
        raise KGError("invalid_native_id", f"{field} must be an object, got {type(properties).__name__}", field=field)
    if len(properties) > MAX_PROPERTIES:
        raise KGError("invalid_native_id", f"at most {MAX_PROPERTIES} {field} allowed", field=field)
    for k, v in properties.items():
        if not isinstance(k, str) or len(k) > MAX_PROPERTY_KEY:
            raise KGError("invalid_native_id", f"each {field} key must be a string of at most {MAX_PROPERTY_KEY} "
                          "characters", field=field)
        if v is not None and len(str(v)) > MAX_PROPERTY_VALUE:
            raise KGError("invalid_native_id", f"each {field} value must be at most {MAX_PROPERTY_VALUE} characters",
                          field=field)


def _check_caps(native_id, display_name, aliases, properties=None, memory_ids=None) -> None:
    if isinstance(native_id, str) and len(native_id) > MAX_NATIVE_ID:
        raise KGError("invalid_native_id", f"native_id exceeds {MAX_NATIVE_ID} characters", field="native_id")
    if isinstance(display_name, str) and len(display_name) > MAX_DISPLAY_NAME:
        raise KGError("invalid_native_id", f"display_name exceeds {MAX_DISPLAY_NAME} characters", field="display_name")
    _check_str_list(aliases, "aliases", MAX_ALIASES, MAX_DISPLAY_NAME)
    _check_properties(properties)
    _check_str_list(memory_ids, "memory_ids", MAX_MEMORY_IDS, MAX_MEMORY_ID)


def _check_keys(item: dict, allowed: tuple[str, ...]) -> None:
    for k in item:
        if k not in allowed:
            raise KGError("invalid_native_id", f"unknown key {k!r}; allowed keys: {', '.join(allowed)}", field=k)


class KGService:
    def __init__(self, registry, store, dup_threshold: float = 0.90):
        self.reg = registry
        self.store = store
        self.dup_threshold = dup_threshold

    # ── entities ─────────────────────────────────────────────────────────────
    def _prepare_entity(self, graph, provider, type, native_id, display_name=None, aliases=None, properties=None,
                        memory_ids=None, agent="claude-code", valid_from=None, valid_to=None) -> Entity:
        self.reg.require_graph(graph)
        _check_caps(native_id, display_name, aliases, properties, memory_ids)
        self.reg.require_provider(provider)
        td = self.reg.resolve_type(provider, type)
        nid = normalize_native_id(provider, td, native_id)
        _check_interval(valid_from, valid_to)
        now = now_iso()
        return Entity(key=make_key(provider, nid), provider=provider, type=td.type, kind=td.kind, native_id=nid,
                      display_name=display_name or nid, aliases=_union([], aliases), properties=dict(properties or {}),
                      memory_ids=_union([], memory_ids), agent=agent, created_at=now, updated_at=now,
                      valid_from=valid_from or now, valid_to=valid_to)

    def _write_entity(self, graph: str, new: Entity) -> tuple[Entity, str]:
        old = self.store.get_entity(graph, new.key)
        if old is None:
            status = "retired" if new.valid_to else "created"
            return self.store.upsert_entity(graph, new), status
        props = dict(old.properties)
        for k, v in new.properties.items():
            if v is None:
                props.pop(k, None)
            else:
                props[k] = v
        if new.valid_to:
            status, valid_to = "retired", new.valid_to
        elif old.valid_to:
            status, valid_to = "revived", None
        else:
            status, valid_to = "updated", None
        merged = replace(old, type=new.type, kind=new.kind, display_name=new.display_name,
                         aliases=_union(old.aliases, new.aliases), properties=props,
                         memory_ids=_union(old.memory_ids, new.memory_ids), agent=new.agent,
                         updated_at=new.updated_at, valid_to=valid_to)
        # Re-check caps after merge: _union/dict merge can grow collections past input limits across updates.
        _check_str_list(merged.aliases, "aliases", MAX_ALIASES, MAX_DISPLAY_NAME)
        _check_properties(merged.properties)
        _check_str_list(merged.memory_ids, "memory_ids", MAX_MEMORY_IDS, MAX_MEMORY_ID)
        return self.store.upsert_entity(graph, merged), status

    def _duplicates(self, graph: str, e: Entity) -> list[dict]:
        vec = self.store.embedder.embed(f"{e.display_name} {' '.join(e.aliases)} {e.type}")
        hits = self.store.search_entities(graph, vec, EntityFilter(kind=e.kind), 5)
        return [{**h.compact(), "score": round(s, 3)} for h, s in hits if h.key != e.key and s >= self.dup_threshold]

    def upsert_entity(self, graph, provider, type, native_id, display_name=None, aliases=None, properties=None,
                      memory_ids=None, agent="claude-code", valid_from=None, valid_to=None) -> dict:
        new = self._prepare_entity(graph, provider, type, native_id, display_name, aliases, properties,
                                   memory_ids, agent, valid_from, valid_to)
        ent, status = self._write_entity(graph, new)
        _writes.add(1, {"graph": graph, "op": "upsert_entity"})
        out = {"key": ent.key, "status": status, "type": ent.type, "kind": ent.kind}
        if status == "created":
            dups = self._duplicates(graph, ent)
            if dups:
                out["possible_duplicates"] = dups
        return out

    def _require_entity(self, graph: str, key: str, field: str, current: bool, pending: dict | None = None) -> Entity:
        e = (pending or {}).get(key) or self.store.get_entity(graph, key)
        if e is None or (current and e.valid_to is not None):
            state = "does not exist" if e is None else "is retired"
            raise KGError("endpoint_not_found", f"{field} '{key}' {state} in graph '{graph}'", field=field,
                          suggestions=["kg_resolve to find the right key"])
        return e

    # ── edges ────────────────────────────────────────────────────────────────
    def _check_edge(self, graph, src, relation, dst, current=True, pending=None):
        if "::" in src or "::" in dst:
            raise KGError("cross_graph_edge", "Edges must stay within one graph; use kg_xref to reference another graph",
                          field="dst" if "::" in dst else "src")
        rel = self.reg.relation(relation)
        if rel.cls != "topology":
            raise KGError("relation_class_mismatch", f"'{relation}' is a reference relation; use kg_xref", field="relation")
        s = self._require_entity(graph, src, "src", current, pending)
        d = self._require_entity(graph, dst, "dst", current, pending)
        self.reg.check_kinds(rel, s.kind, d.kind)
        return rel

    def _write_edge(self, graph, src, relation, dst, properties=None, evidence_memory_ids=None, agent="claude-code",
                    valid_from=None, valid_to=None, retire_reason=None) -> dict:
        now = now_iso()
        _check_properties(properties)
        _check_str_list(evidence_memory_ids, "evidence_memory_ids", MAX_MEMORY_IDS, MAX_MEMORY_ID)
        if valid_to:  # historical record
            e = Edge(src=src, relation=relation, dst=dst, properties=dict(properties or {}),
                     evidence_memory_ids=_union([], evidence_memory_ids), agent=agent, created_at=now,
                     valid_from=valid_from or now, valid_to=valid_to, retire_reason=retire_reason)
            self.store.put_edge(graph, e)
            return {"src": src, "relation": relation, "dst": dst, "status": "recorded", "valid_from": e.valid_from}
        cur = self.store.current_edge(graph, src, relation, dst)
        if cur:
            e = replace(cur, properties={**cur.properties, **(properties or {})},
                        evidence_memory_ids=_union(cur.evidence_memory_ids, evidence_memory_ids), agent=agent)
            # Re-check after merge: repeated links can grow these past input limits.
            _check_properties(e.properties)
            _check_str_list(e.evidence_memory_ids, "evidence_memory_ids", MAX_MEMORY_IDS, MAX_MEMORY_ID)
            status = "updated"
        else:
            e = Edge(src=src, relation=relation, dst=dst, properties=dict(properties or {}),
                     evidence_memory_ids=_union([], evidence_memory_ids), agent=agent, created_at=now,
                     valid_from=valid_from or now)
            status = "created"
        self.store.put_edge(graph, e)
        return {"src": src, "relation": relation, "dst": dst, "status": status, "valid_from": e.valid_from}

    def link(self, graph, src, relation, dst, properties=None, evidence_memory_ids=None, agent="claude-code",
             valid_from=None) -> dict:
        self.reg.require_graph(graph)
        _check_iso(valid_from, "valid_from")
        self._check_edge(graph, src, relation, dst)
        _writes.add(1, {"graph": graph, "op": "link"})
        return self._write_edge(graph, src, relation, dst, properties, evidence_memory_ids, agent, valid_from)

    def unlink(self, graph, src, relation, dst, reason) -> dict:
        self.reg.require_graph(graph)
        cur = self.store.current_edge(graph, src, relation, dst)
        if cur is None:
            raise KGError("not_found", f"No current edge {src} -{relation}-> {dst}", field="relation")
        now = now_iso()
        self.store.put_edge(graph, replace(cur, valid_to=now, retire_reason=reason))
        _writes.add(1, {"graph": graph, "op": "unlink"})
        return {"src": src, "relation": relation, "dst": dst, "status": "retired", "valid_to": now}

    def retire_entity(self, graph, key, reason) -> dict:
        self.reg.require_graph(graph)
        e = self.store.get_entity(graph, key)
        if e is None or e.valid_to is not None:
            raise KGError("not_found", f"No current entity '{key}' in graph '{graph}'", field="key")
        now = now_iso()
        edges = self.store.edges_from(graph, [key]) + self.store.edges_to(graph, [key])
        for ed in edges:
            self.store.put_edge(graph, replace(ed, valid_to=now, retire_reason=reason))
        self.store.upsert_entity(graph, replace(e, valid_to=now, updated_at=now))
        _writes.add(1, {"graph": graph, "op": "retire_entity"})
        return {"key": key, "status": "retired", "edges_retired": len(edges)}

    # ── batch ────────────────────────────────────────────────────────────────
    def batch(self, graph, entities, edges, agent="claude-code") -> dict:
        self.reg.require_graph(graph)
        n_items = len(entities or []) + len(edges or [])
        if n_items > MAX_BATCH_ITEMS:
            raise KGError("invalid_native_id",
                          f"batch has {n_items} items; at most {MAX_BATCH_ITEMS} (entities + edges) allowed",
                          field=None)
        errors, prepared = [], []
        for i, item in enumerate(entities or []):
            try:
                # Shape validation: must be dict
                if not isinstance(item, dict):
                    raise KGError("invalid_native_id", f"entities[{i}] must be an object", field=None)
                _check_keys(item, ENTITY_FIELDS + ("agent",))
                # Validate required string fields
                for field in ("provider", "type", "native_id"):
                    val = item.get(field)
                    if val is not None and not isinstance(val, str):
                        raise KGError("invalid_native_id", f"{field} must be a string, got {type(val).__name__}", field=field)
                # Validate optional string fields
                for field in ("display_name",):
                    val = item.get(field)
                    if val is not None and not isinstance(val, str):
                        raise KGError("invalid_native_id", f"{field} must be a string, got {type(val).__name__}", field=field)
                prepared.append(self._prepare_entity(graph, agent=item.get("agent", agent),
                                                     **{k: item.get(k) for k in ENTITY_FIELDS}))
            except KGError as ex:
                errors.append({"item": f"entities[{i}]", **ex.to_dict()})
            except (TypeError, AttributeError, ValueError) as ex:
                errors.append({"item": f"entities[{i}]", "error": "invalid_native_id", "message": str(ex),
                               "field": None, "suggestions": []})
        pending = {e.key: e for e in prepared}
        for i, item in enumerate(edges or []):
            try:
                # Shape validation: must be dict
                if not isinstance(item, dict):
                    raise KGError("endpoint_not_found", f"edges[{i}] must be an object", field=None)
                _check_keys(item, EDGE_FIELDS + ("agent",))
                # Validate required string fields
                for field in ("src", "dst", "relation"):
                    val = item.get(field)
                    if val is None:
                        if field == "relation":
                            raise KGError("unknown_relation", f"missing {field!r}", field=field)
                        else:
                            raise KGError("endpoint_not_found", f"missing {field!r}", field=field)
                    if not isinstance(val, str):
                        if field == "relation":
                            raise KGError("unknown_relation", f"{field} must be a string, got {type(val).__name__}", field=field)
                        else:
                            raise KGError("endpoint_not_found", f"{field} must be a string, got {type(val).__name__}", field=field)
                _check_interval(item.get("valid_from"), item.get("valid_to"))
                _check_properties(item.get("properties"))
                _check_str_list(item.get("evidence_memory_ids"), "evidence_memory_ids", MAX_MEMORY_IDS, MAX_MEMORY_ID)
                self._check_edge(graph, item["src"], item["relation"], item["dst"],
                                 current=not item.get("valid_to"), pending=pending)
            except KGError as ex:
                errors.append({"item": f"edges[{i}]", **ex.to_dict()})
            except (TypeError, AttributeError, KeyError, ValueError) as ex:
                errors.append({"item": f"edges[{i}]", "error": "endpoint_not_found", "message": str(ex),
                               "field": None, "suggestions": []})
        if errors:
            for er in errors:
                count_error(er["error"])
            return {"status": "rejected", "written": [], "errors": errors}
        written = []
        try:
            for e in prepared:
                ent, status = self._write_entity(graph, e)
                written.append({"key": ent.key, "status": status})
            for item in edges or []:
                written.append(self._write_edge(graph, agent=item.get("agent", agent),
                                                **{k: item.get(k) for k in EDGE_FIELDS}))
        except Exception as ex:  # infrastructure failure mid-write; retry is safe (deterministic ids)
            return {"status": "partial", "written": written, "failed_at": len(written), "error": str(ex)}
        _writes.add(len(written), {"graph": graph, "op": "batch"})
        return {"status": "ok", "written": written}

    # ── cross-graph references ───────────────────────────────────────────────
    def xref(self, src, relation, dst, note, evidence_memory_ids=None, agent="claude-code") -> dict:
        rel = self.reg.relation(relation)
        if rel.cls != "reference":
            raise KGError("relation_class_mismatch", f"'{relation}' is a topology relation; use kg_link", field="relation")
        if isinstance(note, str) and len(note) > MAX_NOTE:
            raise KGError("invalid_native_id", f"note exceeds {MAX_NOTE} characters", field="note")
        _check_str_list(evidence_memory_ids, "evidence_memory_ids", MAX_MEMORY_IDS, MAX_MEMORY_ID)
        for fq, field in ((src, "src"), (dst, "dst")):
            g, k = split_fq(fq)
            self.reg.require_graph(g)
            self._require_entity(g, k, field, current=False)
        old = self.store.get_xref(src, relation, dst)
        x = XRef(src=src, relation=relation, dst=dst, note=note,
                 evidence_memory_ids=_union(old.evidence_memory_ids if old else [], evidence_memory_ids),
                 agent=agent, created_at=old.created_at if old else now_iso())
        # Re-check after merge: repeated xrefs can grow evidence past the input limit.
        _check_str_list(x.evidence_memory_ids, "evidence_memory_ids", MAX_MEMORY_IDS, MAX_MEMORY_ID)
        self.store.put_xref(x)
        _writes.add(1, {"graph": src.split("::", 1)[0], "op": "xref"})
        return {"src": src, "relation": relation, "dst": dst, "status": "updated" if old else "created"}

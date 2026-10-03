"""Knowledge-graph data model, keys, time helpers and errors."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

KG_NAMESPACE = uuid.UUID("6f1d3c52-2f0e-4c7a-9a43-0d6c4b3f2a11")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_to_ts(s: str | None) -> float | None:
    if s is None:
        return None
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def is_current(valid_from: str, valid_to: str | None, at_ts: float) -> bool:
    vf = iso_to_ts(valid_from)
    vt = iso_to_ts(valid_to)
    return vf <= at_ts and (vt is None or vt > at_ts)


def make_key(provider: str, native_id: str) -> str:
    return f"{provider}:{native_id}"


def fq_key(graph: str, key: str) -> str:
    return f"{graph}::{key}"


def split_fq(fq: str) -> tuple[str, str]:
    if "::" not in fq:
        raise KGError("invalid_native_id", f"'{fq}' is not a fully qualified key ({{graph}}::{{key}})", field="key")
    graph, key = fq.split("::", 1)
    return graph, key


def entity_point_id(key: str) -> str:
    return str(uuid.uuid5(KG_NAMESPACE, key))


def edge_point_id(src: str, relation: str, dst: str, valid_from: str) -> str:
    return str(uuid.uuid5(KG_NAMESPACE, f"{src}|{relation}|{dst}|{valid_from}"))


def xref_point_id(src: str, relation: str, dst: str) -> str:
    return str(uuid.uuid5(KG_NAMESPACE, f"{src}|{relation}|{dst}"))


class KGError(Exception):
    def __init__(self, code: str, message: str, field: str | None = None, suggestions: list[str] | None = None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field
        self.suggestions = suggestions or []

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "field": self.field, "suggestions": self.suggestions}


def _with_ts(d: dict) -> dict:
    d["valid_from_ts"] = iso_to_ts(d["valid_from"])
    d["valid_to_ts"] = iso_to_ts(d["valid_to"])
    return d


def _strip_ts(p: dict) -> dict:
    return {k: v for k, v in p.items() if k not in ("valid_from_ts", "valid_to_ts")}


@dataclass
class Entity:
    key: str
    provider: str
    type: str
    kind: str
    native_id: str
    display_name: str
    aliases: list[str] = field(default_factory=list)
    properties: dict = field(default_factory=dict)
    memory_ids: list[str] = field(default_factory=list)
    agent: str = "claude-code"
    created_at: str = ""
    updated_at: str = ""
    valid_from: str = ""
    valid_to: str | None = None

    def to_payload(self) -> dict:
        return _with_ts(asdict(self))

    @classmethod
    def from_payload(cls, p: dict) -> "Entity":
        return cls(**_strip_ts(p))

    def compact(self, graph: str | None = None) -> dict:
        out = {"graph": graph} if graph else {}
        out.update({"key": self.key, "display_name": self.display_name, "kind": self.kind,
                    "type_short": self.type.split("/")[-1].split("::")[-1]})
        return out


@dataclass
class Edge:
    src: str
    relation: str
    dst: str
    properties: dict = field(default_factory=dict)
    evidence_memory_ids: list[str] = field(default_factory=list)
    agent: str = "claude-code"
    created_at: str = ""
    valid_from: str = ""
    valid_to: str | None = None
    retire_reason: str | None = None

    def to_payload(self) -> dict:
        return _with_ts(asdict(self))

    @classmethod
    def from_payload(cls, p: dict) -> "Edge":
        return cls(**_strip_ts(p))


@dataclass
class XRef:
    src: str  # fully qualified
    relation: str
    dst: str  # fully qualified
    note: str
    evidence_memory_ids: list[str] = field(default_factory=list)
    agent: str = "claude-code"
    created_at: str = ""
    valid_to: str | None = None
    dangling: bool = False

    def to_payload(self) -> dict:
        d = asdict(self)
        d["src_graph"] = self.src.split("::", 1)[0]
        d["dst_graph"] = self.dst.split("::", 1)[0]
        return d

    @classmethod
    def from_payload(cls, p: dict) -> "XRef":
        return cls(**{k: v for k, v in p.items() if k not in ("src_graph", "dst_graph")})

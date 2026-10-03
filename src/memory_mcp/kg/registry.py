"""Graph, relation and type registry loaded from YAML/JSON config."""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from memory_mcp.kg.models import KGError

PROVIDERS = ("azure", "aws", "gcp", "vmware", "hyperv", "cloudflare", "kubernetes", "grafana", "prometheus",
             "netbox", "logical")
KINDS = ("compute", "container", "network", "dns", "edge", "database", "storage", "identity",
         "security", "secret", "observability", "messaging", "analytics", "ai", "integration",
         "org", "host", "work", "inventory", "other")
GRAPH_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_PKG = Path(__file__).parent


def suggest(word: str, choices) -> list[str]:
    return difflib.get_close_matches(word, list(choices), n=3, cutoff=0.5)


@dataclass(frozen=True)
class RelationDef:
    name: str
    inverse: str
    cls: str  # "topology" | "reference"
    impact: str  # "propagates" | "none"
    src_kinds: frozenset[str] | None
    dst_kinds: frozenset[str] | None


@dataclass(frozen=True)
class TypeDef:
    provider: str
    type: str
    kind: str
    aliases: tuple[str, ...] = ()


class Registry:
    def __init__(self, graphs: dict[str, dict], relations: dict[str, RelationDef],
                 types: dict[str, dict[str, TypeDef]]):
        self.graphs = graphs
        self.relations = relations
        self.types = types  # provider -> {native type -> TypeDef}
        self._build_type_index()

    @classmethod
    def load(cls, config_dir: Path | None = None, catalog_dir: Path | None = None) -> "Registry":
        config_dir = Path(config_dir) if config_dir else _PKG / "config"
        catalog_dir = Path(catalog_dir) if catalog_dir else _PKG / "catalog"
        graphs = (yaml.safe_load((config_dir / "kg_graphs.yaml").read_text()) or {}).get("graphs") or {}
        for gid in graphs:
            if not GRAPH_ID_RE.match(gid):
                raise ValueError(f"invalid graph id in kg_graphs.yaml: {gid!r}")
        raw_rel = (yaml.safe_load((config_dir / "kg_relations.yaml").read_text()) or {}).get("relations") or {}
        relations = {
            name: RelationDef(
                name=name, inverse=r["inverse"], cls=r.get("class", "topology"),
                impact=r.get("impact", "none"),
                src_kinds=frozenset(r["src_kinds"]) if r.get("src_kinds") else None,
                dst_kinds=frozenset(r["dst_kinds"]) if r.get("dst_kinds") else None,
            )
            for name, r in raw_rel.items()
        }
        types: dict[str, dict[str, TypeDef]] = {p: {} for p in PROVIDERS}
        if catalog_dir.is_dir():
            for f in sorted(catalog_dir.glob("*.json")):
                doc = json.loads(f.read_text())
                prov = doc["provider"]
                for t in doc["types"]:
                    types.setdefault(prov, {})[t["type"]] = TypeDef(prov, t["type"], t["kind"], tuple(t.get("aliases", [])))
        return cls(graphs, relations, types)

    def _build_type_index(self) -> None:
        """Placeholder hook; Task 4 fills in alias/case-insensitive indexes."""

    def require_graph(self, graph: str) -> None:
        if graph not in self.graphs:
            raise KGError("unknown_graph", f"Unknown graph '{graph}'", field="graph",
                          suggestions=suggest(graph, self.graphs))

    def graph_ids(self, graphs: str | list[str]) -> list[str]:
        if graphs == "*":
            return sorted(self.graphs)
        ids = [graphs] if isinstance(graphs, str) else list(graphs)
        for g in ids:
            self.require_graph(g)
        return ids

    def require_provider(self, provider: str) -> None:
        if provider not in PROVIDERS:
            raise KGError("unknown_provider", f"Unknown provider '{provider}'", field="provider",
                          suggestions=suggest(provider, PROVIDERS))

    def relation(self, name: str) -> RelationDef:
        r = self.relations.get(name)
        if r is None:
            raise KGError("unknown_relation", f"Unknown relation '{name}'", field="relation",
                          suggestions=suggest(name, self.relations))
        return r

    def inverse_label(self, name: str) -> str:
        return self.relation(name).inverse

    def propagating_relations(self) -> list[str]:
        return sorted(n for n, r in self.relations.items() if r.impact == "propagates")

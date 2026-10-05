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
K8S_CRD_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+/[A-Z][A-Za-z0-9]+$")
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
        self._alias: dict[str, dict[str, TypeDef]] = {}
        self._lower: dict[str, dict[str, TypeDef]] = {}
        for prov, types in self.types.items():
            self._alias[prov] = {a: td for td in types.values() for a in td.aliases}
            self._lower[prov] = {t.lower(): td for t, td in types.items()}

    def resolve_type(self, provider: str, type_str: str) -> TypeDef:
        self.require_provider(provider)
        types = self.types.get(provider, {})
        td = types.get(type_str) or self._alias[provider].get(type_str) or self._lower[provider].get(type_str.lower())
        if td:
            return td
        if provider == "kubernetes" and K8S_CRD_RE.match(type_str):
            known_groups = {t.split("/")[0] for t in types}
            if type_str.split("/")[0] not in known_groups:
                return TypeDef("kubernetes", type_str, "container")
        choices = list(types) + list(self._alias[provider])
        raise KGError("unknown_type", f"Unknown {provider} type '{type_str}'", field="type",
                      suggestions=suggest(type_str, choices))

    def check_kinds(self, rel: RelationDef, src_kind: str, dst_kind: str) -> None:
        if rel.src_kinds and src_kind not in rel.src_kinds:
            raise KGError("kind_constraint", f"'{rel.name}' source must be one of {sorted(rel.src_kinds)}, got '{src_kind}'",
                          field="src")
        if rel.dst_kinds and dst_kind not in rel.dst_kinds:
            raise KGError("kind_constraint", f"'{rel.name}' target must be one of {sorted(rel.dst_kinds)}, got '{dst_kind}'",
                          field="dst")

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

    def describe_catalog(self, provider: str | None = None, kind: str | None = None,
                         search: str | None = None, max_types: int = 200) -> dict:
        """Surface the supported providers, type catalog, relations, graphs and kinds so agents
        don't have to guess (addresses the KG Unsupported Resource Types Tracker open question).
        Optionally filter the type listing by provider, kind, or a case-insensitive substring."""
        if provider is not None:
            self.require_provider(provider)
        s = search.lower() if search else None
        providers_out = {}
        truncated = {}
        for prov in (([provider] if provider else list(PROVIDERS))):
            rows = []
            for td in self.types.get(prov, {}).values():
                if kind and td.kind != kind:
                    continue
                if s and s not in td.type.lower() and not any(s in a.lower() for a in td.aliases):
                    continue
                rows.append({"type": td.type, "kind": td.kind, "aliases": list(td.aliases)})
            rows.sort(key=lambda r: r["type"])
            if len(rows) > max_types:
                truncated[prov] = len(rows)
                rows = rows[:max_types]
            providers_out[prov] = rows
        out = {
            "providers": sorted(PROVIDERS),
            "kinds": sorted(KINDS),
            "graphs": sorted(self.graphs),
            "relations": {
                n: {"inverse": r.inverse, "class": r.cls, "impact": r.impact,
                    "src_kinds": sorted(r.src_kinds) if r.src_kinds else None,
                    "dst_kinds": sorted(r.dst_kinds) if r.dst_kinds else None}
                for n, r in sorted(self.relations.items())
            },
            "types": providers_out,
            "type_counts": {p: len(self.types.get(p, {})) for p in sorted(PROVIDERS)},
        }
        if truncated:
            out["truncated"] = {p: {"shown": max_types, "total": n} for p, n in truncated.items()}
        return out

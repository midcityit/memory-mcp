"""Knowledge graph package: build the registry/store/service/query stack."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opentelemetry import metrics

from memory_mcp.kg.query import KGQuery
from memory_mcp.kg.registry import Registry
from memory_mcp.kg.service import KGService
from memory_mcp.kg.store import EntityFilter, QdrantGraphStore

_meter = metrics.get_meter("memory_mcp.kg")
_gauge_registered = False


@dataclass
class KG:
    registry: Registry
    store: QdrantGraphStore
    service: KGService
    query: KGQuery


def _memory_lookup(memory_store):
    def lookup(memory_id: str) -> dict | None:
        try:
            r = memory_store.get(memory_id)
        except Exception:  # malformed ids raise in qdrant retrieve; treat as missing
            return None
        return {"id": r.id, "name": r.name, "type": r.type} if r else None
    return lookup


def build_kg(client, embedder, memory_store=None, config_dir: str | None = None,
             dup_threshold: float = 0.90, max_chars: int = 6000) -> KG:
    if config_dir:
        d = Path(config_dir)
        registry = Registry.load(config_dir=d, catalog_dir=d / "catalog" if (d / "catalog").is_dir() else None)
    else:
        registry = Registry.load()
    store = QdrantGraphStore(client, embedder)
    for g in registry.graphs:
        store.ensure_graph(g)
    store.ensure_xrefs()
    service = KGService(registry, store, dup_threshold=dup_threshold)
    query = KGQuery(registry, store, memory_lookup=_memory_lookup(memory_store) if memory_store else None,
                    max_chars=max_chars)

    global _gauge_registered
    if not _gauge_registered:
        def observe(_options):
            for g in registry.graphs:
                try:
                    yield metrics.Observation(store.count_entities(g, EntityFilter()), {"graph": g})
                except Exception:
                    pass
        _meter.create_observable_gauge("kg_entities", callbacks=[observe], description="Current kg entities per graph")
        _gauge_registered = True
    return KG(registry, store, service, query)

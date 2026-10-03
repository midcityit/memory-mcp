"""MCP bindings for the knowledge graph. Thin: validation lives in service/query."""
from __future__ import annotations

import time

from opentelemetry import metrics

from memory_mcp.kg.models import KGError
from memory_mcp.kg.service import count_error

_dur = metrics.get_meter("memory_mcp.kg").create_histogram(
    "kg_query_duration_seconds", unit="s", description="kg tool latency")


def _call(tool: str, graph, fn, *args, **kwargs) -> dict:
    t0 = time.monotonic()
    try:
        return fn(*args, **kwargs)
    except KGError as e:
        count_error(e.code)
        return e.to_dict()
    finally:
        _dur.record(time.monotonic() - t0, {"tool": tool, "graph": graph if isinstance(graph, str) else ",".join(graph or [])})


def register(mcp, service, query) -> None:
    query_engine = query  # `query` is also kg_resolve's argument name; use query_engine inside the tools

    @mcp.tool()
    def kg_upsert_entity(graph: str, provider: str, type: str, native_id: str, display_name: str | None = None,
                         aliases: list[str] | None = None, properties: dict | None = None,
                         memory_ids: list[str] | None = None, agent: str = "claude-code") -> dict:
        """Create/update a knowledge-graph entity. provider: azure|aws|gcp|vmware|hyperv|cloudflare|kubernetes|logical.
        type: provider-native (e.g. Microsoft.ContainerService/managedClusters, AWS::EC2::VPC, apps/Deployment) or a
        Terraform type (azurerm_kubernetes_cluster). native_id: ARM ID / ARN / GCP full name /
        k8s '{cluster}/{ns|_cluster}/{group}/{Kind}/{name}' / Jira key. Call kg_resolve first to reuse keys."""
        return _call("kg_upsert_entity", graph, service.upsert_entity, graph, provider, type, native_id,
                     display_name, aliases, properties, memory_ids, agent)

    @mcp.tool()
    def kg_link(graph: str, src: str, relation: str, dst: str, properties: dict | None = None,
                evidence_memory_ids: list[str] | None = None, agent: str = "claude-code",
                valid_from: str | None = None) -> dict:
        """Create/refresh a topology edge between two current entities in one graph (e.g. runs_on, routes_to,
        depends_on, tracked_in). Only record verified topology; attach evidence_memory_ids."""
        return _call("kg_link", graph, service.link, graph, src, relation, dst, properties, evidence_memory_ids,
                     agent, valid_from)

    @mcp.tool()
    def kg_unlink(graph: str, src: str, relation: str, dst: str, reason: str) -> dict:
        """Soft-retire a current edge (history is kept). Use when something moves or is reconfigured."""
        return _call("kg_unlink", graph, service.unlink, graph, src, relation, dst, reason)

    @mcp.tool()
    def kg_retire_entity(graph: str, key: str, reason: str) -> dict:
        """Soft-retire an entity and all its current edges (decommission). Never hard-delete via agents."""
        return _call("kg_retire_entity", graph, service.retire_entity, graph, key, reason)

    @mcp.tool()
    def kg_batch(graph: str, entities: list[dict] | None = None, edges: list[dict] | None = None,
                 agent: str = "claude-code") -> dict:
        """Record a whole topology at once (e.g. after a verified terraform apply). Everything is validated first;
        any error rejects the batch. Entity items: provider,type,native_id,display_name,aliases,properties,
        memory_ids,valid_from,valid_to. Edge items: src,relation,dst,properties,evidence_memory_ids,valid_from,
        valid_to,retire_reason (valid_to records historical edges). Safe to re-run."""
        return _call("kg_batch", graph, service.batch, graph, entities or [], edges or [], agent)

    @mcp.tool()
    def kg_xref(src: str, relation: str, dst: str, note: str, evidence_memory_ids: list[str] | None = None,
                agent: str = "claude-code") -> dict:
        """Cross-graph reference between fully qualified keys ('{graph}::{key}'). relation: pattern_from,
        lessons_from, similar_to, supersedes_approach_of. Use when one graph's work reuses another's pattern."""
        return _call("kg_xref", None, service.xref, src, relation, dst, note, evidence_memory_ids, agent)

    @mcp.tool()
    def kg_resolve(query: str, graphs: str | list[str] = "*", provider: str | None = None, kind: str | None = None,
                   type: str | None = None, include_retired: bool = False, limit: int = 10) -> dict:
        """Find entity keys by name, alias, native id or description. Call before writing or querying."""
        return _call("kg_resolve", graphs, query_engine.resolve, query, graphs, provider, kind, type,
                     include_retired, limit)

    @mcp.tool()
    def kg_get_entity(graph: str, key: str, as_of: str | None = None, include_history: bool = False) -> dict:
        """Everything about one entity: edges both directions, linked memories, xrefs, optional history."""
        return _call("kg_get_entity", graph, query_engine.get_entity, graph, key, as_of, include_history)

    @mcp.tool()
    def kg_find(graph: str, provider: str | None = None, kind: str | None = None, type: str | None = None,
                missing_relation: str | None = None, direction: str = "out", include_retired: bool = False,
                limit: int = 100, cursor: str | None = None) -> dict:
        """List entities by filter. missing_relation finds orphans (e.g. workloads without tracked_in).
        has_more=false means the list is complete."""
        return _call("kg_find", graph, query_engine.find, graph, provider, kind, type, missing_relation, direction,
                     include_retired, limit, cursor)

    @mcp.tool()
    def kg_traverse(graph: str, start: str, direction: str = "both", relations: list[str] | None = None,
                    kinds: list[str] | None = None, max_depth: int = 3, as_of: str | None = None,
                    follow_xrefs: bool = False, limit: int = 200) -> dict:
        """Neighborhood subgraph around an entity (BFS, max_depth<=6). as_of='YYYY-MM-DD' shows past topology."""
        return _call("kg_traverse", graph, query_engine.traverse, graph, start, direction, relations, kinds,
                     max_depth, as_of, follow_xrefs, limit)

    @mcp.tool()
    def kg_path(graph: str, src: str, dst: str, relations: list[str] | None = None, max_depth: int = 6) -> dict:
        """Up to 3 shortest paths between two entities (edges followed in either direction)."""
        return _call("kg_path", graph, query_engine.path, graph, src, dst, relations, max_depth)

    @mcp.tool()
    def kg_impact(graph: str, key: str, max_depth: int = 6, as_of: str | None = None) -> dict:
        """Blast radius: everything that depends on this entity, by depth, plus linked Jira/Confluence."""
        return _call("kg_impact", graph, query_engine.impact, graph, key, max_depth, as_of)

    @mcp.tool()
    def kg_overview(graphs: str | list[str] = "*") -> dict:
        """Short orientation per graph: counts, hubs (likely single points of failure), recent changes."""
        return _call("kg_overview", graphs, query_engine.overview, graphs)

    @mcp.tool()
    def kg_related_across(graph: str, key: str, target_graphs: str | list[str] = "*", limit: int = 10) -> dict:
        """Lessons/patterns from other graphs: explicit xrefs plus similar entities with their
        decision/troubleshooting/runbook memories."""
        return _call("kg_related_across", graph, query_engine.related_across, graph, key, target_graphs, limit)

    @mcp.tool()
    def kg_for_memory(memory_id: str, graphs: str | list[str] = "*") -> dict:
        """Entities and edges that reference a memory (memory_ids / evidence_memory_ids)."""
        return _call("kg_for_memory", graphs, query_engine.for_memory, memory_id, graphs)

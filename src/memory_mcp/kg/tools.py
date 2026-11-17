"""MCP bindings for the knowledge graph. Thin: validation lives in service/query, authorization in authz."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from opentelemetry import metrics

from memory_mcp.admin.audit import NullAudit
from memory_mcp.admin.auth import principal_from_ctx
from memory_mcp.admin.models import AuthzError, Principal
from memory_mcp.kg.authz import KGGuard, fq_graph
from memory_mcp.kg.models import KGError
from memory_mcp.kg.service import count_error

_dur = metrics.get_meter("memory_mcp.kg").create_histogram(
    "kg_query_duration_seconds", unit="s", description="kg tool latency")

# No tenants: every check passes, nothing is filtered (direct registration in tests, legacy mode).
_OPEN = KGGuard(None, NullAudit())

Deps = tuple[Any, Any, "KGGuard | None"]  # (service, query, guard)


def _label(graph) -> str:
    return graph if isinstance(graph, str) else ",".join(graph or [])


class _Call:
    """One tool call: the principal, its action (the tool name) and the guard, bound together."""

    def __init__(self, guard: KGGuard, p: Principal | None, action: str):
        self.guard, self.p, self.action = guard, p, action

    def graph(self, graph: str, target: str | None = None) -> None:
        self.guard.graph(self.p, self.action, graph, target)

    def graphs(self, graphs):
        return self.guard.graphs(self.p, self.action, graphs)

    def fq(self, fq: str, target: str | None = None) -> None:
        self.guard.fq(self.p, self.action, fq, target)

    @property
    def visible(self) -> frozenset[str] | None:
        return self.guard.visible(self.p)

    def written(self, graph: str | None, target: str | None, result: dict) -> dict:
        if result.get("status") != "rejected":
            self.guard.written(self.p, self.action, graph, target)
        return result


def _xref_target(src: str, relation: str, dst: str) -> str:
    return f"{src} -{relation}-> {dst}"


def register(mcp, service, query, guard: KGGuard | None = None) -> None:
    """Register the kg tools bound to fixed dependencies."""
    register_resolved(mcp, lambda: (service, query, guard))


def register_resolved(mcp, deps: Callable[[], Deps]) -> None:
    """Register the kg tools; `deps()` supplies (service, query, guard) on every call."""

    def run(ctx: Context, tool: str, graph, body: Callable[[Any, Any, _Call], dict]) -> dict:
        t0 = time.monotonic()
        try:
            service, query, guard = deps()
            guard = guard or _OPEN
            p = principal_from_ctx(ctx) if guard.enforcing else None
            return body(service, query, _Call(guard, p, tool))
        except KGError as e:
            count_error(e.code)
            return e.to_dict()
        except AuthzError as e:
            raise ToolError(e.message) from e
        finally:
            _dur.record(time.monotonic() - t0, {"tool": tool, "graph": _label(graph)})

    @mcp.tool()
    def kg_upsert_entity(ctx: Context, graph: str, provider: str, type: str, native_id: str,
                         display_name: str | None = None, aliases: list[str] | None = None,
                         properties: dict | None = None, memory_ids: list[str] | None = None,
                         agent: str = "claude-code") -> dict:
        """Create/update a knowledge-graph entity. provider: azure|aws|gcp|vmware|hyperv|cloudflare|kubernetes|grafana|prometheus|netbox|logical.
        type: provider-native (e.g. Microsoft.ContainerService/managedClusters, AWS::EC2::VPC, apps/Deployment) or a
        Terraform type (azurerm_kubernetes_cluster). native_id: ARM ID / ARN / GCP full name /
        k8s '{cluster}/{ns|_cluster}/{group}/{Kind}/{name}' / Jira key. Call kg_resolve first to reuse keys."""
        def body(service, _query, c):
            c.graph(graph, native_id)
            out = service.upsert_entity(graph, provider, type, native_id, display_name, aliases, properties,
                                        memory_ids, agent)
            return c.written(graph, out["key"], out)
        return run(ctx, "kg_upsert_entity", graph, body)

    @mcp.tool()
    def kg_link(ctx: Context, graph: str, src: str, relation: str, dst: str, properties: dict | None = None,
                evidence_memory_ids: list[str] | None = None, agent: str = "claude-code",
                valid_from: str | None = None) -> dict:
        """Create/refresh a topology edge between two current entities in one graph (e.g. runs_on, routes_to,
        depends_on, tracked_in). Only record verified topology; attach evidence_memory_ids."""
        target = _xref_target(src, relation, dst)

        def body(service, _query, c):
            c.graph(graph, target)
            return c.written(graph, target, service.link(graph, src, relation, dst, properties,
                                                         evidence_memory_ids, agent, valid_from))
        return run(ctx, "kg_link", graph, body)

    @mcp.tool()
    def kg_unlink(ctx: Context, graph: str, src: str, relation: str, dst: str, reason: str) -> dict:
        """Soft-retire a current edge (history is kept). Use when something moves or is reconfigured."""
        target = _xref_target(src, relation, dst)

        def body(service, _query, c):
            c.graph(graph, target)
            return c.written(graph, target, service.unlink(graph, src, relation, dst, reason))
        return run(ctx, "kg_unlink", graph, body)

    @mcp.tool()
    def kg_retire_entity(ctx: Context, graph: str, key: str, reason: str) -> dict:
        """Soft-retire an entity and all its current edges (decommission). Prefer this over hard delete;
        history is preserved and the entity can be revived by upserting it again."""
        def body(service, _query, c):
            c.graph(graph, key)
            return c.written(graph, key, service.retire_entity(graph, key, reason))
        return run(ctx, "kg_retire_entity", graph, body)

    @mcp.tool()
    def kg_delete_entity(ctx: Context, graph: str, key: str, confirm: bool = False) -> dict:
        """Hard-delete (purge) an entity, its edges, and any cross-graph xrefs touching it. IRREVERSIBLE —
        use kg_retire_entity for normal decommissioning. Requires confirm=true. Use to remove test/erroneous
        entities, or to clean up an endpoint whose xrefs would otherwise be left dangling."""
        def body(service, _query, c):
            c.graph(graph, key)
            return c.written(graph, key, service.delete_entity(graph, key, confirm,
                                                               xref_removable=c.guard.xref_removable(c.p)))
        return run(ctx, "kg_delete_entity", graph, body)

    @mcp.tool()
    def kg_delete_xref(ctx: Context, src: str, relation: str, dst: str) -> dict:
        """Hard-delete a cross-graph reference ('{graph}::{key}' endpoints). Use to remove an erroneous xref or
        clean up one left dangling after both endpoint entities were deleted (previously no delete path existed)."""
        target = _xref_target(src, relation, dst)

        def body(service, _query, c):
            c.fq(src, target)
            c.fq(dst, target)
            return c.written(fq_graph(src), target, service.delete_xref(src, relation, dst))
        return run(ctx, "kg_delete_xref", None, body)

    @mcp.tool()
    def kg_catalog(ctx: Context, provider: str | None = None, kind: str | None = None,
                   search: str | None = None) -> dict:
        """Discover what the graph supports: providers, the (provider -> type/kind/aliases) catalog, relations
        (with their kind constraints), graphs, and valid kinds. Call this BEFORE guessing a (provider, type) pair.
        Filter the type listing by provider, kind, or a case-insensitive substring (e.g. provider='azure',
        search='servicePrincipal')."""
        def body(service, _query, c):
            graphs = c.graphs("*")
            out = service.reg.describe_catalog(provider, kind, search)
            if graphs != "*":
                out["graphs"] = sorted(graphs)
            return out
        return run(ctx, "kg_catalog", None, body)

    @mcp.tool()
    def kg_batch(ctx: Context, graph: str, entities: list[dict] | None = None, edges: list[dict] | None = None,
                 agent: str = "claude-code") -> dict:
        """Record a whole topology at once (e.g. after a verified terraform apply). Everything is validated first;
        any error rejects the batch. Entity items: provider,type,native_id,display_name,aliases,properties,
        memory_ids,valid_from,valid_to. Edge items: src,relation,dst,properties,evidence_memory_ids,valid_from,
        valid_to,retire_reason (valid_to records historical edges). Safe to re-run."""
        def body(service, _query, c):
            c.graph(graph)
            return c.written(graph, None, service.batch(graph, entities or [], edges or [], agent))
        return run(ctx, "kg_batch", graph, body)

    @mcp.tool()
    def kg_xref(ctx: Context, src: str, relation: str, dst: str, note: str,
                evidence_memory_ids: list[str] | None = None, agent: str = "claude-code") -> dict:
        """Cross-graph reference between fully qualified keys ('{graph}::{key}'). relation: pattern_from,
        lessons_from, similar_to, supersedes_approach_of. Use when one graph's work reuses another's pattern."""
        target = _xref_target(src, relation, dst)

        def body(service, _query, c):
            c.fq(src, target)
            c.fq(dst, target)
            return c.written(fq_graph(src), target,
                             service.xref(src, relation, dst, note, evidence_memory_ids, agent))
        return run(ctx, "kg_xref", None, body)

    @mcp.tool()
    def kg_resolve(ctx: Context, query: str, graphs: str | list[str] = "*", provider: str | None = None,
                   kind: str | None = None, type: str | None = None, include_retired: bool = False,
                   limit: int = 10) -> dict:
        """Find entity keys by name, alias, native id or description. Call before writing or querying."""
        def body(_service, q, c):
            return q.resolve(query, c.graphs(graphs), provider, kind, type, include_retired, limit)
        return run(ctx, "kg_resolve", graphs, body)

    @mcp.tool()
    def kg_get_entity(ctx: Context, graph: str, key: str, as_of: str | None = None,
                      include_history: bool = False) -> dict:
        """Everything about one entity: edges both directions, linked memories, xrefs, optional history."""
        def body(_service, q, c):
            c.graph(graph, key)
            return q.get_entity(graph, key, as_of, include_history, visible=c.visible)
        return run(ctx, "kg_get_entity", graph, body)

    @mcp.tool()
    def kg_find(ctx: Context, graph: str, provider: str | None = None, kind: str | None = None,
                type: str | None = None, missing_relation: str | None = None, direction: str = "out",
                include_retired: bool = False, limit: int = 100, cursor: str | None = None) -> dict:
        """List entities by filter. missing_relation finds orphans (e.g. workloads without tracked_in).
        has_more=false means the list is complete."""
        def body(_service, q, c):
            c.graph(graph)
            return q.find(graph, provider, kind, type, missing_relation, direction, include_retired, limit, cursor)
        return run(ctx, "kg_find", graph, body)

    @mcp.tool()
    def kg_traverse(ctx: Context, graph: str, start: str, direction: str = "both",
                    relations: list[str] | None = None, kinds: list[str] | None = None, max_depth: int = 3,
                    as_of: str | None = None, follow_xrefs: bool = False, limit: int = 200) -> dict:
        """Neighborhood subgraph around an entity (BFS, max_depth<=6). as_of='YYYY-MM-DD' shows past topology."""
        def body(_service, q, c):
            c.graph(graph, start)
            return q.traverse(graph, start, direction, relations, kinds, max_depth, as_of, follow_xrefs, limit,
                              visible=c.visible)
        return run(ctx, "kg_traverse", graph, body)

    @mcp.tool()
    def kg_path(ctx: Context, graph: str, src: str, dst: str, relations: list[str] | None = None,
                max_depth: int = 6) -> dict:
        """Up to 3 shortest paths between two entities (edges followed in either direction)."""
        def body(_service, q, c):
            c.graph(graph)
            return q.path(graph, src, dst, relations, max_depth)
        return run(ctx, "kg_path", graph, body)

    @mcp.tool()
    def kg_impact(ctx: Context, graph: str, key: str, max_depth: int = 6, as_of: str | None = None) -> dict:
        """Blast radius: everything that depends on this entity, by depth, plus linked Jira/Confluence."""
        def body(_service, q, c):
            c.graph(graph, key)
            return q.impact(graph, key, max_depth, as_of)
        return run(ctx, "kg_impact", graph, body)

    @mcp.tool()
    def kg_overview(ctx: Context, graphs: str | list[str] = "*") -> dict:
        """Short orientation per graph: counts, hubs (likely single points of failure), recent changes."""
        def body(_service, q, c):
            return q.overview(c.graphs(graphs), visible=c.visible)
        return run(ctx, "kg_overview", graphs, body)

    @mcp.tool()
    def kg_related_across(ctx: Context, graph: str, key: str, target_graphs: str | list[str] = "*",
                          limit: int = 10) -> dict:
        """Lessons/patterns from other graphs: explicit xrefs plus similar entities with their
        decision/troubleshooting/runbook memories."""
        def body(_service, q, c):
            c.graph(graph, key)
            return q.related_across(graph, key, c.graphs(target_graphs), limit, visible=c.visible)
        return run(ctx, "kg_related_across", graph, body)

    @mcp.tool()
    def kg_for_memory(ctx: Context, memory_id: str, graphs: str | list[str] = "*") -> dict:
        """Entities and edges that reference a memory (memory_ids / evidence_memory_ids)."""
        def body(_service, q, c):
            return q.for_memory(memory_id, c.graphs(graphs))
        return run(ctx, "kg_for_memory", graphs, body)

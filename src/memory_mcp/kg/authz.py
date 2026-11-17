"""Per-graph authorization for the knowledge graph, shared by the MCP tools and the /kg routes.

A graph id is a tenant id. Every call names its action (the tool name, or for a REST
route the tool that does the same thing) and the guard authorizes it exactly like a
memory call: ``authorize`` first, so a caller who does not hold a graph gets the same
403 whether or not it exists, then the tenant must be active. Refusals and successful
writes go to the audit sink memory operations use.

With no tenant service (legacy mode) every check passes and nothing is filtered.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from memory_mcp.admin.audit import AuditSink
from memory_mcp.admin.authz import authorize, visible_tenants
from memory_mcp.admin.models import AuthzError, Principal
from memory_mcp.admin.tenants import TenantService
from memory_mcp.kg.models import XRef, is_fq_key


def fq_graph(fq: str) -> str | None:
    """The graph part of a '{graph}::{key}' key, or None if `fq` is not fully qualified."""
    return fq.split("::", 1)[0] if is_fq_key(fq) else None


class KGGuard:
    def __init__(self, tenants: TenantService | None, audit: AuditSink):
        self._tenants = tenants
        self._audit = audit

    @property
    def enforcing(self) -> bool:
        """False in legacy mode (no tenants): no principal is needed and everything is allowed."""
        return self._tenants is not None

    # ── checks ────────────────────────────────────────────────────────────────
    def _check(self, p: Principal, action: str, graph: str) -> None:
        authorize(p, action, graph)  # before any lookup: no existence leak
        t = self._tenants.get(graph)
        if t is not None and t.status != "active":
            raise AuthzError(403, "tenant_disabled", f"Tenant '{graph}' is disabled")
        # An unknown graph held by the caller falls through: the KG reports unknown_graph.

    def _denied(self, p: Principal, action: str, graph: str | None, target: str | None,
                e: AuthzError) -> None:
        outcome = "denied" if 400 <= e.status < 500 else "error"
        self._audit.record(p, action, graph, target, outcome, {"rule": e.code})

    def graph(self, p: Principal, action: str, graph: str, target: str | None = None) -> None:
        """Raise AuthzError (403) unless `p` may perform `action` in `graph`."""
        if not self.enforcing:
            return
        try:
            self._check(p, action, graph)
        except AuthzError as e:
            self._denied(p, action, graph, target, e)
            raise

    def graphs(self, p: Principal, action: str, graphs: str | list[str]) -> str | list[str]:
        """The graphs a multi-graph call may search.

        "*" -> the visible graphs `p` may perform `action` on. A name or list -> every
        named graph must pass, or the whole call is refused. Legacy mode returns `graphs`
        unchanged (the registry expands it as before).
        """
        if not self.enforcing:
            return graphs
        if graphs == "*":
            allowed, first_error = [], None
            for g in visible_tenants(p, self._tenants):
                try:
                    self._check(p, action, g)
                    allowed.append(g)
                except AuthzError as e:
                    first_error = first_error or (g, e)
            if not allowed and first_error is not None:
                # Holds graphs but may not use this tool on any: say so rather than return nothing.
                self._denied(p, action, first_error[0], None, first_error[1])
                raise first_error[1]
            return allowed
        named = [graphs] if isinstance(graphs, str) else list(graphs)
        for g in named:
            self.graph(p, action, g)
        return named

    def fq(self, p: Principal, action: str, fq: str, target: str | None = None) -> None:
        """Authorize the graph of a fully qualified key. A malformed key is left for the KG to reject."""
        g = fq_graph(fq)
        if g is not None:
            self.graph(p, action, g, target)

    def allows(self, p: Principal, action: str, graph: str) -> bool:
        """Whether `p` may perform `action` in `graph`; records nothing."""
        if not self.enforcing:
            return True
        try:
            self._check(p, action, graph)
            return True
        except AuthzError:
            return False

    def xref_removable(self, p: Principal) -> Callable[[XRef], bool] | None:
        """For a hard entity delete: an xref goes only if `p` may delete xrefs in both its graphs.
        None (legacy mode) means every touching xref goes, as before."""
        if not self.enforcing:
            return None

        def removable(x: XRef) -> bool:
            return all(g is not None and self.allows(p, "kg_delete_xref", g)
                       for g in (fq_graph(x.src), fq_graph(x.dst)))
        return removable

    def visible(self, p: Principal) -> frozenset[str] | None:
        """Graphs whose cross-graph references `p` may see; None means all (legacy mode)."""
        if not self.enforcing:
            return None
        return frozenset(visible_tenants(p, self._tenants))

    # ── audit ─────────────────────────────────────────────────────────────────
    def written(self, p: Principal, action: str, graph: str | None, target: str | None,
                detail: dict[str, Any] | None = None) -> None:
        """Record a successful write."""
        if self.enforcing:
            self._audit.record(p, action, graph, target, "allowed", detail)

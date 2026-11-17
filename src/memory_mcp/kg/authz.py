"""Per-graph authorization for the knowledge graph, shared by the MCP tools and the /kg routes.

A graph id is a tenant id. Every call names its action (the tool name, or for a REST
route the tool that does the same thing) and the guard authorizes it exactly like a
memory call: ``authorize`` first, so a caller who does not hold a graph gets the same
403 whether or not it exists, then the tenant must exist and be active. A graph id the
caller lists but that is not a tenant gets that same 403. Refusals and successful
writes go to the audit sink memory operations use.

With no tenant service (legacy mode) every check passes and nothing is filtered.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from memory_mcp.admin.audit import AuditSink
from memory_mcp.admin.authz import authorize, visible_tenants
from memory_mcp.admin.models import AuthzError, Principal
from memory_mcp.admin.tenants import TenantService
from memory_mcp.kg.models import XRef, fq_graph

__all__ = ["KGGuard", "XrefDeletion", "fq_graph"]


def _no_access(graph: str) -> AuthzError:
    # Same code and message as authorize() gives a graph the caller does not hold.
    return AuthzError(403, "tenant_forbidden", f"No access to tenant '{graph}'")


class XrefDeletion:
    """Hard-delete policy for the xrefs touching an entity in `graph`.

    Called by the service once per touching xref: True deletes it, False keeps it
    marked dangling. An xref goes only if the caller may delete xrefs in both its
    graphs. Records, per other graph, how many of its xrefs were removed or kept.
    """

    def __init__(self, guard: "KGGuard", p: Principal, graph: str):
        self._guard, self._p, self._graph = guard, p, graph
        self.affected: dict[str, Counter] = {}

    def __call__(self, x: XRef) -> bool:
        graphs = (fq_graph(x.src), fq_graph(x.dst))
        removable = all(g is not None and self._guard.allows(self._p, "kg_delete_xref", g) for g in graphs)
        for g in {g for g in graphs if g is not None and g != self._graph}:
            self.affected.setdefault(g, Counter())["xrefs_removed" if removable else "xrefs_dangling"] += 1
        return removable


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
        if t is None:
            raise _no_access(graph)
        if t.status != "active":
            raise AuthzError(403, "tenant_disabled", f"Tenant '{graph}' is disabled")

    def _denied(self, p: Principal, action: str, graph: str | None, target: str | None,
                e: AuthzError) -> None:
        outcome = "denied" if 400 <= e.status < 500 else "error"
        self._audit.record(p, action, graph, target, outcome, {"rule": e.code})

    def deny(self, p: Principal, action: str, graph: str | None, target: str | None, e: AuthzError) -> AuthzError:
        """Audit a refusal decided by the caller and return it for raising."""
        if self.enforcing:
            self._denied(p, action, graph, target, e)
        return e

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

    def fq(self, p: Principal, action: str, fq: str, target: str | None = None) -> str | None:
        """Authorize the graph of a fully qualified key and return it.

        Enforcing mode refuses a key that is not fully qualified (400 invalid_key); legacy
        mode leaves it for the KG to reject.
        """
        g = fq_graph(fq)
        if not self.enforcing:
            return g
        if g is None:
            raise self.deny(p, action, None, target, AuthzError(
                400, "invalid_key", f"'{fq}' is not a fully qualified key ({{graph}}::{{key}})"))
        self.graph(p, action, g, target)
        return g

    def allows(self, p: Principal, action: str, graph: str) -> bool:
        """Whether `p` may perform `action` in `graph`; records nothing."""
        if not self.enforcing:
            return True
        try:
            self._check(p, action, graph)
            return True
        except AuthzError:
            return False

    def visible(self, p: Principal) -> frozenset[str] | None:
        """Graphs whose cross-graph references `p` may see; None means all (legacy mode)."""
        if not self.enforcing:
            return None
        return frozenset(visible_tenants(p, self._tenants))

    def xref_deletion(self, p: Principal, graph: str) -> XrefDeletion | None:
        """The hard-delete xref policy; None (legacy mode) deletes every touching xref, as before."""
        return XrefDeletion(self, p, graph) if self.enforcing else None

    # ── results ───────────────────────────────────────────────────────────────
    def catalog(self, p: Principal, action: str, build: Callable[[], dict]) -> dict:
        """Authorize a catalog read, then build it with `graphs` limited to those `p` may use."""
        allowed = self.graphs(p, action, "*")
        catalog = build()
        if allowed != "*":
            catalog["graphs"] = sorted(allowed)
        return catalog

    def scrub(self, p: Principal | None, result: dict) -> dict:
        """Drop `suggestions` naming a graph outside the caller's visible set (also in `errors` items)."""
        if not self.enforcing or not isinstance(result, dict):
            return result
        hidden = {t.id for t in self._tenants.all()} - self.visible(p)

        def clean(d: dict) -> None:
            if isinstance(d.get("suggestions"), list):
                d["suggestions"] = [s for s in d["suggestions"] if s not in hidden]
        clean(result)
        for item in result.get("errors") or ():
            if isinstance(item, dict):
                clean(item)
        return result

    # ── audit ─────────────────────────────────────────────────────────────────
    def written(self, p: Principal, action: str, graphs: str | list[str | None] | None, target: str | None,
                result: dict | None = None, detail: dict[str, Any] | None = None) -> dict | None:
        """Record a successful write, one row per distinct graph; a rejected batch writes nothing.
        Returns `result`."""
        if not self.enforcing or (isinstance(result, dict) and result.get("status") == "rejected"):
            return result
        for g in dict.fromkeys([graphs] if graphs is None or isinstance(graphs, str) else graphs):
            self._audit.record(p, action, g, target, "allowed", detail)
        return result

    def written_deletion(self, p: Principal, action: str, graph: str, target: str, result: dict,
                         deletion: XrefDeletion | None) -> dict:
        """Record a hard delete, plus a row under each other graph whose xrefs it removed or left dangling."""
        self.written(p, action, graph, target, result)
        if self.enforcing and deletion is not None:
            for g, counts in sorted(deletion.affected.items()):
                self._audit.record(p, action, g, target, "allowed", dict(counts))
        return result

"""Authorization: which tenant a call targets and whether the caller may perform it.

Pure decisions over a resolved ``Principal``; no I/O except reading the tenant
registry in ``visible_tenants``. Callers must ``authorize`` a tenant before looking
it up, so a caller who does not hold a tenant cannot learn whether it exists.
"""
from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import Any

from memory_mcp.admin.models import AuthzError, Principal
from memory_mcp.admin.tenants import TenantService

READ_ACTIONS: frozenset[str] = frozenset({
    "search_memories", "list_memories",
    "kg_catalog", "kg_resolve", "kg_get_entity", "kg_find", "kg_traverse", "kg_path",
    "kg_impact", "kg_overview", "kg_related_across", "kg_for_memory",
})
WRITE_ACTIONS: frozenset[str] = frozenset({
    "save_memory", "delete_memory",
    "kg_upsert_entity", "kg_link", "kg_unlink", "kg_retire_entity", "kg_delete_entity",
    "kg_delete_xref", "kg_batch", "kg_xref",
})
REST_ACTION: dict[tuple[str, str], str] = {
    ("GET", "/memories"): "list_memories",
    ("GET", "/memories/{id}"): "search_memories",
    ("POST", "/memories/search"): "search_memories",
    ("POST", "/memories"): "save_memory",
    ("PATCH", "/memories/{id}"): "save_memory",
    ("DELETE", "/memories/{id}"): "delete_memory",
}

DEFAULT_AGENT = "claude-code"
_WILDCARD = "*"


def role_on(p: Principal, tenant: str) -> str | None:
    """The principal's role on `tenant`.

    The "*" wildcard (every tenant) applies to UI principals only, where it comes from
    an admin group. Keys never hold "*": a key lists its tenants explicitly, so a "*"
    in a key's roles is ignored rather than widening its reach.
    """
    role = p.roles.get(tenant)
    if role is None and p.kind == "ui":
        role = p.roles.get(_WILDCARD)
    return role


def _holds(p: Principal, tenant: str) -> bool:
    return p.all_tenants or role_on(p, tenant) is not None


def visible_tenants(p: Principal, tenants: TenantService) -> list[str]:
    """Ids of the active tenants the principal holds, sorted."""
    return [t.id for t in tenants.all() if t.status == "active" and _holds(p, t.id)]


def resolve_tenant(p: Principal, requested: str | None, tenants: TenantService,
                   settings: Mapping[str, Any]) -> str:
    """The tenant a call targets. Does not check that the principal holds it (see `authorize`)."""
    if requested is not None:
        return requested
    if p.kind == "key":
        held = [t for t in p.roles if t != _WILDCARD]
        if len(held) == 1:
            return held[0]
        raise AuthzError(400, "tenant_required", "This key covers several tenants; pass tenant")
    default = settings.get("default_tenant")
    if isinstance(default, str) and default:
        return default
    raise AuthzError(400, "tenant_required", "No default tenant is configured; pass tenant")


def authorize(p: Principal, action: str, tenant: str) -> None:
    """Raise AuthzError(403) unless `p` may perform `action` in `tenant`.

    Order: tenant held -> access level -> denied tools. Unknown actions need write access.
    """
    if p.all_tenants:
        return
    role = role_on(p, tenant)
    if role is None:
        # Same answer whether or not the tenant exists.
        raise AuthzError(403, "tenant_forbidden", f"No access to tenant '{tenant}'")
    if action not in READ_ACTIONS:
        if p.kind == "key" and p.access != "write":
            raise AuthzError(403, "key_read_only", f"This key is read-only; {action} is not allowed")
        if role not in ("editor", "admin"):
            raise AuthzError(403, "role_insufficient",
                             f"Role '{role}' cannot call {action} in tenant '{tenant}'")
    if action in p.denied_tools:
        raise AuthzError(403, "tool_denied", f"{action} is denied for this caller")


def repo_allowed(p: Principal, source_repo: str) -> bool:
    """True if `source_repo` matches one of the principal's patterns (case-sensitive), or there are none."""
    if not p.source_repo_patterns:
        return True
    return any(fnmatchcase(source_repo, pat) for pat in p.source_repo_patterns)


def stamped_agent(p: Principal, declared: str | None) -> str:
    """The agent recorded on a write: keys and UI users are stamped; legacy keeps the declared one."""
    if p.kind == "key":
        return p.agent or f"key:{p.id}"  # never the caller's declared value
    if p.kind == "ui":
        return f"ui:{p.user_name}"
    return declared or DEFAULT_AGENT

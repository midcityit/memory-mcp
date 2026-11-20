"""MCP tool layer — memory tools over MemoryOps (authorization included), mounted at /mcp."""
import os
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from memory_mcp.admin.auth import principal_from_ctx
from memory_mcp.admin.memory_ops import MemoryOps
from memory_mcp.admin.models import AuthzError
from memory_mcp.store import record_dict

_allowed_hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "localhost").split(",") if h.strip()]
_transport_security = TransportSecuritySettings(allowed_hosts=_allowed_hosts)

mcp = MCPServer("memory-twin")

# Bound by _init(). The tools are registered once and read these on every call,
# because MCPServer keeps the first registration of a name.
_ops: MemoryOps | None = None
_kg = None
_kg_guard = None
_kg_registered = False


def streamable_http_app():
    """Build the mounted ASGI app with the transport settings that moved off the MCPServer constructor in mcp v2."""
    return mcp.streamable_http_app(
        stateless_http=True,
        streamable_http_path="/",
        transport_security=_transport_security,
    )


def _init(ops: MemoryOps, kg=None, kg_guard=None) -> None:
    """Bind the live MemoryOps and KG used by the tools; register the kg tools on first use."""
    global _ops, _kg, _kg_guard, _kg_registered
    _ops = ops
    _kg, _kg_guard = kg, kg_guard
    if kg is not None and not _kg_registered:
        from memory_mcp.kg.tools import register_resolved
        register_resolved(mcp, _kg_deps)
        _kg_registered = True


def _kg_deps():
    if _kg is None:
        raise ToolError("The knowledge graph is not enabled on this server")
    return _kg.service, _kg.query, _kg_guard


def _call(ctx: Context, fn: Callable[[MemoryOps, Any], Any]) -> Any:
    """Run `fn(ops, principal)`; authorization failures reach the client as a tool error."""
    if _ops is None:
        raise RuntimeError("memory tools used before _init()")
    try:
        return fn(_ops, principal_from_ctx(ctx))
    except AuthzError as e:
        raise ToolError(e.message) from e


@mcp.tool()
def save_memory(
    ctx: Context,
    type: str,
    name: str,
    content: str,
    source_repo: str = "global",
    agent: str = "claude-code",
    tags: list[str] = [],
    tenant: str | None = None,
) -> dict:
    """Save or update a memory by name within a source_repo."""
    record = _call(ctx, lambda ops, p: ops.save(
        p, tenant, type=type, name=name, content=content, source_repo=source_repo, agent=agent, tags=tags))
    return record_dict(record)


@mcp.tool()
def search_memories(
    ctx: Context,
    query: str,
    limit: int = 10,
    filter_type: str | None = None,
    filter_source_repo: str | None = None,
    tenant: str | None = None,
) -> list[dict]:
    """Semantic search across stored memories."""
    records = _call(ctx, lambda ops, p: ops.search(
        p, tenant, query=query, limit=limit, filter_type=filter_type, filter_source_repo=filter_source_repo))
    return [record_dict(r) for r in records]


@mcp.tool()
def list_memories(
    ctx: Context,
    type: str | None = None,
    source_repo: str | None = None,
    agent: str | None = None,
    tags: str | None = None,
    limit: int = 1000,
    offset: str | None = None,
    tenant: str | None = None,
) -> dict:
    """List memories with optional filters. tags is comma-separated. Returns memories and next_offset for pagination."""
    tag_list = tags.split(",") if tags else None
    records, next_offset = _call(ctx, lambda ops, p: ops.list(
        p, tenant, type=type, source_repo=source_repo, agent=agent, tags=tag_list, limit=limit, offset=offset))
    result = {"memories": [record_dict(r) for r in records]}
    if next_offset:
        result["next_offset"] = next_offset
    return result


@mcp.tool()
def delete_memory(ctx: Context, memory_id: str, tenant: str | None = None) -> dict:
    """Delete a memory by ID."""
    return {"deleted": _call(ctx, lambda ops, p: ops.delete(p, tenant, memory_id))}

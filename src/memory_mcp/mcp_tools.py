"""MCP tool layer — wraps MemoryStore as MCP tools, mounted at /mcp."""
from mcp.server.fastmcp import FastMCP
from memory_mcp.store import MemoryStore, MemoryRecord
import dataclasses

mcp = FastMCP("memory-twin", stateless_http=True, streamable_http_path="/")


def _init(store: MemoryStore) -> None:
    """Bind a live MemoryStore into the tool closures."""

    @mcp.tool()
    def save_memory(
        type: str,
        name: str,
        content: str,
        source_repo: str = "global",
        agent: str = "claude-code",
        tags: list[str] = [],
    ) -> dict:
        """Save or update a memory by name within a source_repo."""
        now = MemoryStore.now_iso()
        existing = store.list_memories(filter_source_repo=source_repo, limit=1000)
        match = next((r for r in existing if r.name == name), None)
        if match:
            record = store.update(match.id, content, tags, type=type)
        else:
            record = store.upsert(MemoryRecord(
                id=MemoryStore.new_id(),
                type=type, name=name, content=content,
                source_repo=source_repo, agent=agent, tags=tags,
                created_at=now, updated_at=now,
            ))
        return dataclasses.asdict(record)

    @mcp.tool()
    def search_memories(
        query: str,
        limit: int = 10,
        filter_type: str | None = None,
        filter_source_repo: str | None = None,
    ) -> list[dict]:
        """Semantic search across stored memories."""
        records = store.search(
            query=query,
            limit=limit,
            filter_type=filter_type,
            filter_source_repo=filter_source_repo,
        )
        return [dataclasses.asdict(r) for r in records]

    @mcp.tool()
    def list_memories(
        type: str | None = None,
        source_repo: str | None = None,
        agent: str | None = None,
        tags: str | None = None,
    ) -> list[dict]:
        """List memories with optional filters. tags is comma-separated."""
        tag_list = tags.split(",") if tags else None
        records = store.list_memories(
            filter_type=type,
            filter_source_repo=source_repo,
            filter_agent=agent,
            filter_tags=tag_list,
        )
        return [dataclasses.asdict(r) for r in records]

    @mcp.tool()
    def delete_memory(memory_id: str) -> dict:
        """Delete a memory by ID."""
        ok = store.delete(memory_id)
        return {"deleted": ok}

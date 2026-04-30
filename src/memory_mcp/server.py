import dataclasses
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Security, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from memory_mcp.config import load_config, Config
from memory_mcp.store import MemoryStore, MemoryRecord
from memory_mcp import mcp_tools

# auto_error=False so we can return 401 (not 403) when no Authorization header
_security = HTTPBearer(auto_error=False)


def create_app() -> FastAPI:
    cfg = load_config()
    store = MemoryStore(qdrant_url=cfg.qdrant_url, stale_days=cfg.stale_days)
    mcp_tools._init(store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp_tools.mcp.session_manager.run():
            yield

    app = FastAPI(title="memory-mcp", lifespan=lifespan)

    # Guard /mcp with bearer token before routing to the MCP sub-app
    @app.middleware("http")
    async def _bearer_guard(request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != cfg.api_token:
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    app.mount("/mcp", mcp_tools.mcp.streamable_http_app())

    def require_token(
        credentials: Optional[HTTPAuthorizationCredentials] = Security(_security),
    ):
        if credentials is None or credentials.credentials != cfg.api_token:
            raise HTTPException(status_code=401, detail="Invalid or missing token")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # ── REST list ──────────────────────────────────────────────────────────────
    @app.get("/memories", dependencies=[Depends(require_token)])
    def list_memories(
        type: Optional[str] = None,
        source_repo: Optional[str] = None,
        agent: Optional[str] = None,
        tags: Optional[str] = None,
    ):
        tag_list = tags.split(",") if tags else None
        records = store.list_memories(
            filter_type=type,
            filter_source_repo=source_repo,
            filter_agent=agent,
            filter_tags=tag_list,
        )
        return {"memories": [_record_dict(r) for r in records]}

    # ── REST get single ────────────────────────────────────────────────────────
    @app.get("/memories/{memory_id}", dependencies=[Depends(require_token)])
    def get_memory(memory_id: str):
        record = store.get(memory_id)
        if not record:
            raise HTTPException(status_code=404, detail="Memory not found")
        return _record_dict(record)

    # ── REST search ────────────────────────────────────────────────────────────
    class SearchRequest(BaseModel):
        query: str
        limit: int = 10
        filter_type: Optional[str] = None
        filter_source_repo: Optional[str] = None

    @app.post("/memories/search", dependencies=[Depends(require_token)])
    def search_memories(req: SearchRequest):
        records = store.search(
            query=req.query,
            limit=req.limit,
            filter_type=req.filter_type,
            filter_source_repo=req.filter_source_repo,
        )
        return {"memories": [_record_dict(r) for r in records]}

    # ── REST save ──────────────────────────────────────────────────────────────
    class SaveRequest(BaseModel):
        type: str
        name: str
        content: str
        source_repo: str = "global"
        agent: str = "claude-code"
        tags: list[str] = []

    @app.post("/memories", dependencies=[Depends(require_token)], status_code=201)
    def save_memory(req: SaveRequest):
        now = MemoryStore.now_iso()
        existing = store.list_memories(filter_source_repo=req.source_repo, limit=1000)
        match = next((r for r in existing if r.name == req.name), None)
        if match:
            record = store.update(match.id, req.content, req.tags, type=req.type)
        else:
            record = store.upsert(MemoryRecord(
                id=MemoryStore.new_id(),
                type=req.type, name=req.name, content=req.content,
                source_repo=req.source_repo, agent=req.agent, tags=req.tags,
                created_at=now, updated_at=now,
            ))
        return _record_dict(record)

    # ── REST update ────────────────────────────────────────────────────────────
    class UpdateRequest(BaseModel):
        content: str
        tags: list[str] = []

    @app.patch("/memories/{memory_id}", dependencies=[Depends(require_token)])
    def update_memory(memory_id: str, req: UpdateRequest):
        record = store.update(memory_id, req.content, req.tags)
        if not record:
            raise HTTPException(status_code=404, detail="Memory not found")
        return _record_dict(record)

    # ── REST delete ────────────────────────────────────────────────────────────
    @app.delete("/memories/{memory_id}", dependencies=[Depends(require_token)], status_code=204)
    def delete_memory(memory_id: str):
        if not store.delete(memory_id):
            raise HTTPException(status_code=404, detail="Memory not found")

    return app


def _record_dict(r: MemoryRecord) -> dict:
    d = dataclasses.asdict(r)
    if r.stale:
        d["stale_warning"] = "⚠️ This memory is stale — verify it's still current"
    return d


# Only instantiate at module level when running for real (env vars present).
# Tests call create_app() directly with mocks, so this is skipped when
# QDRANT_URL is not set.
import os as _os
if _os.environ.get("QDRANT_URL"):
    app = create_app()

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from memory_mcp.admin.audit import Audit, NullAudit, run_purge_loop
from memory_mcp.admin.auth import Authenticator, PrincipalMiddleware, principal_from_request
from memory_mcp.admin.memory_ops import MemoryOps
from memory_mcp.admin.models import AuthzError, Principal
from memory_mcp.admin.rest import build_admin_router
from memory_mcp.admin.store import AdminStore
from memory_mcp.admin.tenants import TenantService
from memory_mcp.config import cfg_str, load_config, Config
from memory_mcp.store import MemoryStore, MemoryRecord, record_dict
from memory_mcp import mcp_tools
from memory_mcp.telemetry import setup_telemetry

TENANT_HEADER = "x-mt-tenant"


def build_admin_store(cfg: Config) -> AdminStore | None:
    """The admin store (tenants, keys, role mappings, settings), or None for legacy mode.

    In legacy mode only API_TOKEN is accepted and every memory lives in one collection.
    """
    dsn = cfg_str(cfg, "admin_pg_dsn")
    if dsn is None:
        return None
    from memory_mcp.admin.pg_store import PgAdminStore
    store = PgAdminStore(dsn)
    store.ensure_schema()
    return store


def _rest_tenant(request: Request) -> str | None:
    """The tenant a REST call names via ?tenant= or X-MT-Tenant (they must agree), or None."""
    named = set(request.query_params.getlist("tenant"))
    header = request.headers.get(TENANT_HEADER)
    if header is not None:
        named.add(header)
    if len(named) > 1:
        raise AuthzError(400, "tenant_conflict", "?tenant= and X-MT-Tenant name different tenants")
    return next(iter(named), None)


def create_app(*, admin_store: AdminStore | None = None, memory_store: MemoryStore | None = None,
               audit_purge_interval_s: float | None = None,
               audit_purge_first_delay_s: float | None = None) -> FastAPI:
    """Build the app. `admin_store` / `memory_store` override the configured ones (tests)."""
    cfg = load_config()

    # Bootstrap OTel before anything else
    if cfg.otlp_endpoint:
        setup_telemetry(cfg.otlp_endpoint)

    store = memory_store if memory_store is not None else MemoryStore(qdrant_url=cfg.qdrant_url,
                                                                      stale_days=cfg.stale_days)
    kg = None
    if cfg.kg_enabled:
        from memory_mcp.kg import build_kg
        kg = build_kg(store.client, store.embedder, memory_store=store, config_dir=cfg.kg_config_dir,
                      dup_threshold=cfg.kg_dup_threshold, max_chars=cfg.kg_max_chars,
                      backend=cfg.kg_backend, pg_dsn=cfg.kg_pg_dsn,
                      dual_write=cfg.kg_dual_write, dual_write_strict=cfg.kg_dual_write_strict)

    if admin_store is None:
        admin_store = build_admin_store(cfg)
    tenants = None
    if admin_store is not None:
        tenants = TenantService(admin_store, store, kg.store if kg is not None else None)
        tenants.bootstrap(cfg.bootstrap_tenant_id, cfg.bootstrap_tenant_name,
                          existing_graphs=list(kg.registry.graphs) if kg is not None else [])

    # Resolve the caller (scoped key, legacy API_TOKEN, or UI acting for a user) on every
    # path except /health, before routing; handlers and MCP tools read the Principal.
    if kg is not None and tenants is not None:
        # After bootstrap, which adopts the configured graphs as tenants.
        from memory_mcp.kg import use_tenants
        use_tenants(kg, tenants, store)

    authenticator = Authenticator(cfg, admin_store)
    audit = Audit(admin_store, authenticator.settings) if admin_store is not None else NullAudit()
    # Settings come from the authenticator's cache so admin writes invalidate one place.
    ops = MemoryOps(store, tenants, authenticator.settings, audit)
    kg_guard = None
    if kg is not None:
        from memory_mcp.kg.authz import KGGuard
        kg_guard = KGGuard(tenants, audit)
    mcp_tools._init(ops, kg, kg_guard)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        purge_task = None
        if admin_store is not None:
            kw = {}
            if audit_purge_interval_s is not None:
                kw["interval_s"] = audit_purge_interval_s
            if audit_purge_first_delay_s is not None:
                kw["first_delay_s"] = audit_purge_first_delay_s
            purge_task = asyncio.create_task(run_purge_loop(audit, **kw))
        try:
            async with mcp_tools.mcp.session_manager.run():
                yield
        finally:
            if purge_task is not None:
                purge_task.cancel()
                await asyncio.gather(purge_task, return_exceptions=True)

    app = FastAPI(title="memory-mcp", lifespan=lifespan)

    # Auto-instrument HTTP metrics (request count, duration)
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        pass  # graceful degradation if instrumentation unavailable

    app.state.admin_store = admin_store
    app.state.tenants = tenants
    app.state.authenticator = authenticator
    app.state.audit = audit
    app.add_middleware(PrincipalMiddleware, authenticator=authenticator)

    @app.exception_handler(AuthzError)
    async def authz_error(_request: Request, e: AuthzError):
        return JSONResponse({"detail": e.message, "code": e.code}, status_code=e.status)

    app.mount("/mcp", mcp_tools.streamable_http_app())

    app.include_router(build_admin_router(
        store=admin_store, tenants=tenants, authenticator=authenticator, audit=audit,
        memory_store=store, admin_group_ids=cfg.admin_group_ids, stale_days=cfg.stale_days))

    if kg is not None:
        from memory_mcp.kg.rest import build_router
        app.include_router(build_router(kg.service, kg.query, principal_from_request, kg_guard))

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # ── REST memory routes (authorization lives in MemoryOps, shared with MCP) ──
    @app.get("/memories")
    def list_memories(
        type: Optional[str] = None,
        source_repo: Optional[str] = None,
        agent: Optional[str] = None,
        tags: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[str] = None,
        p: Principal = Depends(principal_from_request),
        tenant: Optional[str] = Depends(_rest_tenant),
    ):
        tag_list = tags.split(",") if tags else None
        records, next_offset = ops.list(
            p, tenant, type=type, source_repo=source_repo, agent=agent, tags=tag_list,
            limit=limit or cfg.default_list_limit, offset=offset,
        )
        result = {"memories": [_record_dict(r) for r in records]}
        if next_offset:
            result["next_offset"] = next_offset
        return result

    @app.get("/memories/{memory_id}")
    def get_memory(memory_id: str, p: Principal = Depends(principal_from_request),
                   tenant: Optional[str] = Depends(_rest_tenant)):
        record = ops.get(p, tenant, memory_id)
        if not record:
            raise HTTPException(status_code=404, detail="Memory not found")
        return _record_dict(record)

    class SearchRequest(BaseModel):
        query: str
        limit: int = 10
        filter_type: Optional[str] = None
        filter_source_repo: Optional[str] = None

    @app.post("/memories/search")
    def search_memories(req: SearchRequest, p: Principal = Depends(principal_from_request),
                        tenant: Optional[str] = Depends(_rest_tenant)):
        records = ops.search(p, tenant, query=req.query, limit=req.limit,
                             filter_type=req.filter_type, filter_source_repo=req.filter_source_repo)
        return {"memories": [_record_dict(r) for r in records]}

    class SaveRequest(BaseModel):
        type: str
        name: str
        content: str
        source_repo: str = "global"
        agent: str = "claude-code"
        tags: list[str] = []

    @app.post("/memories", status_code=201)
    def save_memory(req: SaveRequest, p: Principal = Depends(principal_from_request),
                    tenant: Optional[str] = Depends(_rest_tenant)):
        record = ops.save(p, tenant, type=req.type, name=req.name, content=req.content,
                          source_repo=req.source_repo, agent=req.agent, tags=req.tags)
        return _record_dict(record)

    class UpdateRequest(BaseModel):
        content: str
        tags: list[str] = []

    @app.patch("/memories/{memory_id}")
    def update_memory(memory_id: str, req: UpdateRequest, p: Principal = Depends(principal_from_request),
                      tenant: Optional[str] = Depends(_rest_tenant)):
        record = ops.update(p, tenant, memory_id, req.content, req.tags)
        if not record:
            raise HTTPException(status_code=404, detail="Memory not found")
        return _record_dict(record)

    @app.delete("/memories/{memory_id}", status_code=204)
    def delete_memory(memory_id: str, p: Principal = Depends(principal_from_request),
                      tenant: Optional[str] = Depends(_rest_tenant)):
        if not ops.delete(p, tenant, memory_id):
            raise HTTPException(status_code=404, detail="Memory not found")

    return app


def _record_dict(r: MemoryRecord) -> dict:
    d = record_dict(r)
    if r.stale:
        d["stale_warning"] = "⚠️ This memory is stale — verify it's still current"
    return d


# `uvicorn memory_mcp.server:create_app --factory` (the Dockerfile) builds the app once per
# worker. `uvicorn memory_mcp.server:app` still works: the module attribute `app` is built
# lazily on first access, so importing this module never builds an app (no second model
# load, bootstrap or admin pool per worker).
_app: FastAPI | None = None


def __getattr__(name: str):
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

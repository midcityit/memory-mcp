"""Shared app builder for the admin REST and copy tests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from qdrant_client import QdrantClient

from memory_mcp.admin.keys import generate_key
from memory_mcp.admin.models import ApiKey, RoleMapping
from memory_mcp.admin.store import InMemoryAdminStore
from memory_mcp.config import Config
from memory_mcp.store import COLLECTION, MemoryRecord, MemoryStore
from tests.kg.conftest import StubEmbedder

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
LEGACY = "legacy-token-value"
UI = "ui-token-value"
G_VIEW = "00000000-0000-0000-0000-00000000000a"    # viewer on alpha
G_EDIT = "00000000-0000-0000-0000-00000000000b"    # editor on alpha
G_TADMIN = "00000000-0000-0000-0000-00000000000d"  # admin on alpha, viewer on beta
G_BADMIN = "00000000-0000-0000-0000-00000000000e"  # admin on beta
G_ADMIN = "00000000-0000-0000-0000-00000000000c"   # instance admin via ADMIN_GROUP_IDS


def ui(*groups: str, user: str = "user-1", name: str = "Ada Example") -> dict:
    return {"Authorization": f"Bearer {UI}", "X-MT-User": user, "X-MT-User-Name": name,
            "X-MT-Groups": ",".join(groups)}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def collection(tenant: str) -> str:
    return COLLECTION if tenant == "alpha" else f"memories_{tenant}"


@dataclass
class Env:
    app: Any
    admin: InMemoryAdminStore | None
    mstore: MemoryStore
    client: AsyncClient

    def key(self, tenants=("alpha",), access="write", agent="agent-a", expires_at=None) -> tuple[str, str]:
        """Insert a key straight into the store; returns (full key, key id)."""
        full, key_id, secret_hash = generate_key()
        self.admin.insert_key(ApiKey(
            id=key_id, agent=agent, secret_hash=secret_hash, tenants=tuple(tenants), access=access,
            denied_tools=(), source_repo_patterns=(), expires_at=expires_at, created_at=T0,
            created_by="seed", revoked_at=None, last_used_at=None, rotated_from=None,
        ))
        return full, key_id

    def seed(self, tenant: str, name: str, source_repo: str = "global", content: str | None = None,
             tags=(), type: str = "note") -> str:
        now = MemoryStore.now_iso()
        rid = MemoryStore.new_id()
        self.mstore.upsert(MemoryRecord(id=rid, type=type, name=name, content=content or f"{name} content",
                                        source_repo=source_repo, agent="seed", tags=list(tags),
                                        created_at=now, updated_at=now), collection=collection(tenant))
        return rid

    def audit(self, **filters) -> list[dict]:
        rows, _ = self.admin.query_audit(limit=10_000, **filters)
        return rows


def cfg(**over) -> Config:
    base = dict(qdrant_url="unused", api_token=LEGACY, stale_days=30, otlp_endpoint="",
                default_list_limit=1000, ui_token=UI, admin_group_ids=(G_ADMIN,),
                bootstrap_tenant_id="alpha", bootstrap_tenant_name="Alpha")
    base.update(over)
    return Config(**base)


def build_app(multitenant: bool = True):
    from memory_mcp.server import create_app
    mstore = MemoryStore("unused", embedder=StubEmbedder(), client=QdrantClient(":memory:"))
    admin = InMemoryAdminStore() if multitenant else None
    with patch("memory_mcp.server.load_config", return_value=cfg() if multitenant else cfg(ui_token=None)):
        app = create_app(admin_store=admin, memory_store=mstore)
    if multitenant:
        app.state.tenants.create("beta", "Beta", "", by="test")
        app.state.tenants.create("gamma", "Gamma", "", by="test")
        admin.replace_mappings([
            RoleMapping(G_VIEW, "alpha", "viewer"), RoleMapping(G_EDIT, "alpha", "editor"),
            RoleMapping(G_TADMIN, "alpha", "admin"), RoleMapping(G_TADMIN, "beta", "viewer"),
            RoleMapping(G_BADMIN, "beta", "admin"),
        ], by="test")
    return app, admin, mstore


async def serve(multitenant: bool = True):
    app, admin, mstore = build_app(multitenant)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        yield Env(app, admin, mstore, c)

import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import MagicMock, patch


def make_app():
    with patch("memory_mcp.server.MemoryStore") as mock_store_cls, \
         patch("memory_mcp.server.load_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(
            qdrant_url="http://localhost:6333",
            api_token="test-token",
            stale_days=30,
            otlp_endpoint="",
            kg_enabled=False,
        )
        mock_store_cls.return_value.list_memories.return_value = ([], None)
        from memory_mcp.server import create_app
        return create_app()


@pytest.fixture
def app():
    return make_app()


@pytest.mark.asyncio
async def test_health_returns_ok(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_missing_token_returns_401(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/memories")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wrong_token_returns_401(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/memories", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_correct_token_passes(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/memories", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200


def test_create_app_with_kg_enabled_registers_tools_and_routes():
    import asyncio
    from qdrant_client import QdrantClient
    from memory_mcp import mcp_tools
    from tests.kg.conftest import StubEmbedder
    with patch("memory_mcp.server.MemoryStore") as mock_store_cls, \
         patch("memory_mcp.server.load_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(
            qdrant_url="http://localhost:6333", api_token="test-token", stale_days=30, otlp_endpoint="",
            kg_enabled=True, kg_config_dir=None, kg_dup_threshold=0.90, kg_max_chars=6000,
            # RCL-50 added a dual-write branch in build_kg keyed on these config
            # attrs. A bare MagicMock returns truthy children, which would force the
            # dual-write path (and a psycopg import the Qdrant-only CI env lacks).
            # Pin the Qdrant single-store defaults this test actually exercises.
            kg_backend="qdrant", kg_pg_dsn=None, kg_dual_write=False, kg_dual_write_strict=False,
        )
        store = mock_store_cls.return_value
        store.list_memories.return_value = ([], None)
        store.client = QdrantClient(":memory:")
        store.embedder = StubEmbedder()
        from memory_mcp.server import create_app
        app = create_app()

    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            ok = await c.get("/kg/graphs", headers={"Authorization": "Bearer test-token"})
            bad = await c.get("/kg/graphs")
        return ok, bad

    ok, bad = asyncio.run(run())
    assert ok.status_code == 200
    assert {"mcit", "vtv"} <= set(ok.json()["graphs"])
    assert bad.status_code == 401
    assert "kg_upsert_entity" in {t.name for t in asyncio.run(mcp_tools.mcp.list_tools())}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/no-such-path"])
async def test_legacy_mode_guards_docs_and_unknown_paths(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(path)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


@pytest.mark.asyncio
async def test_legacy_mode_health_needs_no_token(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200


def test_import_does_not_build_app_and_app_attribute_builds_once(monkeypatch):
    """`--factory` builds the app per worker; importing the module must not build another one."""
    import importlib

    import memory_mcp.config
    import memory_mcp.server as server

    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid:6333")
    try:
        # create_app() calls load_config() first; reloading must not reach it.
        with patch.object(memory_mcp.config, "load_config", side_effect=AssertionError("app built at import")):
            importlib.reload(server)
        assert "app" not in vars(server) and server._app is None
        sentinel = object()
        with patch.object(server, "create_app", return_value=sentinel) as build:
            assert server.app is sentinel
            assert server.app is sentinel
            from memory_mcp.server import app as imported  # what `uvicorn memory_mcp.server:app` does
            assert imported is sentinel
        build.assert_called_once_with()
        with pytest.raises(AttributeError):
            server.no_such_attribute
    finally:
        monkeypatch.delenv("QDRANT_URL")
        importlib.reload(server)


def test_legacy_mode_with_only_ui_token_fails_at_startup():
    from memory_mcp.config import Config
    from memory_mcp.server import create_app

    cfg = Config(qdrant_url="unused", api_token=None, stale_days=30, otlp_endpoint="",
                 default_list_limit=1000, ui_token="ui-token-value")
    with patch("memory_mcp.server.load_config", return_value=cfg), \
         patch("memory_mcp.server.MemoryStore") as store_cls:
        with pytest.raises(RuntimeError, match="UI_TOKEN is set but ADMIN_PG_DSN is not"):
            create_app()
    store_cls.assert_not_called()  # fails before loading the model or touching Qdrant

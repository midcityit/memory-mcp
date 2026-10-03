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

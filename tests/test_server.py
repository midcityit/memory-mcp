import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import MagicMock, patch


def make_app():
    with patch("memory_mcp.server.MemoryStore"), \
         patch("memory_mcp.server.load_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(
            qdrant_url="http://localhost:6333",
            api_token="test-token",
            stale_days=30,
        )
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

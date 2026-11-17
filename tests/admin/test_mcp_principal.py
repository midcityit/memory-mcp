"""The principal reaches an MCP tool per request, even under interleaved concurrent calls."""
import asyncio
import json
from datetime import datetime, timezone

from httpx import ASGITransport, AsyncClient
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount

from memory_mcp.admin.auth import Authenticator, PrincipalMiddleware, principal_from_ctx
from memory_mcp.admin.keys import generate_key
from memory_mcp.admin.models import ApiKey
from memory_mcp.admin.store import InMemoryAdminStore

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
ACCEPT = "application/json, text/event-stream"


class _Cfg:
    api_token = "api-token-value"
    ui_token = None
    admin_group_ids = ()


def _add_key(store, agent):
    full, key_id, secret_hash = generate_key()
    store.insert_key(ApiKey(
        id=key_id, agent=agent, secret_hash=secret_hash, tenants=("alpha",), access="write",
        denied_tools=(), source_repo_patterns=(), expires_at=None, created_at=T0, created_by="admin",
        revoked_at=None, last_used_at=None, rotated_from=None,
    ))
    return full, key_id


def _build():
    mcp = MCPServer("whoami-test")

    @mcp.tool()
    async def whoami(ctx: Context) -> str:
        await asyncio.sleep(0)  # yield so concurrent calls interleave inside the tool
        principal = principal_from_ctx(ctx)
        await asyncio.sleep(0.01)
        return principal.id

    store = InMemoryAdminStore()
    keys = [_add_key(store, "agent-a"), _add_key(store, "agent-b")]
    app = Starlette(routes=[Mount("/mcp", app=mcp.streamable_http_app(
        stateless_http=True, streamable_http_path="/",
        transport_security=TransportSecuritySettings(allowed_hosts=["test"]),
    ))])
    app.add_middleware(PrincipalMiddleware, authenticator=Authenticator(_Cfg(), store))
    return mcp, app, keys


def _result_text(resp) -> str:
    assert resp.status_code == 200, resp.text
    if resp.headers["content-type"].startswith("text/event-stream"):
        data = [line[5:].strip() for line in resp.text.splitlines() if line.startswith("data:")]
        body = json.loads(data[-1])
    else:
        body = resp.json()
    assert "error" not in body, body
    result = body["result"]
    assert not result.get("isError"), result
    return result["content"][0]["text"]


async def test_concurrent_tool_calls_see_their_own_principal():
    mcp, app, keys = _build()
    async with mcp.session_manager.run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            async def call(i: int, token: str):
                headers = {"Authorization": f"Bearer {token}", "Accept": ACCEPT,
                           "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
                resp = await c.post("/mcp/", headers=headers, json={
                    "jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": "whoami", "arguments": {}},
                })
                return _result_text(resp)

            plan = [keys[i % 2] for i in range(20)]
            got = await asyncio.gather(*(call(i, full) for i, (full, _) in enumerate(plan)))

    assert got == [key_id for _, key_id in plan]
    assert {*got} == {keys[0][1], keys[1][1]}


async def test_mcp_without_valid_token_is_401():
    mcp, app, _ = _build()
    async with mcp.session_manager.run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/mcp/", headers={"Accept": ACCEPT}, json={})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}

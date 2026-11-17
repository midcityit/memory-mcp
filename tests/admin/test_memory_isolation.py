"""Authorization on the memory REST routes and MCP tools (RCL-73 task 5).

Every isolation case runs over REST and over real MCP tool calls through the full app.
The MCP surface has no get-by-id or update-by-id tool, so those cases are REST only.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from qdrant_client import QdrantClient

from memory_mcp import mcp_tools
from memory_mcp.admin.authz import (
    READ_ACTIONS, REST_ACTION, WRITE_ACTIONS, authorize, repo_allowed, resolve_tenant,
    stamped_agent, visible_tenants,
)
from memory_mcp.admin.keys import generate_key
from memory_mcp.admin.models import ApiKey, AuthzError, Principal, RoleMapping
from memory_mcp.admin.store import InMemoryAdminStore
from memory_mcp.config import Config
from memory_mcp.store import COLLECTION, MemoryRecord, MemoryStore
from tests.kg.conftest import StubEmbedder

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect")

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
LEGACY = "legacy-token-value"
UI = "ui-token-value"
ACCEPT = "application/json, text/event-stream"
G_VIEW = "00000000-0000-0000-0000-00000000000a"
G_EDIT = "00000000-0000-0000-0000-00000000000b"
G_ADMIN = "00000000-0000-0000-0000-00000000000c"
TRANSPORTS = ["rest", "mcp"]


class RecordingAudit:
    def __init__(self):
        self.rows: list[tuple] = []

    def record(self, principal, action, tenant, target, outcome, detail=None):
        self.rows.append((principal.id, action, tenant, target, outcome, detail))


# ── app under test ──────────────────────────────────────────────────────────────
@dataclass
class Env:
    app: Any
    admin: InMemoryAdminStore | None
    mstore: MemoryStore
    audit: RecordingAudit
    client: AsyncClient = None

    def key(self, tenants=("alpha",), access="write", denied=(), patterns=(), agent="agent-a") -> str:
        full, key_id, secret_hash = generate_key()
        self.admin.insert_key(ApiKey(
            id=key_id, agent=agent, secret_hash=secret_hash, tenants=tuple(tenants), access=access,
            denied_tools=tuple(denied), source_repo_patterns=tuple(patterns), expires_at=None,
            created_at=T0, created_by="admin", revoked_at=None, last_used_at=None, rotated_from=None,
        ))
        return full

    def settings(self, **values) -> None:
        self.admin.put_settings(values, by="test")
        self.app.state.authenticator.invalidate()

    def seed(self, tenant: str, name: str, source_repo: str = "global", type: str = "note") -> str:
        """Write a record straight into a tenant's collection; returns its id."""
        coll = COLLECTION if tenant == "alpha" else f"memories_{tenant}"
        now = MemoryStore.now_iso()
        rid = MemoryStore.new_id()
        self.mstore.upsert(MemoryRecord(id=rid, type=type, name=name, content=f"{name} content",
                                        source_repo=source_repo, agent="seed", tags=[],
                                        created_at=now, updated_at=now), collection=coll)
        return rid

    def exists(self, tenant: str, rid: str) -> bool:
        coll = COLLECTION if tenant == "alpha" else f"memories_{tenant}"
        return self.mstore.get(rid, collection=coll) is not None


def _cfg(**over) -> Config:
    base = dict(qdrant_url="unused", api_token=LEGACY, stale_days=30, otlp_endpoint="",
                default_list_limit=1000, ui_token=UI, admin_group_ids=(G_ADMIN,),
                bootstrap_tenant_id="alpha", bootstrap_tenant_name="Alpha")
    base.update(over)
    return Config(**base)


def _build(multitenant: bool = True) -> Env:
    from memory_mcp.server import create_app
    qc = QdrantClient(":memory:")
    mstore = MemoryStore("unused", embedder=StubEmbedder(), client=qc)
    admin = InMemoryAdminStore() if multitenant else None
    audit = RecordingAudit()
    cfg = _cfg() if multitenant else _cfg(ui_token=None)
    with patch("memory_mcp.server.load_config", return_value=cfg), \
         patch("memory_mcp.server.NullAudit", return_value=audit), \
         patch("memory_mcp.server.Audit", return_value=audit):
        app = create_app(admin_store=admin, memory_store=mstore)
    if multitenant:
        app.state.tenants.create("beta", "Beta", "", by="test")
        admin.replace_mappings([RoleMapping(G_VIEW, "alpha", "viewer"),
                                RoleMapping(G_EDIT, "alpha", "editor")], by="test")
    return Env(app, admin, mstore, audit)


async def _serving(e: Env):
    """Run the MCP session manager in its own task (enter and exit must share a task)."""
    ready, stop = asyncio.Event(), asyncio.Event()

    async def run():
        async with mcp_tools.mcp.session_manager.run():
            ready.set()
            await stop.wait()

    task = asyncio.create_task(run())
    await ready.wait()
    async with AsyncClient(transport=ASGITransport(app=e.app), base_url="http://localhost") as c:
        e.client = c
        yield e
    stop.set()
    await task


@pytest.fixture
async def env():
    async for e in _serving(_build()):
        yield e


@pytest.fixture
async def legacy_env():
    async for e in _serving(_build(multitenant=False)):
        yield e


# ── one call surface over both transports ───────────────────────────────────────
@dataclass
class Result:
    ok: bool
    data: Any = None
    message: str = ""
    status: int | None = None  # REST only
    code: str | None = None  # REST only
    not_found: bool = False


def ui_headers(name="Ada Example", groups=(G_EDIT,), user="user-1") -> dict:
    return {"Authorization": f"Bearer {UI}", "X-MT-User": user, "X-MT-User-Name": name,
            "X-MT-Groups": ",".join(groups)}


def _auth(who) -> dict:
    return dict(who) if isinstance(who, dict) else {"Authorization": f"Bearer {who}"}


class Caller:
    def __init__(self, transport: str, client: AsyncClient):
        self.transport = transport
        self.c = client

    async def _rest(self, method, path, who, tenant=None, body=None, params=None, headers=None) -> Result:
        params = dict(params or {})
        if tenant is not None:
            params["tenant"] = tenant
        h = _auth(who) | (headers or {})
        resp = await self.c.request(method, path, headers=h, params=params, json=body)
        if resp.status_code >= 400:
            j = resp.json()
            return Result(False, message=j.get("detail", ""), status=resp.status_code, code=j.get("code"),
                          not_found=resp.status_code == 404)
        return Result(True, data=resp.json() if resp.content else None, status=resp.status_code)

    async def _mcp(self, tool, who, args) -> Result:
        h = _auth(who) | {"Accept": ACCEPT, "Content-Type": "application/json",
                          "MCP-Protocol-Version": "2025-06-18"}
        resp = await self.c.post("/mcp/", headers=h, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
        assert resp.status_code == 200, resp.text
        if resp.headers["content-type"].startswith("text/event-stream"):
            data = [ln[5:].strip() for ln in resp.text.splitlines() if ln.startswith("data:")]
            body = json.loads(data[-1])
        else:
            body = resp.json()
        assert "error" not in body, body
        result = body["result"]
        if result.get("isError"):
            return Result(False, message=result["content"][0]["text"])
        if "structuredContent" in result:  # list returns are wrapped as {"result": [...]}
            return Result(True, data=result["structuredContent"]["result"])
        return Result(True, data=json.loads(result["content"][0]["text"]))

    @staticmethod
    def _args(tenant, **kw) -> dict:
        args = {k: v for k, v in kw.items() if v is not None}
        if tenant is not None:
            args["tenant"] = tenant
        return args

    async def save(self, who, name="n1", tenant=None, type="note", source_repo="global", agent=None,
                   content="some content") -> Result:
        fields = self._args(None, type=type, name=name, content=content, source_repo=source_repo, agent=agent)
        if self.transport == "rest":
            return await self._rest("POST", "/memories", who, tenant, body=fields)
        return await self._mcp("save_memory", who, self._args(tenant, **fields))

    async def search(self, who, query="content", tenant=None, filter_source_repo=None) -> Result:
        if self.transport == "rest":
            r = await self._rest("POST", "/memories/search", who, tenant,
                                 body=self._args(None, query=query, filter_source_repo=filter_source_repo))
            if r.ok:
                r.data = r.data["memories"]
            return r
        return await self._mcp("search_memories", who,
                               self._args(tenant, query=query, filter_source_repo=filter_source_repo))

    async def list(self, who, tenant=None, source_repo=None) -> Result:
        if self.transport == "rest":
            r = await self._rest("GET", "/memories", who, tenant, params=self._args(None, source_repo=source_repo))
        else:
            r = await self._mcp("list_memories", who, self._args(tenant, source_repo=source_repo))
        if r.ok:
            r.data = r.data["memories"]
        return r

    async def delete(self, who, rid, tenant=None) -> Result:
        if self.transport == "rest":
            return await self._rest("DELETE", f"/memories/{rid}", who, tenant)
        r = await self._mcp("delete_memory", who, self._args(tenant, memory_id=rid))
        if r.ok and r.data == {"deleted": False}:
            r.not_found = True
        return r

    # REST only
    async def get(self, who, rid, tenant=None) -> Result:
        return await self._rest("GET", f"/memories/{rid}", who, tenant)

    async def update(self, who, rid, tenant=None, content="changed") -> Result:
        return await self._rest("PATCH", f"/memories/{rid}", who, tenant, body={"content": content})


def denied(r: Result, status: int, code: str, fragment: str) -> None:
    assert not r.ok, r
    assert fragment in r.message, r.message
    if r.status is not None:
        assert (r.status, r.code) == (status, code), r


def names(r: Result) -> set[str]:
    assert r.ok, r
    return {m["name"] for m in r.data}


@pytest.fixture(params=TRANSPORTS)
def call(request, env) -> Caller:
    return Caller(request.param, env.client)


@pytest.fixture(params=TRANSPORTS)
def legacy_call(request, legacy_env) -> Caller:
    return Caller(request.param, legacy_env.client)


# ── isolation: alpha key vs beta ────────────────────────────────────────────────
async def test_alpha_key_cannot_touch_beta_when_named(env, call):
    k = env.key()
    rid = env.seed("beta", "secret")
    msg = "No access to tenant 'beta'"
    denied(await call.save(k, tenant="beta"), 403, "tenant_forbidden", msg)
    denied(await call.search(k, tenant="beta"), 403, "tenant_forbidden", msg)
    denied(await call.list(k, tenant="beta"), 403, "tenant_forbidden", msg)
    denied(await call.delete(k, rid, tenant="beta"), 403, "tenant_forbidden", msg)
    if call.transport == "rest":
        denied(await call.get(k, rid, tenant="beta"), 403, "tenant_forbidden", msg)
        denied(await call.update(k, rid, tenant="beta"), 403, "tenant_forbidden", msg)
    assert env.exists("beta", rid)
    assert env.mstore.count("memories_beta") == 1


async def test_beta_id_without_tenant_is_not_found(env, call):
    k = env.key()
    rid = env.seed("beta", "secret")
    assert (await call.delete(k, rid)).not_found
    if call.transport == "rest":
        assert (await call.get(k, rid)).not_found
        assert (await call.update(k, rid)).not_found
    assert env.exists("beta", rid)
    assert env.mstore.get(rid, collection="memories_beta").content == "secret content"


async def test_alpha_reads_never_return_beta_records(env, call):
    k = env.key()
    env.seed("alpha", "mine content")
    env.seed("beta", "theirs content")
    assert names(await call.search(k)) == {"mine content"}
    assert names(await call.list(k)) == {"mine content"}


async def test_save_lands_only_in_resolved_tenant(env, call):
    k = env.key()
    assert (await call.save(k, name="fresh")).ok
    assert env.mstore.find_by_name("fresh", "global", collection=COLLECTION) is not None
    assert env.mstore.find_by_name("fresh", "global", collection="memories_beta") is None


async def test_unknown_and_existing_unheld_tenant_look_identical(env, call):
    """Rule 1: authorize before lookup, so 'gamma' (absent) and 'beta' (present) answer alike."""
    k = env.key()
    beta = await call.list(k, tenant="beta")
    gamma = await call.list(k, tenant="gamma")
    denied(beta, 403, "tenant_forbidden", "No access to tenant")
    denied(gamma, 403, "tenant_forbidden", "No access to tenant")
    assert beta.message.replace("beta", "X") == gamma.message.replace("gamma", "X")


# ── tenant resolution ───────────────────────────────────────────────────────────
async def test_multi_tenant_key_without_tenant_is_400(env, call):
    k = env.key(tenants=("alpha", "beta"))
    denied(await call.list(k), 400, "tenant_required", "This key covers several tenants; pass tenant")
    denied(await call.save(k), 400, "tenant_required", "pass tenant")
    assert (await call.save(k, name="in-beta", tenant="beta")).ok
    assert env.mstore.find_by_name("in-beta", "global", collection="memories_beta") is not None


async def test_single_tenant_key_without_tenant_uses_its_tenant(env, call):
    k = env.key(tenants=("beta",))
    env.seed("beta", "beta content")
    assert names(await call.list(k)) == {"beta content"}
    assert (await call.save(k, name="b2")).ok
    assert env.mstore.find_by_name("b2", "global", collection="memories_beta") is not None


async def test_legacy_principal_defaults_to_default_tenant(env, call):
    env.seed("alpha", "a content")
    env.seed("beta", "b content")
    assert names(await call.list(LEGACY)) == {"a content"}
    assert names(await call.list(LEGACY, tenant="beta")) == {"b content"}


async def test_ui_without_default_tenant_needs_tenant(env, call):
    env.admin._settings.pop("default_tenant")
    env.app.state.authenticator.invalidate()
    denied(await call.list(ui_headers()), 400, "tenant_required", "pass tenant")
    assert (await call.list(ui_headers(), tenant="alpha")).ok


async def test_rest_tenant_header_and_query(env):
    k = env.key(tenants=("alpha", "beta"))
    c = Caller("rest", env.client)
    env.seed("beta", "b content")
    r = await c._rest("GET", "/memories", k, headers={"X-MT-Tenant": "beta"})
    assert {m["name"] for m in r.data["memories"]} == {"b content"}
    r = await c._rest("GET", "/memories", k, tenant="beta", headers={"X-MT-Tenant": "beta"})
    assert r.ok
    r = await c._rest("GET", "/memories", k, tenant="alpha", headers={"X-MT-Tenant": "beta"})
    assert (r.status, r.code) == (400, "tenant_conflict")


# ── access level, denied tools ──────────────────────────────────────────────────
async def test_read_only_key_cannot_write(env, call):
    k = env.key(access="read")
    rid = env.seed("alpha", "kept")
    denied(await call.save(k), 403, "key_read_only", "read-only")
    denied(await call.delete(k, rid), 403, "key_read_only", "read-only")
    assert (await call.search(k)).ok
    assert (await call.list(k)).ok
    assert env.exists("alpha", rid)


async def test_denied_tool_is_403(env, call):
    k = env.key(denied=("search_memories", "delete_memory"))
    rid = env.seed("alpha", "kept")
    denied(await call.search(k), 403, "tool_denied", "search_memories is denied")
    denied(await call.delete(k, rid), 403, "tool_denied", "delete_memory is denied")
    assert (await call.list(k)).ok
    assert (await call.save(k)).ok
    assert env.exists("alpha", rid)


async def test_viewer_cannot_write_editor_can(env, call):
    viewer = ui_headers(groups=(G_VIEW,))
    editor = ui_headers(groups=(G_EDIT,))
    denied(await call.save(viewer), 403, "role_insufficient", "Role 'viewer'")
    assert (await call.list(viewer)).ok
    assert (await call.save(editor, name="by-editor")).ok
    rid = env.seed("alpha", "doomed")
    denied(await call.delete(viewer, rid), 403, "role_insufficient", "Role 'viewer'")
    assert (await call.delete(editor, rid)).ok
    assert not env.exists("alpha", rid)


async def test_ui_role_on_alpha_does_not_reach_beta(env, call):
    denied(await call.list(ui_headers(groups=(G_EDIT,)), tenant="beta"), 403, "tenant_forbidden",
           "No access to tenant 'beta'")


async def test_ui_wildcard_admin_reaches_every_tenant(env, call):
    env.seed("beta", "b content")
    assert names(await call.list(ui_headers(groups=(G_ADMIN,)), tenant="beta")) == {"b content"}


async def test_disabled_tenant_is_403(env, call):
    k = env.key(tenants=("alpha", "beta"))
    outsider = env.key(tenants=("alpha",))
    env.app.state.tenants.set_status("beta", "disabled")
    denied(await call.list(k, tenant="beta"), 403, "tenant_disabled", "Tenant 'beta' is disabled")
    denied(await call.save(k, tenant="beta"), 403, "tenant_disabled", "disabled")
    # a caller who does not hold it still sees only "no access"
    denied(await call.list(outsider, tenant="beta"), 403, "tenant_forbidden", "No access to tenant 'beta'")


# ── source_repo patterns ────────────────────────────────────────────────────────
async def test_source_repo_patterns(env, call):
    k = env.key(patterns=("team-*",))
    inside = env.seed("alpha", "inside content", source_repo="team-one")
    outside = env.seed("alpha", "outside content", source_repo="other")
    env.seed("alpha", "case content", source_repo="Team-two")  # fnmatch is case-sensitive

    denied(await call.save(k, source_repo="other"), 403, "source_repo_not_allowed", "source_repo 'other'")
    assert (await call.save(k, name="ok", source_repo="team-two")).ok
    assert names(await call.list(k)) == {"inside content", "ok"}
    assert names(await call.search(k)) == {"inside content", "ok"}
    assert names(await call.list(k, source_repo="other")) == set()
    assert names(await call.search(k, filter_source_repo="other")) == set()
    assert names(await call.list(k, source_repo="team-one")) == {"inside content"}

    assert (await call.delete(k, outside)).not_found
    assert env.exists("alpha", outside)
    if call.transport == "rest":
        assert (await call.get(k, outside)).not_found
        assert (await call.update(k, outside)).not_found
        assert (await call.get(k, inside)).ok
    assert env.mstore.get(outside, collection=COLLECTION).content == "outside content content"
    assert (await call.delete(k, inside)).ok
    assert not env.exists("alpha", inside)


# ── agent stamping ──────────────────────────────────────────────────────────────
async def test_agent_is_stamped(env, call):
    k = env.key(agent="agent-a")
    cases = [
        (k, "by-key", "spoofed", "agent-a"),
        (ui_headers(name="Ada Example"), "by-ui", "spoofed", "ui:Ada Example"),
        (LEGACY, "by-legacy-declared", "declared-agent", "declared-agent"),
        (LEGACY, "by-legacy-default", None, "claude-code"),
    ]
    for who, name, declared, expected in cases:
        r = await call.save(who, name=name, agent=declared)
        assert r.ok, r
        assert r.data["agent"] == expected, name
        assert env.mstore.find_by_name(name, "global", collection=COLLECTION).agent == expected


# ── allowed_types ───────────────────────────────────────────────────────────────
async def test_allowed_types(env, call):
    k = env.key()
    env.settings(allowed_types=["note", "decision"])
    denied(await call.save(k, type="rant"), 400, "type_not_allowed", "allowed: note, decision")
    assert (await call.save(k, type="decision")).ok
    env.settings(allowed_types=[])
    assert (await call.save(k, name="any", type="rant")).ok


# ── audit ───────────────────────────────────────────────────────────────────────
async def test_writes_and_denials_are_audited(env, call):
    k = env.key(tenants=("alpha",))
    key_id = k.split("_")[1]
    saved = await call.save(k, name="audited")
    rid = saved.data["id"]
    await call.save(k, tenant="beta")
    await call.list(k)  # allowed read: not audited
    await call.delete(k, rid)
    rows = env.audit.rows
    assert rows == [
        (key_id, "save_memory", "alpha", rid, "allowed", None),
        (key_id, "save_memory", "beta", "n1", "denied", {"rule": "tenant_forbidden"}),
        (key_id, "delete_memory", "alpha", rid, "allowed", None),
    ]


async def test_out_of_pattern_get_is_audited_as_denied(env, call):
    k = env.key(patterns=("team-*",))
    outside = env.seed("alpha", "outside content", source_repo="other")
    await call.delete(k, outside)
    assert env.audit.rows[-1][1:] == ("delete_memory", "alpha", outside, "denied",
                                      {"rule": "source_repo_not_allowed"})


# ── legacy mode (no admin store) ────────────────────────────────────────────────
async def test_legacy_mode_rejects_tenant_and_uses_default_collection(legacy_env, legacy_call):
    denied(await legacy_call.list(LEGACY, tenant="alpha"), 400, "tenant_not_supported", "no tenants")
    denied(await legacy_call.save(LEGACY, tenant="alpha"), 400, "tenant_not_supported", "no tenants")
    assert (await legacy_call.save(LEGACY, name="plain", agent="declared")).ok
    rec = legacy_env.mstore.find_by_name("plain", "global", collection=COLLECTION)
    assert rec.agent == "declared"
    assert names(await legacy_call.list(LEGACY)) == {"plain"}


async def test_legacy_mode_rest_tenant_header_rejected(legacy_env):
    r = await Caller("rest", legacy_env.client)._rest("GET", "/memories", LEGACY,
                                                      headers={"X-MT-Tenant": "alpha"})
    assert (r.status, r.code) == (400, "tenant_not_supported")


# ── pure authz units ────────────────────────────────────────────────────────────
def _key(tenants=("alpha",), access="write", denied=(), patterns=()):
    return Principal(kind="key", id="k1", agent="agent-a",
                     roles={t: ("editor" if access == "write" else "viewer") for t in tenants},
                     access=access, denied_tools=frozenset(denied), source_repo_patterns=tuple(patterns))


LEGACY_P = Principal(kind="legacy", id="legacy", agent=None, roles={}, all_tenants=True)


def test_action_sets():
    assert {"search_memories", "list_memories", "kg_find", "kg_for_memory"} <= READ_ACTIONS
    assert {"save_memory", "delete_memory", "kg_batch", "kg_xref", "kg_delete_xref"} <= WRITE_ACTIONS
    assert not READ_ACTIONS & WRITE_ACTIONS
    assert set(REST_ACTION.values()) <= READ_ACTIONS | WRITE_ACTIONS


def test_action_sets_cover_every_registered_tool():
    from mcp.server.mcpserver import MCPServer
    from memory_mcp.kg.tools import register
    server = MCPServer("t")
    register(server, object(), object())
    kg_tools = {t.name for t in asyncio.run(server.list_tools())}
    memory_tools = {"save_memory", "search_memories", "list_memories", "delete_memory"}
    assert kg_tools | memory_tools == READ_ACTIONS | WRITE_ACTIONS


def test_authorize_checks_tenant_before_tool_and_access():
    p = _key(tenants=("alpha",), access="read", denied=("save_memory",))
    with pytest.raises(AuthzError) as e:
        authorize(p, "save_memory", "beta")
    assert e.value.code == "tenant_forbidden"
    with pytest.raises(AuthzError) as e:
        authorize(p, "save_memory", "alpha")
    assert e.value.code == "key_read_only"
    authorize(_key(denied=("kg_find",)), "list_memories", "alpha")
    with pytest.raises(AuthzError) as e:
        authorize(_key(denied=("kg_find",)), "kg_find", "alpha")
    assert e.value.code == "tool_denied"


def test_authorize_unknown_action_needs_write():
    with pytest.raises(AuthzError):
        authorize(_key(access="read"), "something_new", "alpha")


def test_key_cannot_use_wildcard_role():
    p = Principal(kind="key", id="k", agent="a", roles={"*": "editor"})
    with pytest.raises(AuthzError):
        authorize(p, "list_memories", "alpha")


def test_legacy_may_do_everything():
    authorize(LEGACY_P, "kg_delete_entity", "anything")


def test_resolve_tenant():
    s = {"default_tenant": "alpha"}
    assert resolve_tenant(_key(tenants=("beta",)), None, None, s) == "beta"
    assert resolve_tenant(_key(tenants=("alpha", "beta")), "beta", None, s) == "beta"
    with pytest.raises(AuthzError) as e:
        resolve_tenant(_key(tenants=("alpha", "beta")), None, None, s)
    assert (e.value.status, e.value.code) == (400, "tenant_required")
    assert resolve_tenant(LEGACY_P, None, None, s) == "alpha"
    ui = Principal(kind="ui", id="u", agent=None, roles={"beta": "viewer"}, user_name="U")
    assert resolve_tenant(ui, None, None, s) == "alpha"
    with pytest.raises(AuthzError):
        resolve_tenant(ui, None, None, {})


@dataclass
class _T:
    id: str
    status: str = "active"


@dataclass
class _Tenants:
    items: list = field(default_factory=list)

    def all(self):
        return self.items


def test_visible_tenants_only_active_and_held():
    ts = _Tenants([_T("alpha"), _T("beta"), _T("gamma", "disabled")])
    assert visible_tenants(_key(tenants=("alpha", "gamma")), ts) == ["alpha"]
    assert visible_tenants(LEGACY_P, ts) == ["alpha", "beta"]
    ui_star = Principal(kind="ui", id="u", agent=None, roles={"*": "admin"}, user_name="U")
    assert visible_tenants(ui_star, ts) == ["alpha", "beta"]
    ui = Principal(kind="ui", id="u", agent=None, roles={"beta": "viewer"}, user_name="U")
    assert visible_tenants(ui, ts) == ["beta"]


def test_repo_allowed():
    assert repo_allowed(_key(), "anything")
    p = _key(patterns=("team-*", "exact"))
    assert repo_allowed(p, "team-x")
    assert repo_allowed(p, "exact")
    assert not repo_allowed(p, "Team-x")
    assert not repo_allowed(p, "other")


def test_stamped_agent():
    assert stamped_agent(_key(), "spoof") == "agent-a"
    ui = Principal(kind="ui", id="u", agent=None, roles={}, user_name="Ada")
    assert stamped_agent(ui, "spoof") == "ui:Ada"
    assert stamped_agent(LEGACY_P, "mine") == "mine"
    assert stamped_agent(LEGACY_P, None) == "claude-code"


# ── server wiring ───────────────────────────────────────────────────────────────
def test_build_admin_store_only_with_dsn():
    from memory_mcp.server import build_admin_store
    assert build_admin_store(_cfg(admin_pg_dsn=None)) is None
    with patch("memory_mcp.admin.pg_store.PgAdminStore") as pg:
        store = build_admin_store(_cfg(admin_pg_dsn="postgresql://unused"))
    pg.assert_called_once_with("postgresql://unused")
    assert store is pg.return_value
    store.ensure_schema.assert_called_once_with()


def test_create_app_bootstraps_and_exposes_state():
    e = _build()
    assert e.admin.get_settings()["default_tenant"] == "alpha"
    assert e.app.state.tenants.collection_for("alpha") == COLLECTION
    assert e.app.state.admin_store is e.admin
    assert e.app.state.audit is e.audit


# ── fix round 1 ─────────────────────────────────────────────────────────────────
async def test_rest_patch_denied_for_read_only_key_and_viewer(env):
    c = Caller("rest", env.client)
    rid = env.seed("alpha", "kept")
    denied(await c.update(env.key(access="read"), rid), 403, "key_read_only", "read-only")
    denied(await c.update(ui_headers(groups=(G_VIEW,)), rid), 403, "role_insufficient", "Role 'viewer'")
    assert env.mstore.get(rid, collection=COLLECTION).content == "kept content"


async def test_rest_patch_success_is_audited(env):
    k = env.key()
    key_id = k.split("_")[1]
    rid = env.seed("alpha", "kept")
    r = await Caller("rest", env.client).update(k, rid, content="new text")
    assert r.ok and r.data["content"] == "new text"
    assert env.audit.rows == [(key_id, "save_memory", "alpha", rid, "allowed", None)]


async def test_each_rest_route_uses_its_rest_action(env):
    """The action authorized and audited per route is the one REST_ACTION names."""
    c = Caller("rest", env.client)
    k = env.key()
    rid = env.seed("beta", "x")
    calls = {
        ("GET", "/memories"): lambda: c.list(k, tenant="beta"),
        ("GET", "/memories/{id}"): lambda: c.get(k, rid, tenant="beta"),
        ("POST", "/memories/search"): lambda: c.search(k, tenant="beta"),
        ("POST", "/memories"): lambda: c.save(k, tenant="beta"),
        ("PATCH", "/memories/{id}"): lambda: c.update(k, rid, tenant="beta"),
        ("DELETE", "/memories/{id}"): lambda: c.delete(k, rid, tenant="beta"),
    }
    assert set(calls) == set(REST_ACTION)
    for route, fn in calls.items():
        denied(await fn(), 403, "tenant_forbidden", "No access")
        assert env.audit.rows[-1][1] == REST_ACTION[route], route


async def test_store_outage_is_audited_as_error_not_denial(env, call):
    k = env.key()
    assert (await call.list(k)).ok  # key now known

    def down():
        raise RuntimeError("db down")
    env.admin.get_settings = down
    env.app.state.authenticator.invalidate()
    r = await call.list(k)
    assert not r.ok and "Admin store unavailable" in r.message
    if r.status is not None:
        assert (r.status, r.code) == (503, "store_unavailable")
    assert env.audit.rows[-1][4:] == ("error", {"rule": "store_unavailable"})


def test_authenticator_settings_public_cached_and_invalidated():
    from memory_mcp.admin.auth import Authenticator
    admin = InMemoryAdminStore()
    admin.put_settings({"default_tenant": "alpha"}, by="t")
    a = Authenticator(_cfg(), admin)
    assert a.settings()["default_tenant"] == "alpha"
    admin.put_settings({"default_tenant": "beta"}, by="t")
    assert a.settings()["default_tenant"] == "alpha"  # cached
    a.invalidate()
    assert a.settings()["default_tenant"] == "beta"
    assert Authenticator(_cfg(), None).settings() == {}


def test_authenticator_settings_outage_is_503():
    from memory_mcp.admin.auth import Authenticator
    admin = InMemoryAdminStore()

    def down():
        raise RuntimeError("db down")
    admin.get_settings = down
    with pytest.raises(AuthzError) as e:
        Authenticator(_cfg(), admin).settings()
    assert (e.value.status, e.value.code) == (503, "store_unavailable")


async def test_kg_pg_dsn_alone_keeps_legacy_mode():
    from memory_mcp.server import create_app
    qc = QdrantClient(":memory:")
    mstore = MemoryStore("unused", embedder=StubEmbedder(), client=qc)
    cfg = _cfg(ui_token=None, kg_pg_dsn="postgresql://kg-only", admin_pg_dsn=None)
    with patch("memory_mcp.server.load_config", return_value=cfg), \
         patch("memory_mcp.admin.pg_store.PgAdminStore") as pg:
        app = create_app(memory_store=mstore)
    pg.assert_not_called()
    assert app.state.admin_store is None and app.state.tenants is None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        rest = Caller("rest", c)
        assert (await rest.save(LEGACY, name="plain")).ok
        r = await rest.list(LEGACY, tenant="alpha")
        assert (r.status, r.code) == (400, "tenant_not_supported")
    assert mstore.find_by_name("plain", "global", collection=COLLECTION) is not None

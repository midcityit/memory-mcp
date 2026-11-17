"""POST /admin/copy and copy_memories (RCL-73 task 8, spec 5.8)."""
from __future__ import annotations

import pytest

from memory_mcp.admin.copy import copy_memories
from memory_mcp.admin.models import AuthzError, RoleMapping
from tests.admin._admin_app import G_ADMIN, G_BADMIN, G_EDIT, G_TADMIN, G_VIEW, collection, serve, ui

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect")

G_BOTH = "00000000-0000-0000-0000-0000000000f2"  # viewer on alpha, admin on beta


@pytest.fixture
async def env():
    async for e in serve():
        e.admin.replace_mappings(e.admin.list_mappings() + [
            RoleMapping(G_BOTH, "alpha", "viewer"), RoleMapping(G_BOTH, "beta", "admin")], by="test")
        e.app.state.authenticator.invalidate()
        yield e


def _point(env, tenant: str, rid: str):
    got = env.mstore.client.retrieve(collection(tenant), ids=[rid], with_payload=True, with_vectors=True)
    return got[0] if got else None


def _count(env, tenant: str) -> int:
    return env.mstore.count(collection(tenant))


async def _copy(env, headers, **body):
    body = {"from": "alpha", "to": "beta", "ids": [], "dry_run": False} | body
    return await env.client.post("/admin/copy", headers=headers, json=body)


async def test_copy_creates_new_id_with_provenance_and_same_vector(env):
    src = env.seed("alpha", "deploy notes", source_repo="org/app", tags=["ops"], type="decision")
    before = _point(env, "alpha", src)
    resp = await _copy(env, ui(G_BOTH, user="user-9"), ids=[src])
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["updated"] == [] and result["missing"] == []
    [created] = result["created"]
    assert created["name"] == "deploy notes" and created["id"] != src

    copy = _point(env, "beta", created["id"])
    assert copy.payload["content"] == before.payload["content"]
    assert copy.payload["type"] == "decision" and copy.payload["tags"] == ["ops"]
    assert copy.payload["source_repo"] == "org/app"
    prov = copy.payload["copied_from"]
    assert prov["tenant"] == "alpha" and prov["id"] == src and prov["by"] == "Ada Example" and prov["at"]
    assert copy.vector == pytest.approx(before.vector)

    # Source untouched.
    after = _point(env, "alpha", src)
    assert after.payload == before.payload and after.vector == before.vector
    assert _count(env, "alpha") == 1

    rows = env.audit(action="copy_memory")
    assert [(r["tenant"], r["target"], r["outcome"], r["detail"]) for r in rows] == [
        ("beta", created["id"], "allowed", {"from": "alpha", "source_id": src})]
    assert rows[0]["principal_id"] == "user-9"


async def test_copy_updates_instead_of_duplicating(env):
    src = env.seed("alpha", "runbook", source_repo="org/app", content="new text", tags=["v2"])
    dest = env.seed("beta", "runbook", source_repo="org/app", content="old text", tags=["v1"])
    other = env.seed("beta", "runbook", source_repo="org/other", content="unrelated")
    resp = await _copy(env, ui(G_ADMIN), ids=[src])
    assert resp.json() == {"created": [], "updated": [{"id": dest, "name": "runbook"}], "missing": []}
    assert _count(env, "beta") == 2
    p = _point(env, "beta", dest)
    assert p.payload["content"] == "new text" and p.payload["tags"] == ["v2"]
    assert p.payload["copied_from"]["id"] == src
    assert p.vector == pytest.approx(_point(env, "alpha", src).vector)
    assert _point(env, "beta", other).payload["content"] == "unrelated"
    assert [r["target"] for r in env.audit(action="copy_memory")] == [dest]

    # Copying again updates the same memory again.
    again = (await _copy(env, ui(G_ADMIN), ids=[src])).json()
    assert again["updated"] == [{"id": dest, "name": "runbook"}] and _count(env, "beta") == 2


async def test_dry_run_writes_nothing_but_reports(env):
    new = env.seed("alpha", "fresh")
    existing = env.seed("alpha", "shared")
    dest = env.seed("beta", "shared", content="keep me")
    resp = await _copy(env, ui(G_ADMIN), ids=[new, existing, "11111111-1111-1111-1111-111111111111"],
                       dry_run=True)
    result = resp.json()
    assert [c["name"] for c in result["created"]] == ["fresh"]
    assert result["updated"] == [{"id": dest, "name": "shared"}]
    assert result["missing"] == ["11111111-1111-1111-1111-111111111111"]
    assert _count(env, "beta") == 1 and _point(env, "beta", dest).payload["content"] == "keep me"
    assert "copied_from" not in _point(env, "beta", dest).payload
    assert env.audit(action="copy_memory") == []


async def test_missing_ids_are_reported(env):
    src = env.seed("alpha", "real")
    beta_only = env.seed("beta", "elsewhere")
    resp = await _copy(env, ui(G_ADMIN), ids=[src, "not-a-uuid", beta_only, src], to="gamma")
    result = resp.json()
    assert [c["name"] for c in result["created"]] == ["real"]  # duplicates in ids copied once
    assert result["missing"] == ["not-a-uuid", beta_only]


async def test_same_name_twice_in_one_request_is_one_memory(env):
    a = env.seed("alpha", "dup", content="first")
    b = env.seed("alpha", "dup", content="second")
    dry = (await _copy(env, ui(G_ADMIN), ids=[a, b], dry_run=True)).json()
    real = (await _copy(env, ui(G_ADMIN), ids=[a, b])).json()
    assert len(dry["created"]) == len(real["created"]) == 1
    assert len(dry["updated"]) == len(real["updated"]) == 1
    assert real["updated"][0]["id"] == real["created"][0]["id"]
    assert _count(env, "beta") == 1


@pytest.mark.parametrize("groups,status", [
    ((G_BOTH,), 200),     # viewer on source, admin on destination
    ((G_ADMIN,), 200),    # instance admin
    ((G_BADMIN,), 403),   # admin on destination, no role on source
    ((G_TADMIN,), 403),   # admin on source, only viewer on destination
    ((G_VIEW,), 403),
    ((G_EDIT,), 403),
])
async def test_copy_permissions(env, groups, status):
    src = env.seed("alpha", "x")
    resp = await _copy(env, ui(*groups), ids=[src])
    assert resp.status_code == status, resp.text
    if status == 403:
        assert _count(env, "beta") == 0
        row = env.audit(action="copy_memory")[0]
        assert row["outcome"] == "denied" and row["tenant"] == "beta"


async def test_copy_request_validation(env):
    h = ui(G_ADMIN)
    resp = await _copy(env, h, **{"to": "alpha"})
    assert resp.status_code == 400 and resp.json()["code"] == "same_tenant"
    ids = [f"00000000-0000-0000-0000-{i:012d}" for i in range(501)]
    resp = await _copy(env, h, ids=ids)
    assert resp.status_code == 400 and resp.json()["code"] == "too_many_ids"
    assert (await _copy(env, h, ids=ids[:500], dry_run=True)).status_code == 200
    assert (await _copy(env, h, dry_run="yes")).status_code == 400
    assert (await _copy(env, h, ids="abc")).status_code == 400


async def test_copy_into_disabled_tenant_is_refused(env):
    env.app.state.tenants.set_status("beta", "disabled")
    src = env.seed("alpha", "x")
    resp = await _copy(env, ui(G_ADMIN), ids=[src])
    assert resp.status_code == 403 and resp.json()["code"] == "tenant_disabled"


async def test_copy_memories_unit_rejects_same_tenant(env):
    with pytest.raises(AuthzError) as e:
        copy_memories(env.mstore, env.app.state.tenants, "alpha", "alpha", [], False, by="t")
    assert e.value.status == 400

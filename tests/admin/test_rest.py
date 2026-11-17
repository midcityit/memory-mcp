"""Admin REST API (RCL-73 task 8): who may call what, keys, access, settings, audit, groups."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from memory_mcp.admin.authz import READ_ACTIONS, WRITE_ACTIONS
from tests.admin._admin_app import (
    G_ADMIN, G_BADMIN, G_EDIT, G_TADMIN, G_VIEW, LEGACY, UI, bearer, serve, ui,
)

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect")

G_NEW = "00000000-0000-0000-0000-0000000000f1"


@pytest.fixture
async def env():
    async for e in serve():
        yield e


@pytest.fixture
async def legacy_env():
    async for e in serve(multitenant=False):
        yield e


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key_body(**over) -> dict:
    body = {"agent": "agent-x", "tenants": ["alpha"], "access": "read", "denied_tools": [],
            "source_repo_patterns": [], "expires_in_days": 30}
    body.update(over)
    return body


# ── role matrix ───────────────────────────────────────────────────────────────
ANY_UI = {"viewer", "editor", "tenant_admin", "instance_admin"}
TENANT_ADMIN = {"tenant_admin", "instance_admin"}
INSTANCE_ADMIN = {"instance_admin"}

ENDPOINTS = [
    ("GET", "/admin/me", None, ANY_UI),
    ("POST", "/admin/groups/known", {"group_ids": [G_VIEW]}, ANY_UI),
    ("GET", "/admin/tenants", None, INSTANCE_ADMIN),
    ("POST", "/admin/tenants", {"id": "delta", "name": "Delta"}, INSTANCE_ADMIN),
    ("PATCH", "/admin/tenants/beta", {"name": "Beta 2"}, INSTANCE_ADMIN),
    ("GET", "/admin/keys", None, TENANT_ADMIN),
    ("POST", "/admin/keys", _key_body(), TENANT_ADMIN),
    ("POST", "/admin/keys/{key}/rotate", None, TENANT_ADMIN),
    ("POST", "/admin/keys/{key}/revoke", None, TENANT_ADMIN),
    ("GET", "/admin/access", None, INSTANCE_ADMIN),
    ("PUT", "/admin/access", {"mappings": [{"group_id": G_VIEW, "label": "", "tenant": "alpha",
                                            "role": "viewer"}]}, INSTANCE_ADMIN),
    ("GET", "/admin/audit?tenant=alpha", None, TENANT_ADMIN),
    ("GET", "/admin/settings", None, INSTANCE_ADMIN),
    ("PUT", "/admin/settings", {"instance_name": "Renamed"}, INSTANCE_ADMIN),
    ("POST", "/admin/copy", {"from": "beta", "to": "alpha", "ids": [], "dry_run": True}, TENANT_ADMIN),
    ("POST", "/admin/legacy-token/retire", None, INSTANCE_ADMIN),
    ("GET", "/admin/tools", None, TENANT_ADMIN),
]
CALLERS = ["viewer", "editor", "tenant_admin", "instance_admin", "key", "legacy"]


def _caller(env, who: str) -> dict:
    return {
        "viewer": lambda: ui(G_VIEW), "editor": lambda: ui(G_EDIT),
        "tenant_admin": lambda: ui(G_TADMIN), "instance_admin": lambda: ui(G_ADMIN),
        "key": lambda: bearer(env.key(tenants=("alpha",))[0]), "legacy": lambda: bearer(LEGACY),
    }[who]()


@pytest.mark.parametrize("method,path,body,allowed", ENDPOINTS, ids=[f"{m} {p}" for m, p, _, _ in ENDPOINTS])
@pytest.mark.parametrize("who", CALLERS)
async def test_role_matrix(env, method, path, body, allowed, who):
    _, key_id = env.key(tenants=("alpha",), agent="target")
    headers = _caller(env, who)
    resp = await env.client.request(method, path.format(key=key_id), headers=headers, json=body)
    if who in allowed:
        assert resp.status_code < 400, resp.text
    else:
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"]
        denied = env.audit(outcome="denied")
        assert denied and denied[0]["detail"] == {"rule": resp.json()["code"]}


@pytest.mark.parametrize("method,path,body,allowed", ENDPOINTS, ids=[f"{m} {p}" for m, p, _, _ in ENDPOINTS])
async def test_legacy_mode_returns_501(legacy_env, method, path, body, allowed):
    resp = await legacy_env.client.request(method, path.format(key="abcdefgh"), headers=bearer(LEGACY),
                                           json=body)
    if path == "/admin/me":
        assert resp.status_code == 200
        me = resp.json()
        assert me["legacy_mode"] is True and me["roles"] == {} and me["tenants"] == []
        assert me["instance_admin"] is False
    else:
        assert resp.status_code == 501
        assert resp.json() == {"detail": "Admin API not available"}


async def test_unauthenticated_admin_call_is_401(env):
    resp = await env.client.get("/admin/me")
    assert resp.status_code == 401


# ── /admin/me and /admin/tools ──────────────────────────────────────────────────
async def test_me_for_viewer_lists_only_held_tenants(env):
    me = (await env.client.get("/admin/me", headers=ui(G_VIEW, name="Ada Example"))).json()
    assert me == {
        "legacy_mode": False, "user": {"id": "user-1", "name": "Ada Example"},
        "roles": {"alpha": "viewer"}, "instance_admin": False,
        "tenants": [{"id": "alpha", "name": "Alpha", "status": "active"}],
        "settings": {"instance_name": "memory-twin", "default_tenant": "alpha"},
    }


async def test_me_for_instance_admin_lists_all_tenants_with_status(env):
    env.app.state.tenants.set_status("gamma", "disabled")
    me = (await env.client.get("/admin/me", headers=ui(G_ADMIN))).json()
    assert me["instance_admin"] is True and me["roles"] == {"*": "admin"}
    assert [(t["id"], t["status"]) for t in me["tenants"]] == [
        ("alpha", "active"), ("beta", "active"), ("gamma", "disabled")]


async def test_tools_lists_every_known_tool_sorted(env):
    tools = (await env.client.get("/admin/tools", headers=ui(G_TADMIN))).json()["tools"]
    assert tools == sorted(READ_ACTIONS | WRITE_ACTIONS)


# ── groups/known ──────────────────────────────────────────────────────────────
async def test_groups_known_for_sign_in_principal(env):
    unknown = "00000000-0000-0000-0000-0000000000ff"
    resp = await env.client.post("/admin/groups/known", headers=bearer(UI),
                                 json={"group_ids": [unknown, G_VIEW.upper(), G_ADMIN, G_BADMIN]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"group_ids": [G_VIEW.upper(), G_ADMIN, G_BADMIN]}


async def test_groups_known_accepts_1000_ids_and_rejects_more(env):
    ids = [f"00000000-0000-0000-0000-{i:012d}" for i in range(1000)]
    resp = await env.client.post("/admin/groups/known", headers=bearer(UI), json={"group_ids": ids})
    assert resp.status_code == 200
    resp = await env.client.post("/admin/groups/known", headers=bearer(UI), json={"group_ids": ids + [G_VIEW]})
    assert resp.status_code == 400 and resp.json()["code"] == "too_many_groups"


async def test_groups_known_refuses_keys_and_bad_bodies(env):
    full, _ = env.key()
    assert (await env.client.post("/admin/groups/known", headers=bearer(full),
                                  json={"group_ids": []})).status_code == 403
    assert (await env.client.post("/admin/groups/known", headers=bearer(UI),
                                  json={"group_ids": "x"})).status_code == 400
    assert (await env.client.post("/admin/groups/known", headers=bearer(UI) | {"Content-Type": "application/json"},
                                  content=b"not json")).status_code == 400


async def test_sign_in_principal_cannot_use_other_endpoints(env):
    # Without X-MT-User the UI token is only accepted on groups/known.
    assert (await env.client.get("/admin/me", headers=bearer(UI))).status_code == 400


# ── tenants ───────────────────────────────────────────────────────────────────
async def test_create_list_and_patch_tenants(env):
    h = ui(G_ADMIN)
    resp = await env.client.post("/admin/tenants", headers=h,
                                 json={"id": "delta", "name": "Delta", "description": "fourth"})
    assert resp.status_code == 201, resp.text
    assert resp.json() == {"id": "delta", "name": "Delta", "description": "fourth", "status": "active",
                           "memories": 0, "entities": 0, "keys": 0}
    env.seed("alpha", "one")
    env.key(tenants=("alpha", "beta"))
    tenants = {t["id"]: t for t in (await env.client.get("/admin/tenants", headers=h)).json()["tenants"]}
    assert set(tenants) == {"alpha", "beta", "gamma", "delta"}
    assert tenants["alpha"]["memories"] == 1 and tenants["alpha"]["keys"] == 1 and tenants["beta"]["keys"] == 1

    resp = await env.client.patch("/admin/tenants/delta", headers=h,
                                  json={"name": "Delta 2", "status": "disabled"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Delta 2" and resp.json()["status"] == "disabled"
    row = env.audit(action="admin_update_tenant")[0]
    assert row["tenant"] == "delta" and row["outcome"] == "allowed"
    assert env.audit(action="admin_create_tenant")[0]["tenant"] == "delta"


@pytest.mark.parametrize("body,status", [
    ({"id": "alpha", "name": "Dup"}, 409),
    ({"id": "Bad", "name": "x"}, 400),
    ({"id": "a", "name": "x"}, 400),
    ({"id": "has-hyphen", "name": "x"}, 400),  # tenant ids are graph ids: no hyphen
    ({"id": "ok_id", "name": ""}, 400),
    ({"id": "ok_id", "name": "x" * 81}, 400),
    ({"id": "ok_id", "name": "x", "description": "d" * 501}, 400),
    ({"id": "ok_id", "name": "x", "extra": 1}, 400),
])
async def test_create_tenant_validation(env, body, status):
    resp = await env.client.post("/admin/tenants", headers=ui(G_ADMIN), json=body)
    assert resp.status_code == status, resp.text


async def test_patch_tenant_validation(env):
    h = ui(G_ADMIN)
    assert (await env.client.patch("/admin/tenants/nope", headers=h, json={"name": "x"})).status_code == 404
    assert (await env.client.patch("/admin/tenants/beta", headers=h, json={"status": "gone"})).status_code == 400
    assert (await env.client.patch("/admin/tenants/beta", headers=h, json={})).status_code == 400


# ── keys ──────────────────────────────────────────────────────────────────────
async def test_full_key_is_returned_once_and_never_listed_audited_or_logged(env, caplog):
    caplog.set_level(logging.DEBUG)
    resp = await env.client.post("/admin/keys", headers=ui(G_TADMIN),
                                 json=_key_body(tenants=["alpha"], access="write",
                                                denied_tools=["delete_memory"],
                                                source_repo_patterns=["org/*"], expires_in_days=90))
    assert resp.status_code == 201, resp.text
    issued = resp.json()
    full = issued["key"]
    secret = full.split("_", 2)[2]
    record = issued["record"]
    assert full.startswith(f"mt_{record['id']}_")
    assert record["agent"] == "agent-x" and record["tenants"] == ["alpha"] and record["access"] == "write"
    assert record["denied_tools"] == ["delete_memory"] and record["source_repo_patterns"] == ["org/*"]
    assert record["created_by"] == "Ada Example" and record["revoked_at"] is None
    expires = datetime.fromisoformat(record["expires_at"])
    assert abs(expires - (_now() + timedelta(days=90))) < timedelta(minutes=1)
    assert "secret_hash" not in record

    # The key works.
    assert (await env.client.get("/memories", headers=bearer(full))).status_code == 200

    listed = await env.client.get("/admin/keys", headers=ui(G_TADMIN))
    assert [k["id"] for k in listed.json()["keys"]] == [record["id"]]
    listed_record = listed.json()["keys"][0]
    assert listed_record["last_used_at"] is not None  # the call above used it
    assert listed_record | {"last_used_at": None} == record
    stored_hash = env.admin.get_key(record["id"]).secret_hash
    for text in (listed.text, json.dumps(env.audit(), default=str), caplog.text):
        assert full not in text and secret not in text and stored_hash not in text
    assert "secret_hash" not in listed.text
    rows = env.audit(action="admin_create_key")
    assert [(r["tenant"], r["target"], r["outcome"]) for r in rows] == [("alpha", record["id"], "allowed")]


async def test_multi_tenant_key_audits_one_row_per_tenant(env):
    resp = await env.client.post("/admin/keys", headers=ui(G_ADMIN), json=_key_body(tenants=["alpha", "beta"]))
    assert resp.status_code == 201
    assert sorted(r["tenant"] for r in env.audit(action="admin_create_key")) == ["alpha", "beta"]


async def test_rotate_keeps_scope_and_old_key_expires_in_24h(env):
    h = ui(G_TADMIN)
    created = (await env.client.post("/admin/keys", headers=h, json=_key_body(
        access="write", denied_tools=["kg_link"], source_repo_patterns=["a/*"], expires_in_days=365))).json()
    old = created["record"]
    resp = await env.client.post(f"/admin/keys/{old['id']}/rotate", headers=h)
    assert resp.status_code == 200, resp.text
    new = resp.json()["record"]
    assert resp.json()["key"].startswith(f"mt_{new['id']}_") and new["id"] != old["id"]
    for field in ("agent", "tenants", "access", "denied_tools", "source_repo_patterns"):
        assert new[field] == old[field]
    assert new["rotated_from"] == old["id"]
    assert abs(datetime.fromisoformat(new["expires_at"]) - (_now() + timedelta(days=365))) < timedelta(minutes=1)
    old_expiry = env.admin.get_key(old["id"]).expires_at
    assert abs(old_expiry - (_now() + timedelta(hours=24))) < timedelta(minutes=1)
    # Both keys work during the grace period.
    assert (await env.client.get("/memories", headers=bearer(created["key"]))).status_code == 200
    assert (await env.client.get("/memories", headers=bearer(resp.json()["key"]))).status_code == 200
    row = env.audit(action="admin_rotate_key")[0]
    assert row["target"] == new["id"] and row["detail"] == {"rotated_from": old["id"]}
    assert resp.json()["key"] not in json.dumps(env.audit(), default=str)


async def test_rotate_keeps_an_earlier_expiry(env):
    soon = _now() + timedelta(hours=1)
    _, key_id = env.key(expires_at=soon)
    assert (await env.client.post(f"/admin/keys/{key_id}/rotate", headers=ui(G_TADMIN))).status_code == 200
    assert env.admin.get_key(key_id).expires_at == soon


async def test_revoke_makes_the_key_unusable(env):
    full, key_id = env.key()
    assert (await env.client.get("/memories", headers=bearer(full))).status_code == 200  # cached now
    resp = await env.client.post(f"/admin/keys/{key_id}/revoke", headers=ui(G_TADMIN))
    assert resp.status_code == 204 and resp.content == b""
    assert (await env.client.get("/memories", headers=bearer(full))).status_code == 401
    assert (await env.client.post(f"/admin/keys/{key_id}/revoke", headers=ui(G_TADMIN))).status_code == 404
    assert (await env.client.post("/admin/keys/zzzzzzzz/revoke", headers=ui(G_TADMIN))).status_code == 404
    assert (await env.client.post(f"/admin/keys/{key_id}/rotate", headers=ui(G_TADMIN))).status_code == 400
    assert env.audit(action="admin_revoke_key", outcome="allowed")[0]["target"] == key_id


async def test_tenant_admin_cannot_reach_keys_outside_their_tenants(env):
    h = ui(G_TADMIN)  # admin on alpha, viewer on beta
    resp = await env.client.post("/admin/keys", headers=h, json=_key_body(tenants=["alpha", "beta"]))
    assert resp.status_code == 403 and resp.json()["code"] == "tenant_forbidden"
    _, mixed = env.key(tenants=("alpha", "beta"))
    _, mine = env.key(tenants=("alpha",))
    listed = (await env.client.get("/admin/keys", headers=h)).json()["keys"]
    assert [k["id"] for k in listed] == [mine]
    assert (await env.client.post(f"/admin/keys/{mixed}/rotate", headers=h)).status_code == 403
    assert (await env.client.post(f"/admin/keys/{mixed}/revoke", headers=h)).status_code == 403
    assert env.admin.get_key(mixed).revoked_at is None
    # An instance admin sees both.
    listed = (await env.client.get("/admin/keys", headers=ui(G_ADMIN))).json()["keys"]
    assert {k["id"] for k in listed} == {mixed, mine}


async def test_unknown_tenant_on_key_is_400_without_suggestions(env):
    resp = await env.client.post("/admin/keys", headers=ui(G_ADMIN), json=_key_body(tenants=["alpah"]))
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Unknown tenant 'alpah'", "code": "unknown_tenant"}


@pytest.mark.parametrize("over", [
    {"agent": "A"}, {"agent": "-bad"}, {"agent": "x" * 64}, {"agent": 5},
    {"tenants": []}, {"tenants": "alpha"},
    {"access": "admin"},
    {"expires_in_days": 45}, {"expires_in_days": True}, {"expires_in_days": 30.0}, {"expires_in_days": None},
    {"denied_tools": ["rm_rf"]}, {"denied_tools": "save_memory"},
    {"source_repo_patterns": ["p"] * 21}, {"source_repo_patterns": ["x" * 201]}, {"source_repo_patterns": [""]},
    {"unexpected": 1},
])
async def test_create_key_validation(env, over):
    resp = await env.client.post("/admin/keys", headers=ui(G_ADMIN), json=_key_body(**over))
    assert resp.status_code == 400, resp.text
    assert isinstance(resp.json()["detail"], str) and resp.json()["code"]
    assert env.admin.list_keys() == []


async def test_keys_list_reports_legacy_token_state(env):
    assert (await env.client.get("/memories", headers=bearer(LEGACY))).status_code == 200
    legacy = (await env.client.get("/admin/keys", headers=ui(G_ADMIN))).json()["legacy"]
    assert legacy["enabled"] is True and legacy["last_used_at"] is not None


async def test_retire_legacy_token(env):
    resp = await env.client.post("/admin/legacy-token/retire", headers=ui(G_ADMIN))
    assert resp.status_code == 204
    assert (await env.client.get("/memories", headers=bearer(LEGACY))).status_code == 401
    assert (await env.client.get("/admin/keys", headers=ui(G_ADMIN))).json()["legacy"]["enabled"] is False
    assert env.audit(action="admin_retire_legacy")[0]["outcome"] == "allowed"


# ── access mappings ─────────────────────────────────────────────────────────────
async def test_put_access_then_me_reflects_new_roles(env):
    h = ui(G_ADMIN)
    before = (await env.client.get("/admin/me", headers=ui(G_NEW))).json()
    assert before["roles"] == {} and before["tenants"] == []
    mappings = [
        {"group_id": G_NEW.upper(), "label": "New team", "tenant": "beta", "role": "admin"},
        {"group_id": G_NEW, "label": "", "tenant": "alpha", "role": "viewer"},
        {"group_id": G_VIEW, "label": "Readers", "tenant": "*", "role": "viewer"},
    ]
    resp = await env.client.put("/admin/access", headers=h, json={"mappings": mappings})
    assert resp.status_code == 200, resp.text
    got = (await env.client.get("/admin/access", headers=h)).json()["mappings"]
    assert sorted((m["group_id"], m["label"], m["tenant"], m["role"]) for m in got) == sorted([
        (G_NEW, "New team", "beta", "admin"), (G_NEW, "New team", "alpha", "viewer"),
        (G_VIEW, "Readers", "*", "viewer")])
    assert resp.json()["mappings"] == got
    after = (await env.client.get("/admin/me", headers=ui(G_NEW))).json()
    assert after["roles"] == {"beta": "admin", "alpha": "viewer"}
    assert [t["id"] for t in after["tenants"]] == ["alpha", "beta"]
    # G_TADMIN's mappings were replaced away.
    assert (await env.client.get("/admin/me", headers=ui(G_TADMIN))).json()["roles"] == {}
    assert env.audit(action="admin_put_access")[0]["detail"] == {"mappings": 3}


@pytest.mark.parametrize("row,code", [
    ({"group_id": "not-a-guid", "label": "", "tenant": "alpha", "role": "viewer"}, "invalid_group_id"),
    ({"group_id": G_NEW, "label": "", "tenant": "alpah", "role": "viewer"}, "unknown_tenant"),
    ({"group_id": G_NEW, "label": "", "tenant": "alpha", "role": "owner"}, "invalid_request"),
])
async def test_put_access_validation(env, row, code):
    resp = await env.client.put("/admin/access", headers=ui(G_ADMIN), json={"mappings": [row]})
    assert resp.status_code == 400 and resp.json()["code"] == code
    if code == "unknown_tenant":
        assert "beta" not in resp.json()["detail"] and "alpha" not in resp.json()["detail"]
    assert len(env.admin.list_mappings()) == 5  # unchanged


async def test_put_access_rejects_duplicates(env):
    row = {"group_id": G_NEW, "label": "", "tenant": "alpha", "role": "viewer"}
    dup = dict(row, group_id=G_NEW.upper(), label="other")
    resp = await env.client.put("/admin/access", headers=ui(G_ADMIN), json={"mappings": [row, dup]})
    assert resp.status_code == 400 and resp.json()["code"] == "duplicate_mapping"


# ── settings ──────────────────────────────────────────────────────────────────
async def test_settings_defaults_and_update(env):
    h = ui(G_ADMIN)
    assert (await env.client.get("/admin/settings", headers=h)).json() == {
        "instance_name": "memory-twin", "default_tenant": "alpha", "stale_days": 30, "allowed_types": [],
        "audit_retention_days": 90, "audit_reads": False, "legacy_token_enabled": True,
    }
    resp = await env.client.put("/admin/settings", headers=h, json={
        "instance_name": "  Team memory ", "default_tenant": "beta", "stale_days": 60,
        "allowed_types": ["note", "decision"], "audit_retention_days": 30, "audit_reads": True,
        "legacy_token_enabled": True})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "instance_name": "Team memory", "default_tenant": "beta", "stale_days": 60,
        "allowed_types": ["note", "decision"], "audit_retention_days": 30, "audit_reads": True,
        "legacy_token_enabled": True,
    }
    # Partial update keeps the rest; the change reaches /admin/me immediately.
    await env.client.put("/admin/settings", headers=h, json={"instance_name": "Again"})
    me = (await env.client.get("/admin/me", headers=h)).json()
    assert me["settings"] == {"instance_name": "Again", "default_tenant": "beta"}
    assert env.audit(action="admin_put_settings")[0]["detail"] == {"keys": ["instance_name"]}


@pytest.mark.parametrize("body", [
    {"default_tenant": "nope"}, {"default_tenant": "gamma"},  # gamma is disabled below
    {"instance_name": ""}, {"instance_name": "x" * 61}, {"instance_name": 3},
    {"stale_days": 0}, {"stale_days": 3651}, {"stale_days": "30"}, {"stale_days": True},
    {"audit_retention_days": 1.5},
    {"audit_reads": 1}, {"legacy_token_enabled": "false"},
    {"allowed_types": "note"}, {"allowed_types": [""]}, {"allowed_types": [1]},
    {"group_labels": {}}, {"legacy_last_used_at": "x"},
])
async def test_settings_validation(env, body):
    env.app.state.tenants.set_status("gamma", "disabled")
    before = env.admin.get_settings()
    resp = await env.client.put("/admin/settings", headers=ui(G_ADMIN), json=body)
    assert resp.status_code == 400, resp.text
    assert isinstance(resp.json()["detail"], str)
    assert env.admin.get_settings() == before


async def test_settings_can_turn_legacy_token_back_on(env):
    h = ui(G_ADMIN)
    await env.client.post("/admin/legacy-token/retire", headers=h)
    resp = await env.client.put("/admin/settings", headers=h, json={"legacy_token_enabled": True})
    assert resp.status_code == 200
    assert (await env.client.get("/memories", headers=bearer(LEGACY))).status_code == 200


# ── audit ─────────────────────────────────────────────────────────────────────
def _seed_audit(env, n_alpha: int, n_beta: int) -> None:
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(max(n_alpha, n_beta)):
        for tenant, n in (("alpha", n_alpha), ("beta", n_beta)):
            if i < n:
                env.admin.add_audit({"ts": base + timedelta(seconds=i), "principal_type": "key",
                                     "principal_id": "k", "agent": "a", "acting_user": None,
                                     "tenant": tenant, "action": "save_memory", "target": None,
                                     "outcome": "allowed", "detail": None})


async def test_tenant_admin_must_name_an_administered_tenant(env):
    _seed_audit(env, 2, 2)
    h = ui(G_TADMIN)
    resp = await env.client.get("/admin/audit", headers=h)
    assert resp.status_code == 403 and resp.json()["code"] == "tenant_required"
    resp = await env.client.get("/admin/audit", params={"tenant": "beta"}, headers=h)  # viewer only there
    assert resp.status_code == 403 and resp.json()["code"] == "tenant_forbidden"
    resp = await env.client.get("/admin/audit", params={"tenant": "alpha"}, headers=h)
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert rows and {r["tenant"] for r in rows} == {"alpha"}
    assert set(rows[0]) == {"id", "ts", "principal_type", "principal_id", "agent", "acting_user", "tenant",
                            "action", "target", "outcome", "detail"}
    assert isinstance(rows[0]["id"], str) and datetime.fromisoformat(rows[0]["ts"])


async def test_cursor_cannot_widen_the_tenant_filter(env):
    _seed_audit(env, 150, 150)
    # A cursor taken from an all-tenant page...
    page = (await env.client.get("/admin/audit", headers=ui(G_ADMIN))).json()
    assert {r["tenant"] for r in page["rows"]} == {"alpha", "beta"} and page["next_cursor"]
    # ...is refused without a tenant, and stays inside the tenant when one is named.
    h = ui(G_TADMIN)
    assert (await env.client.get("/admin/audit", params={"cursor": page["next_cursor"]},
                                 headers=h)).status_code == 403
    seen: list[dict] = []
    cursor = page["next_cursor"]
    while cursor:
        resp = (await env.client.get("/admin/audit", params={"tenant": "alpha", "cursor": cursor},
                                     headers=h)).json()
        seen += resp["rows"]
        cursor = resp["next_cursor"]
    assert seen and {r["tenant"] for r in seen} == {"alpha"}


async def test_audit_filters_and_bad_input(env):
    _seed_audit(env, 3, 1)
    h = ui(G_ADMIN)
    rows = (await env.client.get("/admin/audit", params={"tenant": "beta"}, headers=h)).json()["rows"]
    assert len(rows) == 1
    rows = (await env.client.get("/admin/audit", params={"since": "2026-06-01T00:00:01+00:00",
                                                         "until": "2026-06-01T00:00:01Z"},
                                 headers=h)).json()["rows"]
    assert len(rows) == 1
    assert (await env.client.get("/admin/audit", params={"since": "yesterday"}, headers=h)).status_code == 400
    assert (await env.client.get("/admin/audit", params={"cursor": "%%%"}, headers=h)).status_code == 400


# ── fix round 1 ───────────────────────────────────────────────────────────────
TENANT_ID_HINT = "Use 2–32 characters: a lowercase letter first, then lowercase letters, digits or _."


async def test_tenant_ids_follow_the_graph_id_rule(env):
    h = ui(G_ADMIN)
    longest = "t" + "x" * 31
    assert (await env.client.post("/admin/tenants", headers=h, json={"id": longest, "name": "L"})).status_code == 201
    for bad in ("has-hyphen", "t" + "x" * 32):
        resp = await env.client.post("/admin/tenants", headers=h, json={"id": bad, "name": "x"})
        assert resp.status_code == 400
        assert resp.json() == {"detail": TENANT_ID_HINT, "code": "invalid_tenant_id"}


async def test_default_tenant_cannot_be_disabled(env):
    h = ui(G_ADMIN)
    resp = await env.client.patch("/admin/tenants/alpha", headers=h, json={"status": "disabled"})
    assert resp.status_code == 400
    assert resp.json() == {"detail": "This is the default tenant. Choose another default tenant in Settings first.",
                           "code": "default_tenant"}
    assert env.admin.get_tenant("alpha").status == "active"
    # Renaming it is fine, and so is disabling it once another tenant is the default.
    assert (await env.client.patch("/admin/tenants/alpha", headers=h, json={"name": "A"})).status_code == 200
    await env.client.put("/admin/settings", headers=h, json={"default_tenant": "beta"})
    assert (await env.client.patch("/admin/tenants/alpha", headers=h, json={"status": "disabled"})).status_code == 200


async def test_admin_store_outage_is_503_and_audited(env, monkeypatch):
    def down(*_a, **_k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(env.admin, "list_keys", down)
    resp = await env.client.get("/admin/keys", headers=ui(G_ADMIN))
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Admin store unavailable", "code": "store_unavailable"}
    row = env.audit(action="admin_list_keys")[0]
    assert row["outcome"] == "error" and row["detail"] == {"rule": "store_unavailable"}

    # A failing audit write does not change the answer.
    monkeypatch.setattr(env.admin, "add_audit", down)
    resp = await env.client.get("/admin/keys", headers=ui(G_ADMIN))
    assert resp.status_code == 503 and resp.json()["code"] == "store_unavailable"


async def test_non_ui_callers_get_403_even_with_a_malformed_body(env):
    full, _ = env.key()
    for headers in (bearer(full), bearer(LEGACY)):
        for path in ("/admin/keys", "/admin/copy", "/admin/groups/known"):
            resp = await env.client.post(path, headers=headers | {"Content-Type": "application/json"},
                                         content=b"{not json")
            assert resp.status_code == 403, (path, resp.text)
            assert resp.json()["code"] == "ui_only"
    denied = env.audit(outcome="denied")
    assert len(denied) == 6 and all(r["detail"] == {"rule": "ui_only"} for r in denied)


async def test_refused_key_changes_are_audited_under_each_tenant(env):
    h = ui(G_TADMIN)  # admin on alpha only
    resp = await env.client.post("/admin/keys", headers=h, json=_key_body(tenants=["alpha", "beta", "nosuch"]))
    assert resp.status_code == 403
    rows = env.audit(action="admin_create_key", outcome="denied")
    assert sorted(r["tenant"] for r in rows) == ["alpha", "beta"]  # unknown names are not written

    _, mixed = env.key(tenants=("beta", "gamma"))
    for verb in ("rotate", "revoke"):
        assert (await env.client.post(f"/admin/keys/{mixed}/{verb}", headers=h)).status_code == 403
        rows = env.audit(action=f"admin_{verb}_key", outcome="denied")
        assert sorted(r["tenant"] for r in rows) == ["beta", "gamma"]
    # The beta admin can see the probing in beta's audit log.
    seen = (await env.client.get("/admin/audit", params={"tenant": "beta", "outcome": "denied"},
                                 headers=ui(G_BADMIN))).json()["rows"]
    assert {r["action"] for r in seen} == {"admin_create_key", "admin_rotate_key", "admin_revoke_key"}


async def test_rotating_a_key_without_expiry_gives_365_days(env):
    _, key_id = env.key(expires_at=None)
    resp = await env.client.post(f"/admin/keys/{key_id}/rotate", headers=ui(G_TADMIN))
    assert resp.status_code == 200
    expires = datetime.fromisoformat(resp.json()["record"]["expires_at"])
    assert abs(expires - (_now() + timedelta(days=365))) < timedelta(minutes=1)


async def test_keys_cannot_be_issued_onto_disabled_tenants(env):
    _, key_id = env.key(tenants=("alpha", "gamma"))
    env.app.state.tenants.set_status("gamma", "disabled")
    h = ui(G_ADMIN)
    resp = await env.client.post("/admin/keys", headers=h, json=_key_body(tenants=["gamma"]))
    assert resp.status_code == 400 and resp.json()["code"] == "tenant_disabled"
    resp = await env.client.post(f"/admin/keys/{key_id}/rotate", headers=h)
    assert resp.status_code == 400 and resp.json()["code"] == "tenant_disabled"
    assert len(env.admin.list_keys()) == 1


async def test_key_without_tenants_is_invisible(env):
    _, key_id = env.key(tenants=())
    for h in (ui(G_TADMIN), ui(G_ADMIN)):
        assert (await env.client.get("/admin/keys", headers=h)).json()["keys"] == []
        assert (await env.client.post(f"/admin/keys/{key_id}/rotate", headers=h)).status_code == 403


async def test_denied_row_tenant_comes_only_from_well_formed_ids(env):
    resp = await env.client.get("/admin/audit", params={"tenant": "a" * 10_000}, headers=ui(G_TADMIN))
    assert resp.status_code == 403
    row = env.audit(action="admin_audit")[0]
    assert row["outcome"] == "denied" and row["tenant"] is None


async def test_tenant_vanishing_after_create_is_a_clean_error(env, monkeypatch):
    monkeypatch.setattr(env.app.state.tenants, "stats", lambda: [])
    resp = await env.client.post("/admin/tenants", headers=ui(G_ADMIN), json={"id": "delta", "name": "D"})
    assert resp.status_code == 404 and resp.json()["code"] == "unknown_tenant"


async def test_tenant_admin_cannot_put_wildcard_on_a_key(env):
    resp = await env.client.post("/admin/keys", headers=ui(G_TADMIN), json=_key_body(tenants=["alpha", "*"]))
    assert resp.status_code == 403 and resp.json()["code"] == "tenant_forbidden"
    resp = await env.client.post("/admin/keys", headers=ui(G_ADMIN), json=_key_body(tenants=["*"]))
    assert resp.status_code == 400 and resp.json()["code"] == "unknown_tenant"
    assert env.admin.list_keys() == []


async def test_unexpected_admin_error_is_logged_with_traceback(env, monkeypatch, caplog):
    def bug(*_a, **_k):
        raise KeyError("programming error")

    monkeypatch.setattr(env.admin, "list_keys", bug)
    caplog.set_level(logging.WARNING, logger="memory_mcp.admin.rest")
    resp = await env.client.get("/admin/keys", headers=ui(G_ADMIN) | {"X-Secret-Probe": "do-not-log"})
    assert resp.status_code == 503
    [rec] = [r for r in caplog.records if r.name == "memory_mcp.admin.rest"]
    assert rec.levelno == logging.ERROR and rec.exc_info and rec.exc_info[0] is KeyError
    assert "admin_list_keys" in rec.getMessage()
    assert "do-not-log" not in caplog.text and "Bearer" not in caplog.text


async def test_store_errors_are_logged_without_traceback(env, monkeypatch, caplog):
    # CI installs only the `dev` extra; psycopg comes with the `postgres` extra.
    psycopg = pytest.importorskip("psycopg")

    def down(*_a, **_k):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(env.admin, "list_keys", down)
    caplog.set_level(logging.WARNING, logger="memory_mcp.admin.rest")
    resp = await env.client.get("/admin/keys", headers=ui(G_ADMIN))
    assert resp.status_code == 503
    [rec] = [r for r in caplog.records if r.name == "memory_mcp.admin.rest"]
    assert rec.levelno == logging.WARNING and not rec.exc_info
    assert "OperationalError" in rec.getMessage()

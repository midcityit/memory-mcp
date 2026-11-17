import os
from datetime import datetime, timedelta, timezone

import pytest

from memory_mcp.admin.models import ApiKey, RoleMapping, Tenant
from memory_mcp.admin.store import InMemoryAdminStore

PG_DSN = os.environ.get("TEST_ADMIN_PG_DSN")
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _make_pg():
    from memory_mcp.admin.pg_store import PgAdminStore

    s = PgAdminStore(PG_DSN)
    with s.pool.connection() as conn:
        conn.execute("DROP SCHEMA IF EXISTS mt_admin CASCADE")
    s.ensure_schema()
    return s


@pytest.fixture(params=["memory", pytest.param("pg", marks=pytest.mark.skipif(
    not PG_DSN, reason="TEST_ADMIN_PG_DSN not set"))])
def store(request):
    if request.param == "memory":
        yield InMemoryAdminStore()
        return
    s = _make_pg()
    yield s
    with s.pool.connection() as conn:
        conn.execute("DROP SCHEMA IF EXISTS mt_admin CASCADE")
    s.close()


def _tenant(id="alpha", **kw):
    return Tenant(id=id, name=kw.get("name", id.title()), description="d",
                  collection=f"col_{id}", status="active")


def _key(id="k1", **kw):
    base = dict(id=id, agent="agent-a", secret_hash="h", tenants=("alpha",), access="write",
                denied_tools=("delete_memory",), source_repo_patterns=("org/*",), expires_at=None,
                created_at=NOW, created_by="admin", revoked_at=None, last_used_at=None, rotated_from=None)
    base.update(kw)
    return ApiKey(**base)


def _audit(**kw):
    row = dict(ts=NOW, principal_type="key", principal_id="k1", agent="a", acting_user=None,
               tenant="alpha", action="memory.save", target="m1", outcome="ok", detail={"n": 1})
    row.update(kw)
    return row


def test_ping_and_ensure_schema_idempotent(store):
    store.ensure_schema()
    store.ensure_schema()
    store.ping()


def test_tenant_insert_get_list_update(store):
    store.insert_tenant(_tenant("beta"), "admin")
    store.insert_tenant(_tenant("alpha"), "admin")
    assert store.get_tenant("alpha") == _tenant("alpha")
    assert store.get_tenant("nope") is None
    assert [t.id for t in store.list_tenants()] == ["alpha", "beta"]
    u = store.update_tenant("alpha", name="Renamed", status="disabled")
    assert u.name == "Renamed" and u.status == "disabled" and u.description == "d"
    assert store.get_tenant("alpha") == u
    assert store.update_tenant("nope", name="x") is None
    store.delete_tenant("alpha")
    assert store.get_tenant("alpha") is None


def test_duplicate_tenant_raises(store):
    store.insert_tenant(_tenant(), "admin")
    with pytest.raises(ValueError, match="exists"):
        store.insert_tenant(_tenant(), "admin")


def test_key_lifecycle(store):
    k = _key()
    store.insert_key(k)
    assert store.get_key("k1") == k
    assert store.get_key("nope") is None
    store.insert_key(_key("k2", rotated_from="k1", created_at=NOW + timedelta(seconds=1)))
    assert [x.id for x in store.list_keys()] == ["k1", "k2"]
    assert store.get_key("k2").rotated_from == "k1"

    exp = NOW + timedelta(days=30)
    store.set_key_expiry("k1", exp)
    assert store.get_key("k1").expires_at == exp
    store.touch_key("k1", NOW)
    assert store.get_key("k1").last_used_at == NOW

    assert store.revoke_key("k1", NOW) is True
    assert store.get_key("k1").revoked_at == NOW
    assert store.revoke_key("k1", NOW + timedelta(days=1)) is False
    assert store.get_key("k1").revoked_at == NOW
    assert store.revoke_key("nope", NOW) is False


def test_replace_mappings_is_total(store):
    a = RoleMapping("g1", "alpha", "admin")
    b = RoleMapping("g2", "beta", "viewer")
    store.replace_mappings([a, b], "admin")
    assert store.list_mappings() == [a, b]
    store.replace_mappings([b], "admin")
    assert store.list_mappings() == [b]
    store.replace_mappings([], "admin")
    assert store.list_mappings() == []


def test_settings_merge_on_put(store):
    assert store.get_settings() == {}
    store.put_settings({"a": 1, "b": {"x": [1, 2]}}, "admin")
    store.put_settings({"b": "new", "c": None}, "admin")
    assert store.get_settings() == {"a": 1, "b": "new", "c": None}


def test_audit_filters_and_order(store):
    store.add_audit(_audit(ts=NOW, tenant="alpha", outcome="ok"))
    store.add_audit(_audit(ts=NOW + timedelta(minutes=1), tenant="beta", outcome="ok"))
    store.add_audit(_audit(ts=NOW + timedelta(minutes=2), tenant="alpha", outcome="denied",
                           principal_id="k2", action="key.create", detail=None))
    rows, nxt = store.query_audit()
    assert nxt is None
    assert [r["outcome"] for r in rows] == ["denied", "ok", "ok"]
    assert all("id" in r for r in rows)
    assert rows[0]["detail"] is None and rows[2]["detail"] == {"n": 1}
    assert {r["tenant"] for r in store.query_audit(tenant="alpha")[0]} == {"alpha"}
    assert len(store.query_audit(tenant="alpha", outcome="denied")[0]) == 1
    assert [r["principal_id"] for r in store.query_audit(principal="k2")[0]] == ["k2"]
    assert len(store.query_audit(action="key.create")[0]) == 1
    since = NOW + timedelta(seconds=30)
    assert len(store.query_audit(since=since)[0]) == 2
    assert len(store.query_audit(until=since)[0]) == 1


def test_audit_default_ts_and_cursor_paging(store):
    for i in range(5):
        store.add_audit(_audit(ts=NOW, target=f"m{i}"))  # equal ts: id breaks ties
    seen, cursor = [], None
    while True:
        rows, cursor = store.query_audit(limit=2, cursor=cursor)
        seen += [r["target"] for r in rows]
        if cursor is None:
            break
    assert seen == ["m4", "m3", "m2", "m1", "m0"]
    row = _audit()
    del row["ts"]
    store.add_audit(row)
    assert store.query_audit(limit=1)[0][0]["ts"] > NOW


def test_purge_audit(store):
    store.add_audit(_audit(ts=NOW - timedelta(days=10)))
    store.add_audit(_audit(ts=NOW - timedelta(days=1)))
    store.add_audit(_audit(ts=NOW))
    assert store.purge_audit(NOW - timedelta(days=2)) == 1
    assert len(store.query_audit()[0]) == 2
    assert store.purge_audit(NOW - timedelta(days=2)) == 0


def test_duplicate_key_raises(store):
    store.insert_key(_key())
    with pytest.raises(ValueError, match="exists"):
        store.insert_key(_key())


@pytest.mark.skipif(not PG_DSN, reason="TEST_ADMIN_PG_DSN not set")
def test_pg_restricted_role_ensure_schema():
    import psycopg
    from psycopg.conninfo import make_conninfo

    from memory_mcp.admin.pg_store import PgAdminStore

    role = "mt_admin_test_restricted"
    role_dsn = make_conninfo(PG_DSN, user=role, password="pw")
    admin = _make_pg()
    stores = [admin]
    try:
        with admin.pool.connection() as conn:
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                conn.execute(f"DROP OWNED BY {role}")
            conn.execute(f"DROP ROLE IF EXISTS {role}")
            conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'pw'")
            conn.execute(f"GRANT USAGE ON SCHEMA mt_admin TO {role}")
            conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA mt_admin TO {role}")
            conn.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA mt_admin TO {role}")
            conn.execute(f"REVOKE CREATE ON DATABASE {conn.info.dbname} FROM {role}")
        restricted = PgAdminStore(role_dsn)
        stores.append(restricted)
        restricted.ensure_schema()  # schema present: DDL skipped, no privilege error
        restricted.insert_tenant(_tenant(), "admin")
        assert restricted.get_tenant("alpha") == _tenant()

        with admin.pool.connection() as conn:
            conn.execute("DROP SCHEMA mt_admin CASCADE")
        with pytest.raises(RuntimeError, match=r"schema\.sql"):
            restricted.ensure_schema()
    finally:
        for s in stores[1:]:
            s.close()
        with admin.pool.connection() as conn:
            conn.execute("DROP SCHEMA IF EXISTS mt_admin CASCADE")
            conn.execute(f"DROP OWNED BY {role}")
            conn.execute(f"DROP ROLE IF EXISTS {role}")
        admin.close()

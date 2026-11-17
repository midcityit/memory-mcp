"""Postgres implementation of AdminStore (schema mt_admin)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from typing import Any

from memory_mcp.admin.models import ApiKey, RoleMapping, Tenant
from memory_mcp.admin.store import decode_cursor, encode_cursor

_LOCK_ID = 7300073  # schema creation
_BOOTSTRAP_LOCK_ID = 7300074  # tenant bootstrap
_TABLES = ("tenant", "api_key", "role_mapping", "setting", "audit_log")
_KEY_COLS = ("id, agent, secret_hash, tenants, access, denied_tools, source_repo_patterns, "
             "expires_at, created_at, created_by, revoked_at, last_used_at, rotated_from")
_AUDIT_COLS = ("id", "ts", "principal_type", "principal_id", "agent", "acting_user",
               "tenant", "action", "target", "outcome", "detail")


def _tenant(r: dict) -> Tenant:
    return Tenant(id=r["id"], name=r["name"], description=r["description"],
                  collection=r["collection"], status=r["status"])


def _key(r: dict) -> ApiKey:
    return ApiKey(
        id=r["id"], agent=r["agent"], secret_hash=r["secret_hash"],
        tenants=tuple(r["tenants"] or ()), access=r["access"],
        denied_tools=tuple(r["denied_tools"] or ()),
        source_repo_patterns=tuple(r["source_repo_patterns"] or ()),
        expires_at=r["expires_at"], created_at=r["created_at"], created_by=r["created_by"],
        revoked_at=r["revoked_at"], last_used_at=r["last_used_at"], rotated_from=r["rotated_from"])


class PgAdminStore:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 4):
        # Imported lazily so this module loads without the `postgres` extra.
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size,
                                   kwargs={"row_factory": dict_row}, open=True)

    def close(self) -> None:
        self.pool.close()

    def ensure_schema(self) -> None:
        import psycopg

        sql = resources.files("memory_mcp.admin").joinpath("schema.sql").read_text()
        try:
            with self.pool.connection() as conn:
                # Postgres checks CREATE privilege even for IF NOT EXISTS, so a restricted
                # role must skip the DDL when an admin already applied schema.sql.
                if self._schema_present(conn):
                    return
                conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
                try:
                    conn.execute(sql)
                    conn.commit()
                finally:
                    conn.rollback()
                    conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
                    conn.commit()
        except psycopg.errors.InsufficientPrivilege as e:
            raise RuntimeError(
                "cannot create schema mt_admin: grant CREATE on the database to this role "
                "or apply admin/schema.sql manually") from e

    @contextmanager
    def bootstrap_lock(self) -> Iterator[None]:
        """Session advisory lock held for the whole bootstrap: a second worker waits here and
        then finds bootstrap done. Released on exit, or by Postgres if the connection dies."""
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (_BOOTSTRAP_LOCK_ID,))
            conn.commit()  # session lock: do not sit idle in a transaction while bootstrapping
            try:
                yield
            finally:
                conn.rollback()
                conn.execute("SELECT pg_advisory_unlock(%s)", (_BOOTSTRAP_LOCK_ID,))
                conn.commit()

    @staticmethod
    def _schema_present(conn) -> bool:
        row = conn.execute(
            "SELECT bool_and(to_regclass('mt_admin.' || t) IS NOT NULL) AS ok "
            "FROM unnest(%s::text[]) AS t", (list(_TABLES),)).fetchone()
        return bool(row["ok"])

    def ping(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("SELECT 1")

    # tenants
    def list_tenants(self) -> list[Tenant]:
        with self.pool.connection() as conn:
            return [_tenant(r) for r in conn.execute("SELECT * FROM mt_admin.tenant ORDER BY id")]

    def get_tenant(self, id: str) -> Tenant | None:
        with self.pool.connection() as conn:
            r = conn.execute("SELECT * FROM mt_admin.tenant WHERE id=%s", (id,)).fetchone()
        return _tenant(r) if r else None

    def insert_tenant(self, t: Tenant, by: str) -> None:
        with self.pool.connection() as conn:
            cur = conn.execute(
                "INSERT INTO mt_admin.tenant (id, name, description, collection, status, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                (t.id, t.name, t.description, t.collection, t.status, by))
            if cur.rowcount == 0:
                raise ValueError("exists")

    def update_tenant(self, id: str, *, name: str | None = None, description: str | None = None,
                      status: str | None = None) -> Tenant | None:
        with self.pool.connection() as conn:
            r = conn.execute(
                "UPDATE mt_admin.tenant SET name=COALESCE(%s,name), "
                "description=COALESCE(%s,description), status=COALESCE(%s,status) "
                "WHERE id=%s RETURNING *", (name, description, status, id)).fetchone()
        return _tenant(r) if r else None

    def delete_tenant(self, id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM mt_admin.tenant WHERE id=%s", (id,))

    # keys
    def insert_key(self, k: ApiKey) -> None:
        with self.pool.connection() as conn:
            cur = conn.execute(
                f"INSERT INTO mt_admin.api_key ({_KEY_COLS}) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                (k.id, k.agent, k.secret_hash, list(k.tenants), k.access, list(k.denied_tools),
                 list(k.source_repo_patterns), k.expires_at, k.created_at, k.created_by,
                 k.revoked_at, k.last_used_at, k.rotated_from))
            if cur.rowcount == 0:
                raise ValueError("exists")

    def get_key(self, id: str) -> ApiKey | None:
        with self.pool.connection() as conn:
            r = conn.execute(f"SELECT {_KEY_COLS} FROM mt_admin.api_key WHERE id=%s", (id,)).fetchone()
        return _key(r) if r else None

    def list_keys(self) -> list[ApiKey]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_KEY_COLS} FROM mt_admin.api_key ORDER BY created_at, id").fetchall()
        return [_key(r) for r in rows]

    def set_key_expiry(self, id: str, at: datetime) -> None:
        with self.pool.connection() as conn:
            conn.execute("UPDATE mt_admin.api_key SET expires_at=%s WHERE id=%s", (at, id))

    def revoke_key(self, id: str, at: datetime) -> bool:
        """Revoke once; returns False if unknown or already revoked."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE mt_admin.api_key SET revoked_at=%s WHERE id=%s AND revoked_at IS NULL", (at, id))
            return cur.rowcount > 0

    def touch_key(self, id: str, at: datetime) -> None:
        with self.pool.connection() as conn:
            conn.execute("UPDATE mt_admin.api_key SET last_used_at=%s WHERE id=%s", (at, id))

    # role mappings
    def list_mappings(self) -> list[RoleMapping]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT group_id, tenant, role FROM mt_admin.role_mapping "
                "ORDER BY group_id, tenant, role").fetchall()
        return [RoleMapping(r["group_id"], r["tenant"], r["role"]) for r in rows]

    def replace_mappings(self, rows: list[RoleMapping], by: str) -> None:
        with self.pool.connection() as conn:  # one transaction: delete + insert
            conn.execute("DELETE FROM mt_admin.role_mapping")
            for m in dict.fromkeys(rows):
                conn.execute(
                    "INSERT INTO mt_admin.role_mapping (group_id, tenant, role, created_by) "
                    "VALUES (%s,%s,%s,%s)", (m.group_id, m.tenant, m.role, by))

    # settings
    def get_settings(self) -> dict[str, Any]:
        with self.pool.connection() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM mt_admin.setting")}

    def put_settings(self, values: dict[str, Any], by: str) -> None:
        from psycopg.types.json import Jsonb

        with self.pool.connection() as conn:
            for k, v in values.items():
                conn.execute(
                    "INSERT INTO mt_admin.setting (key, value, updated_by) VALUES (%s,%s,%s) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), "
                    "updated_by=EXCLUDED.updated_by", (k, Jsonb(v), by))

    # audit
    def add_audit(self, row: dict) -> None:
        from psycopg.types.json import Jsonb

        detail = row.get("detail")
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO mt_admin.audit_log (ts, principal_type, principal_id, agent, acting_user, "
                "tenant, action, target, outcome, detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (row.get("ts") or datetime.now(timezone.utc), row.get("principal_type"),
                 row.get("principal_id"), row.get("agent"), row.get("acting_user"), row.get("tenant"),
                 row["action"], row.get("target"), row.get("outcome"),
                 Jsonb(detail) if detail is not None else None))

    def query_audit(self, *, tenant: str | None = None, principal: str | None = None,
                    action: str | None = None, outcome: str | None = None,
                    since: datetime | None = None, until: datetime | None = None,
                    limit: int = 100, cursor: str | None = None) -> tuple[list[dict], str | None]:
        where, args = [], []
        for col, val in (("tenant", tenant), ("principal_id", principal),
                         ("action", action), ("outcome", outcome)):
            if val is not None:
                where.append(f"{col}=%s")
                args.append(val)
        if since is not None:
            where.append("ts>=%s")
            args.append(since)
        if until is not None:
            where.append("ts<=%s")
            args.append(until)
        if cursor is not None:
            cts, cid = decode_cursor(cursor)
            where.append("(ts, id) < (%s, %s)")
            args += [cts, cid]
        sql = (f"SELECT {', '.join(_AUDIT_COLS)} FROM mt_admin.audit_log "
               + (f"WHERE {' AND '.join(where)} " if where else "")
               + "ORDER BY ts DESC, id DESC LIMIT %s")
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (*args, limit + 1)).fetchall()
        page = rows[:limit]
        nxt = encode_cursor(page[-1]["ts"], page[-1]["id"]) if len(rows) > limit else None
        return page, nxt

    def purge_audit(self, before: datetime) -> int:
        with self.pool.connection() as conn:
            return conn.execute("DELETE FROM mt_admin.audit_log WHERE ts < %s", (before,)).rowcount

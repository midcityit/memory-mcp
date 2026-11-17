"""Admin REST API under /admin, used only by memory-ui.

Only UI principals may call it (keys and the legacy token get 403). Roles come from
the principal: an instance admin holds ``admin`` on ``"*"``; a tenant admin holds
``admin`` on that tenant. In legacy mode (no admin store) every endpoint answers 501
except ``GET /admin/me``, which tells the UI to hide the admin area.

Every write and every refused call is audited. Writes that change who can
authenticate (keys, mappings, settings, legacy token) drop the authenticator's caches.
Errors are ``AuthzError`` and reach the client as ``{"detail", "code"}``.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from memory_mcp.admin.audit import AuditSink
from memory_mcp.admin.auth import Authenticator, principal_from_request
from memory_mcp.admin.authz import READ_ACTIONS, WRITE_ACTIONS, role_on
from memory_mcp.admin.copy import copy_memories
from memory_mcp.admin.keys import generate_key
from memory_mcp.admin.models import ApiKey, AuthzError, Principal, RoleMapping
from memory_mcp.admin.store import AdminStore
from memory_mcp.admin.tenants import TenantService
from memory_mcp.kg.models import GRAPH_ID_RE
from memory_mcp.store import MemoryStore

log = logging.getLogger(__name__)

WILDCARD = "*"
ROLES = ("viewer", "editor", "admin")
TENANT_STATUSES = ("active", "disabled")
EXPIRY_DAYS = (30, 90, 365)
MAX_KNOWN_GROUPS = 1000
MAX_REPO_PATTERNS = 20
MAX_REPO_PATTERN_LEN = 200
ROTATE_GRACE = timedelta(hours=24)
KNOWN_TOOLS: frozenset[str] = READ_ACTIONS | WRITE_ACTIONS
DEFAULT_INSTANCE_NAME = "memory-twin"
DEFAULT_AUDIT_RETENTION_DAYS = 90
SETTINGS_KEYS = ("instance_name", "default_tenant", "stale_days", "allowed_types",
                 "audit_retention_days", "audit_reads", "legacy_token_enabled")

_AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

NOT_AVAILABLE = {"detail": "Admin API not available"}
STORE_UNAVAILABLE = "Admin store unavailable"


# ── request helpers ───────────────────────────────────────────────────────────
def _invalid(message: str, code: str = "invalid_request") -> AuthzError:
    return AuthzError(400, code, message)


async def _raw_body(request: Request) -> bytes:
    """The body, unparsed: handlers parse it only after the caller has been let in."""
    return await request.body()


def _parse(raw: bytes) -> Any:
    """The JSON body; 400 with a readable message if it is not JSON."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise _invalid("Request body must be JSON") from None


def _peek(raw: bytes, field: str) -> Any:
    """`field` of a JSON object body, or None; never raises (used only to label audit rows)."""
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        return None
    return body.get(field) if isinstance(body, dict) else None


def _object(body: Any, allowed: Iterable[str]) -> dict:
    if not isinstance(body, dict):
        raise _invalid("Request body must be a JSON object")
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise _invalid(f"Unknown field(s): {', '.join(unknown)}")
    return body


def _str(body: dict, field: str, *, min_len: int = 0, max_len: int, label: str) -> str:
    v = body.get(field)
    if not isinstance(v, str):
        raise _invalid(f"{label} must be text")
    v = v.strip()
    if not min_len <= len(v) <= max_len:
        raise _invalid(f"{label} must be {min_len}-{max_len} characters" if min_len
                       else f"{label} must be at most {max_len} characters")
    return v


def _str_list(v: Any, label: str) -> list[str]:
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise _invalid(f"{label} must be a list of text values")
    return v


def _int_in(v: Any, lo: int, hi: int, label: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise _invalid(f"{label} must be a whole number from {lo} to {hi}")
    return v


def _bool(v: Any, label: str) -> bool:
    if not isinstance(v, bool):
        raise _invalid(f"{label} must be true or false")
    return v


def _when(v: str | None, label: str) -> datetime | None:
    if v is None or v == "":
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise _invalid(f"{label} must be an ISO 8601 date or time") from None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _actor(p: Principal) -> str:
    """Human-readable name stored on records the caller creates (audit keeps the id)."""
    return p.user_name or p.id


def key_record(k: ApiKey) -> dict:
    """A key as the API shows it. Never includes the secret or its hash."""
    return {
        "id": k.id, "agent": k.agent, "tenants": list(k.tenants), "access": k.access,
        "denied_tools": list(k.denied_tools), "source_repo_patterns": list(k.source_repo_patterns),
        "expires_at": _iso(k.expires_at), "created_at": _iso(k.created_at), "created_by": k.created_by,
        "revoked_at": _iso(k.revoked_at), "last_used_at": _iso(k.last_used_at),
        "rotated_from": k.rotated_from,
    }


def _audit_row(r: dict) -> dict:
    ts = r.get("ts")
    return {
        "id": str(r.get("id")), "ts": ts.isoformat() if isinstance(ts, datetime) else ts,
        "principal_type": r.get("principal_type"), "principal_id": r.get("principal_id"),
        "agent": r.get("agent"), "acting_user": r.get("acting_user"), "tenant": r.get("tenant"),
        "action": r.get("action"), "target": r.get("target"), "outcome": r.get("outcome"),
        "detail": r.get("detail"),
    }


# ── roles ─────────────────────────────────────────────────────────────────────
def is_instance_admin(p: Principal) -> bool:
    return p.kind == "ui" and p.roles.get(WILDCARD) == "admin"


def administers(p: Principal, tenant: str) -> bool:
    return p.kind == "ui" and (is_instance_admin(p) or p.roles.get(tenant) == "admin")


def is_tenant_admin(p: Principal) -> bool:
    """Admin of at least one tenant (or instance admin)."""
    return p.kind == "ui" and any(role == "admin" for role in p.roles.values())


def _forbidden(code: str, message: str) -> AuthzError:
    return AuthzError(403, code, message)


# ── the API ───────────────────────────────────────────────────────────────────
class AdminApi:
    def __init__(self, *, store: AdminStore, tenants: TenantService, authenticator: Authenticator,
                 audit: AuditSink, memory_store: MemoryStore, admin_group_ids: Iterable[str],
                 stale_days: int):
        self._store = store
        self._tenants = tenants
        self._auth = authenticator
        self._audit = audit
        self._memory = memory_store
        self._admin_groups = {g.lower() for g in admin_group_ids}
        self._stale_days = stale_days

    # plumbing
    def guarded(self, p: Principal, action: str, fn: Callable[[], Any],
                tenants: Callable[[], Any] | None = None) -> Any:
        """Run `fn`. A refusal (AuthzError 4xx) is audited as denied, a failure as error;
        any other exception is taken as an admin store outage and becomes a 503.

        `tenants` names the tenants the call was about, for the audit rows: one row per
        existing tenant among them, or a single row without a tenant.
        """
        try:
            return fn()
        except AuthzError as e:
            self._record_refusal(p, action, e, tenants)
            raise
        except Exception as e:
            log.warning("admin call %s failed: %s", action, type(e).__name__)
            outage = AuthzError(503, "store_unavailable", STORE_UNAVAILABLE)
            self._record_refusal(p, action, outage, tenants)
            raise outage from e

    def _record_refusal(self, p: Principal, action: str, e: AuthzError,
                        tenants: Callable[[], Any] | None) -> None:
        outcome = "denied" if 400 <= e.status < 500 else "error"
        for tenant in self._audit_tenants(tenants):
            self._audit.record(p, action, tenant, None, outcome, {"rule": e.code})

    def _audit_tenants(self, tenants: Callable[[], Any] | None) -> list[str | None]:
        """Existing tenants among the caller-supplied names; never raises.

        Only well-formed ids that exist are written, so caller input cannot put
        arbitrary text in the audit log or multiply its rows.
        """
        try:
            named = tenants() if tenants is not None else []
            if not isinstance(named, (list, tuple)):
                named = [named]
            found = [t for t in dict.fromkeys(x for x in named if isinstance(x, str))
                     if GRAPH_ID_RE.match(t) and self._tenants.get(t) is not None]
        except Exception:  # noqa: BLE001 - labelling an audit row must not fail the request
            found = []
        return found or [None]

    def key_tenants(self, key_id: str) -> list[str]:
        k = self._store.get_key(key_id)
        return list(k.tenants) if k is not None else []

    @staticmethod
    def _require_ui(p: Principal) -> None:
        if p.kind != "ui" or not p.id:
            raise _forbidden("ui_only", "The admin API is only available to the web UI")

    def _require_instance_admin(self, p: Principal) -> None:
        self._require_ui(p)
        if not is_instance_admin(p):
            raise _forbidden("instance_admin_required", "Instance admin role required")

    def _require_tenant_admin(self, p: Principal) -> None:
        self._require_ui(p)
        if not is_tenant_admin(p):
            raise _forbidden("tenant_admin_required", "Tenant admin role required")

    def _settings(self) -> dict[str, Any]:
        s = self._store.get_settings()
        defaults = {
            "instance_name": DEFAULT_INSTANCE_NAME, "default_tenant": "", "stale_days": self._stale_days,
            "allowed_types": [], "audit_retention_days": DEFAULT_AUDIT_RETENTION_DAYS,
            "audit_reads": False, "legacy_token_enabled": True,
        }
        return {k: s.get(k, defaults[k]) for k in SETTINGS_KEYS}

    def _tenant_view(self, tenant_id: str) -> dict:
        view = next((t for t in self._tenants.stats() if t["id"] == tenant_id), None)
        if view is None:
            raise AuthzError(404, "unknown_tenant", f"Unknown tenant '{tenant_id}'")
        return view

    # me / groups
    def me(self, p: Principal) -> dict:
        self._require_ui(p)
        if is_instance_admin(p):
            held = self._tenants.all()
        else:
            held = [t for t in self._tenants.all() if role_on(p, t.id) is not None]
        settings = self._settings()
        return {
            "legacy_mode": False,
            "user": {"id": p.id, "name": p.user_name or ""},
            "roles": dict(p.roles),
            "instance_admin": is_instance_admin(p),
            "tenants": [{"id": t.id, "name": t.name, "status": t.status} for t in held],
            "settings": {"instance_name": settings["instance_name"],
                         "default_tenant": settings["default_tenant"]},
        }

    def known_groups(self, p: Principal, raw: bytes) -> dict:
        if p.kind != "ui":  # the sign-in principal (empty id) is allowed here
            raise _forbidden("ui_only", "The admin API is only available to the web UI")
        body = _object(_parse(raw), ("group_ids",))
        ids = _str_list(body.get("group_ids"), "group_ids")
        if len(ids) > MAX_KNOWN_GROUPS:
            raise _invalid(f"group_ids lists more than {MAX_KNOWN_GROUPS} groups", "too_many_groups")
        known = self._admin_groups | {m.group_id.lower() for m in self._store.list_mappings()}
        return {"group_ids": [g for g in dict.fromkeys(ids) if g.lower() in known]}

    # tenants
    def list_tenants(self, p: Principal) -> dict:
        self._require_instance_admin(p)
        return {"tenants": self._tenants.stats()}

    def create_tenant(self, p: Principal, raw: bytes) -> dict:
        self._require_instance_admin(p)
        body = _object(_parse(raw), ("id", "name", "description"))
        tid = body.get("id")
        if not isinstance(tid, str) or not GRAPH_ID_RE.match(tid):  # a tenant id is a graph id
            raise _invalid("Use 2–32 characters: a lowercase letter first, then lowercase letters, "
                           "digits or _.", "invalid_tenant_id")
        name = _str(body, "name", min_len=1, max_len=80, label="Name")
        description = (_str(body, "description", max_len=500, label="Description")
                       if body.get("description") is not None else "")
        exists = AuthzError(409, "tenant_exists", f"Tenant '{tid}' already exists")
        if self._store.get_tenant(tid) is not None:
            raise exists
        try:
            self._tenants.create(tid, name, description, by=_actor(p))
        except ValueError:  # id already validated: only a concurrent create gets here
            raise exists from None
        self._audit.record(p, "admin_create_tenant", tid, tid, "allowed", {"name": name})
        return self._tenant_view(tid)

    def update_tenant(self, p: Principal, tenant_id: str, raw: bytes) -> dict:
        self._require_instance_admin(p)
        body = _object(_parse(raw), ("name", "description", "status"))
        if self._store.get_tenant(tenant_id) is None:
            raise AuthzError(404, "unknown_tenant", f"Unknown tenant '{tenant_id}'")
        changes: dict[str, str] = {}
        if body.get("name") is not None:
            changes["name"] = _str(body, "name", min_len=1, max_len=80, label="Name")
        if body.get("description") is not None:
            changes["description"] = _str(body, "description", max_len=500, label="Description")
        status = body.get("status")
        if status is not None and status not in TENANT_STATUSES:
            raise _invalid("Status must be active or disabled")
        if not changes and status is None:
            raise _invalid("Nothing to update")
        if status == "disabled" and self._settings()["default_tenant"] == tenant_id:
            raise _invalid("This is the default tenant. Choose another default tenant in Settings first.",
                           "default_tenant")
        if changes:
            self._tenants.rename(tenant_id, **changes)
        if status is not None:
            self._tenants.set_status(tenant_id, status)
            changes["status"] = status
        self._audit.record(p, "admin_update_tenant", tenant_id, tenant_id, "allowed",
                           {"fields": sorted(changes)})
        return self._tenant_view(tenant_id)

    # keys
    def _visible(self, p: Principal, k: ApiKey) -> bool:
        return bool(k.tenants) and all(administers(p, t) for t in k.tenants)

    def _require_active(self, tenant_ids: Iterable[str]) -> None:
        for t in tenant_ids:
            tenant = self._tenants.get(t)
            if tenant is None:
                raise _invalid(f"Unknown tenant '{t}'", "unknown_tenant")
            if tenant.status != "active":
                raise _invalid(f"Tenant '{t}' is disabled", "tenant_disabled")

    def _key_for_change(self, p: Principal, key_id: str) -> ApiKey:
        k = self._store.get_key(key_id)
        if k is None:
            raise AuthzError(404, "key_not_found", "Key not found")
        if not self._visible(p, k):
            raise _forbidden("key_forbidden", "You do not administer every tenant on this key")
        return k

    def _issue(self, p: Principal, **scope: Any) -> tuple[str, ApiKey]:
        for _ in range(5):  # 8-character ids: a collision is unlikely but not impossible
            full, key_id, secret_hash = generate_key()
            key = ApiKey(id=key_id, secret_hash=secret_hash, created_at=_now(), created_by=_actor(p),
                         revoked_at=None, last_used_at=None, **scope)
            try:
                self._store.insert_key(key)
            except ValueError:
                continue
            return full, key
        raise AuthzError(503, "key_id_collision", "Could not allocate a key id; try again")

    def list_keys(self, p: Principal) -> dict:
        self._require_tenant_admin(p)
        settings = self._store.get_settings()
        return {
            "keys": [key_record(k) for k in self._store.list_keys() if self._visible(p, k)],
            "legacy": {"enabled": settings.get("legacy_token_enabled", True) is True,
                       "last_used_at": settings.get("legacy_last_used_at")},
        }

    def create_key(self, p: Principal, raw: bytes) -> dict:
        self._require_tenant_admin(p)
        body = _object(_parse(raw), ("agent", "tenants", "access", "denied_tools", "source_repo_patterns",
                              "expires_in_days"))
        agent = body.get("agent")
        if not isinstance(agent, str) or not _AGENT_RE.match(agent):
            raise _invalid("Agent must be 2-63 lowercase letters, digits, . _ or -, "
                           "starting with a letter or digit", "invalid_agent")
        tenants = list(dict.fromkeys(_str_list(body.get("tenants"), "tenants")))
        if not tenants:
            raise _invalid("Select at least one tenant")
        for t in tenants:  # permission before existence: no existence signal to tenant admins
            if not administers(p, t):
                raise _forbidden("tenant_forbidden", f"You do not administer tenant '{t}'")
        self._require_active(tenants)
        access = body.get("access")
        if access not in ("read", "write"):
            raise _invalid("Access must be read or write")
        denied = list(dict.fromkeys(_str_list(body.get("denied_tools", []), "denied_tools")))
        unknown_tools = [t for t in denied if t not in KNOWN_TOOLS]
        if unknown_tools:
            raise _invalid(f"Unknown tool(s): {', '.join(unknown_tools)}", "unknown_tool")
        patterns = _str_list(body.get("source_repo_patterns", []), "source_repo_patterns")
        if len(patterns) > MAX_REPO_PATTERNS:
            raise _invalid(f"At most {MAX_REPO_PATTERNS} source repo patterns")
        if any(not x or len(x) > MAX_REPO_PATTERN_LEN for x in patterns):
            raise _invalid(f"Each source repo pattern must be 1-{MAX_REPO_PATTERN_LEN} characters")
        days = body.get("expires_in_days")
        if not isinstance(days, int) or isinstance(days, bool) or days not in EXPIRY_DAYS:
            raise _invalid("Expiry must be 30, 90 or 365 days")

        full, key = self._issue(p, agent=agent, tenants=tuple(tenants), access=access,
                                denied_tools=tuple(denied), source_repo_patterns=tuple(patterns),
                                expires_at=_now() + timedelta(days=days), rotated_from=None)
        self._auth.invalidate()
        for t in key.tenants:
            self._audit.record(p, "admin_create_key", t, key.id, "allowed",
                               {"agent": agent, "access": access, "expires_at": _iso(key.expires_at)})
        return {"key": full, "record": key_record(key)}

    def rotate_key(self, p: Principal, key_id: str) -> dict:
        self._require_tenant_admin(p)
        old = self._key_for_change(p, key_id)
        if old.revoked_at is not None:
            raise _invalid("A revoked key cannot be rotated", "key_revoked")
        self._require_active(old.tenants)
        now = _now()
        # Scope is copied unchanged. The new key gets the old key's lifetime, or the longest
        # offered expiry if the old key never expired: rotation never issues a key without one.
        lifetime = (old.expires_at - old.created_at if old.expires_at is not None
                    else timedelta(days=max(EXPIRY_DAYS)))
        full, key = self._issue(p, agent=old.agent, tenants=old.tenants, access=old.access,
                                denied_tools=old.denied_tools, source_repo_patterns=old.source_repo_patterns,
                                expires_at=now + lifetime, rotated_from=old.id)
        grace_end = now + ROTATE_GRACE
        if old.expires_at is None or old.expires_at > grace_end:
            self._store.set_key_expiry(old.id, grace_end)
        self._auth.invalidate()
        for t in key.tenants:
            self._audit.record(p, "admin_rotate_key", t, key.id, "allowed", {"rotated_from": old.id})
        return {"key": full, "record": key_record(key)}

    def revoke_key(self, p: Principal, key_id: str) -> None:
        self._require_tenant_admin(p)
        k = self._key_for_change(p, key_id)
        if not self._store.revoke_key(k.id, _now()):
            raise AuthzError(404, "key_not_found", "Key not found")
        self._auth.invalidate()
        for t in k.tenants:
            self._audit.record(p, "admin_revoke_key", t, k.id, "allowed")

    def retire_legacy(self, p: Principal) -> None:
        self._require_instance_admin(p)
        self._store.put_settings({"legacy_token_enabled": False}, by=_actor(p))
        self._auth.invalidate()
        self._audit.record(p, "admin_retire_legacy", None, None, "allowed")

    # access mappings
    def get_access(self, p: Principal) -> dict:
        self._require_instance_admin(p)
        labels = self._store.get_settings().get("group_labels") or {}
        return {"mappings": [
            {"group_id": m.group_id, "label": labels.get(m.group_id, "") if isinstance(labels, dict) else "",
             "tenant": m.tenant, "role": m.role}
            for m in self._store.list_mappings()
        ]}

    def put_access(self, p: Principal, raw: bytes) -> dict:
        self._require_instance_admin(p)
        body = _object(_parse(raw), ("mappings",))
        rows = body.get("mappings")
        if not isinstance(rows, list):
            raise _invalid("mappings must be a list")
        mappings: list[RoleMapping] = []
        labels: dict[str, str] = {}
        for i, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise _invalid(f"Row {i}: must be an object")
            row = _object(row, ("group_id", "label", "tenant", "role"))
            gid = row.get("group_id")
            if not isinstance(gid, str) or not _GUID_RE.match(gid.strip()):
                raise _invalid(f"Row {i}: group ID must be a GUID", "invalid_group_id")
            gid = gid.strip().lower()
            label = _str(row, "label", max_len=200, label=f"Row {i}: label") if row.get("label") is not None else ""
            tenant = row.get("tenant")
            if not isinstance(tenant, str) or (tenant != WILDCARD and self._tenants.get(tenant) is None):
                raise _invalid(f"Row {i}: unknown tenant '{tenant}'", "unknown_tenant")
            role = row.get("role")
            if role not in ROLES:
                raise _invalid(f"Row {i}: role must be viewer, editor or admin")
            m = RoleMapping(group_id=gid, tenant=tenant, role=role)
            if m in mappings:
                raise _invalid(f"Row {i}: duplicate of an earlier row", "duplicate_mapping")
            mappings.append(m)
            if label and gid not in labels:
                labels[gid] = label
        self._store.replace_mappings(mappings, by=_actor(p))
        self._store.put_settings({"group_labels": labels}, by=_actor(p))
        self._auth.invalidate()
        self._audit.record(p, "admin_put_access", None, None, "allowed", {"mappings": len(mappings)})
        return self.get_access(p)

    # settings
    def get_settings(self, p: Principal) -> dict:
        self._require_instance_admin(p)
        return self._settings()

    def put_settings(self, p: Principal, raw: bytes) -> dict:
        self._require_instance_admin(p)
        body = _object(_parse(raw), SETTINGS_KEYS)
        values: dict[str, Any] = {}
        if "instance_name" in body:
            values["instance_name"] = _str(body, "instance_name", min_len=1, max_len=60, label="Instance name")
        if "default_tenant" in body:
            t = body["default_tenant"]
            tenant = self._tenants.get(t) if isinstance(t, str) else None
            if tenant is None or tenant.status != "active":
                raise _invalid("Default tenant must be an existing, active tenant", "invalid_default_tenant")
            values["default_tenant"] = t
        for k, label in (("stale_days", "Stale days"), ("audit_retention_days", "Audit retention days")):
            if k in body:
                values[k] = _int_in(body[k], 1, 3650, label)
        if "allowed_types" in body:
            types = _str_list(body["allowed_types"], "Allowed types")
            if any(not t.strip() for t in types):
                raise _invalid("Allowed types must not be empty")
            values["allowed_types"] = list(dict.fromkeys(t.strip() for t in types))
        for k, label in (("audit_reads", "Audit reads"), ("legacy_token_enabled", "Legacy token enabled")):
            if k in body:
                values[k] = _bool(body[k], label)
        if values:
            self._store.put_settings(values, by=_actor(p))
            self._auth.invalidate()
            self._audit.record(p, "admin_put_settings", None, None, "allowed", {"keys": sorted(values)})
        return self._settings()

    # audit
    def audit_rows(self, p: Principal, *, tenant: str | None, principal: str | None, action: str | None,
                   outcome: str | None, since: str | None, until: str | None, cursor: str | None) -> dict:
        self._require_tenant_admin(p)
        if not is_instance_admin(p):
            if not tenant:
                raise _forbidden("tenant_required", "Choose a tenant you administer")
            if not administers(p, tenant):
                raise _forbidden("tenant_forbidden", f"You do not administer tenant '{tenant}'")
        try:
            rows, nxt = self._store.query_audit(
                tenant=tenant or None, principal=principal or None, action=action or None,
                outcome=outcome or None, since=_when(since, "since"), until=_when(until, "until"),
                cursor=cursor or None)
        except ValueError:
            raise _invalid("Invalid cursor", "invalid_cursor") from None
        return {"rows": [_audit_row(r) for r in rows], "next_cursor": nxt}

    # copy
    def copy(self, p: Principal, raw: bytes) -> dict:
        self._require_ui(p)
        body = _object(_parse(raw), ("from", "to", "ids", "dry_run"))
        from_, to = body.get("from"), body.get("to")
        if not isinstance(from_, str) or not isinstance(to, str) or not from_ or not to:
            raise _invalid("from and to must name tenants")
        ids = _str_list(body.get("ids"), "ids")
        dry_run = _bool(body.get("dry_run", False), "dry_run")
        if not administers(p, to):
            raise _forbidden("tenant_admin_required", f"Tenant admin role required on '{to}'")
        if role_on(p, from_) is None:
            raise _forbidden("tenant_forbidden", f"No access to tenant '{from_}'")

        def audited(dest_id: str, source_id: str) -> None:
            self._audit.record(p, "copy_memory", to, dest_id, "allowed", {"from": from_, "source_id": source_id})

        return copy_memories(self._memory, self._tenants, from_, to, ids, dry_run, by=_actor(p),
                             on_write=audited)

    def tools(self, p: Principal) -> dict:
        self._require_tenant_admin(p)
        return {"tools": sorted(KNOWN_TOOLS)}


# ── routers ───────────────────────────────────────────────────────────────────
def _legacy_router() -> APIRouter:
    router = APIRouter(prefix="/admin")

    @router.get("/me")
    def me(p: Principal = Depends(principal_from_request)):
        return {"legacy_mode": True, "user": {"id": p.id, "name": p.user_name or ""}, "roles": {},
                "instance_admin": False, "tenants": [],
                "settings": {"instance_name": DEFAULT_INSTANCE_NAME, "default_tenant": ""}}

    @router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                      include_in_schema=False)
    def not_available(path: str):
        return JSONResponse(NOT_AVAILABLE, status_code=501)

    return router


def build_admin_router(*, store: AdminStore | None, tenants: TenantService | None,
                       authenticator: Authenticator, audit: AuditSink, memory_store: MemoryStore,
                       admin_group_ids: Iterable[str], stale_days: int) -> APIRouter:
    """The /admin router; the 501-only legacy router when there is no admin store."""
    if store is None or tenants is None:
        return _legacy_router()
    api = AdminApi(store=store, tenants=tenants, authenticator=authenticator, audit=audit,
                   memory_store=memory_store, admin_group_ids=admin_group_ids, stale_days=stale_days)
    router = APIRouter(prefix="/admin")
    who = Depends(principal_from_request)
    body = Depends(_raw_body)

    @router.get("/me")
    def me(p: Principal = who):
        return api.guarded(p, "admin_me", lambda: api.me(p))

    @router.post("/groups/known")
    def groups_known(p: Principal = who, raw: bytes = body):
        return api.guarded(p, "admin_groups_known", lambda: api.known_groups(p, raw))

    @router.get("/tenants")
    def list_tenants(p: Principal = who):
        return api.guarded(p, "admin_list_tenants", lambda: api.list_tenants(p))

    @router.post("/tenants", status_code=201)
    def create_tenant(p: Principal = who, raw: bytes = body):
        return api.guarded(p, "admin_create_tenant", lambda: api.create_tenant(p, raw),
                           lambda: _peek(raw, "id"))

    @router.patch("/tenants/{tenant_id}")
    def update_tenant(tenant_id: str, p: Principal = who, raw: bytes = body):
        return api.guarded(p, "admin_update_tenant", lambda: api.update_tenant(p, tenant_id, raw),
                           lambda: tenant_id)

    @router.get("/keys")
    def list_keys(p: Principal = who):
        return api.guarded(p, "admin_list_keys", lambda: api.list_keys(p))

    @router.post("/keys", status_code=201)
    def create_key(p: Principal = who, raw: bytes = body):
        return api.guarded(p, "admin_create_key", lambda: api.create_key(p, raw),
                           lambda: _peek(raw, "tenants"))

    @router.post("/keys/{key_id}/rotate")
    def rotate_key(key_id: str, p: Principal = who):
        return api.guarded(p, "admin_rotate_key", lambda: api.rotate_key(p, key_id),
                           lambda: api.key_tenants(key_id))

    @router.post("/keys/{key_id}/revoke", status_code=204)
    def revoke_key(key_id: str, p: Principal = who):
        api.guarded(p, "admin_revoke_key", lambda: api.revoke_key(p, key_id),
                    lambda: api.key_tenants(key_id))
        return Response(status_code=204)

    @router.get("/access")
    def get_access(p: Principal = who):
        return api.guarded(p, "admin_get_access", lambda: api.get_access(p))

    @router.put("/access")
    def put_access(p: Principal = who, raw: bytes = body):
        return api.guarded(p, "admin_put_access", lambda: api.put_access(p, raw))

    @router.get("/audit")
    def audit_rows(p: Principal = who, tenant: str | None = None, principal: str | None = None,
                   action: str | None = None, outcome: str | None = None, since: str | None = None,
                   until: str | None = None, cursor: str | None = None):
        return api.guarded(p, "admin_audit", lambda: api.audit_rows(
            p, tenant=tenant, principal=principal, action=action, outcome=outcome,
            since=since, until=until, cursor=cursor), lambda: tenant)

    @router.get("/settings")
    def get_settings(p: Principal = who):
        return api.guarded(p, "admin_get_settings", lambda: api.get_settings(p))

    @router.put("/settings")
    def put_settings(p: Principal = who, raw: bytes = body):
        return api.guarded(p, "admin_put_settings", lambda: api.put_settings(p, raw))

    @router.post("/copy")
    def copy(p: Principal = who, raw: bytes = body):
        return api.guarded(p, "copy_memory", lambda: api.copy(p, raw), lambda: _peek(raw, "to"))

    @router.post("/legacy-token/retire", status_code=204)
    def retire_legacy(p: Principal = who):
        api.guarded(p, "admin_retire_legacy", lambda: api.retire_legacy(p))
        return Response(status_code=204)

    @router.get("/tools")
    def tools(p: Principal = who):
        return api.guarded(p, "admin_tools", lambda: api.tools(p))

    return router

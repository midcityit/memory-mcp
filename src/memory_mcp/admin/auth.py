"""Authentication: turn a bearer token into a Principal (who is calling).

No authorization decisions are made here. Three kinds of caller exist:

- a scoped API key (``mt_<id>_<secret>``), looked up in the admin store;
- the legacy shared ``API_TOKEN``;
- the web UI (``UI_TOKEN``) acting for a signed-in human described by the
  ``X-MT-User`` and ``X-MT-User-Name`` headers and the optional ``X-MT-Groups``
  header (absent means no groups).

With no admin store (legacy mode) only ``API_TOKEN`` is accepted.
"""
from __future__ import annotations

import hmac
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

import anyio
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from memory_mcp.admin.keys import parse_key, verify_secret
from memory_mcp.admin.models import ApiKey, AuthzError, Principal, RoleMapping
from memory_mcp.admin.store import AdminStore
from memory_mcp.config import cfg_str

log = logging.getLogger(__name__)

CACHE_TTL_S = 30.0
TOUCH_INTERVAL_S = 60.0
MAX_GROUPS = 50
MAX_USER_ID_LEN = 128
MAX_USER_NAME_LEN = 256
KEY_CACHE_MAX = 4096
NEGATIVE_KEY_CACHE_MAX = 1024
GROUPS_KNOWN_PATH = "/admin/groups/known"
_GROUP_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3}
_UNAUTHORIZED = "Unauthorized"


def _group_set(value: Any) -> frozenset[str]:
    if not isinstance(value, (tuple, list, set, frozenset)):
        return frozenset()
    return frozenset(g.lower() for g in value if isinstance(g, str) and g)


def _token_matches(bearer: str, expected: str | None) -> bool:
    return expected is not None and hmac.compare_digest(bearer.encode(), expected.encode())


def _unauthorized() -> AuthzError:
    return AuthzError(401, "unauthorized", _UNAUTHORIZED)


def _bad_ui_headers(message: str) -> AuthzError:
    return AuthzError(400, "bad_ui_headers", message)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class _TtlLru:
    """Bounded LRU whose entries expire CACHE_TTL_S after they were fetched. Not thread-safe."""

    def __init__(self, cap: int):
        self._cap = cap
        self._d: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._d)

    def get(self, k: str, now: float) -> tuple[bool, Any]:
        hit = self._d.get(k)
        if hit is None:
            return False, None
        if now - hit[0] >= CACHE_TTL_S:
            del self._d[k]
            return False, None
        self._d.move_to_end(k)
        return True, hit[1]

    def put(self, k: str, fetched_at: float, v: Any) -> None:
        self._d[k] = (fetched_at, v)
        self._d.move_to_end(k)
        while len(self._d) > self._cap:
            self._d.popitem(last=False)

    def clear(self) -> None:
        self._d.clear()


class Authenticator:
    def __init__(self, cfg, store: AdminStore | None, clock: Callable[[], float] = time.time):
        self._api_token = cfg_str(cfg, "api_token")
        self._ui_token = cfg_str(cfg, "ui_token")
        self._admin_groups = _group_set(getattr(cfg, "admin_group_ids", ()))
        if self._api_token is not None and _token_matches(self._api_token, self._ui_token):
            raise ValueError("API_TOKEN and UI_TOKEN must differ")
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()
        # Bumped by invalidate(); a fetch that straddles an invalidate() is not cached.
        self._generation = 0
        self._keys = _TtlLru(KEY_CACHE_MAX)
        # Unknown ids are cached separately so unauthenticated callers sending random
        # ids cannot evict real keys or grow memory beyond this cap.
        self._unknown_keys = _TtlLru(NEGATIVE_KEY_CACHE_MAX)
        self._settings: tuple[float, dict[str, Any]] | None = None
        self._mappings: tuple[float, list[RoleMapping]] | None = None
        self._key_touched: dict[str, float] = {}
        self._legacy_touched: float | None = None

    def invalidate(self) -> None:
        """Drop cached keys, settings and role mappings (call after admin writes)."""
        with self._lock:
            self._generation += 1
            self._keys.clear()
            self._unknown_keys.clear()
            self._settings = None
            self._mappings = None

    def settings(self) -> dict[str, Any]:
        """Admin settings, cached for CACHE_TTL_S and dropped by invalidate().

        Raises AuthzError(503, "store_unavailable") if the store cannot be read.
        """
        if self._store is None:
            return {}
        return self._settings_cached()

    def resolve(self, headers: Mapping[str, str], *, path: str | None = None) -> Principal:
        h = {k.lower(): v for k, v in headers.items()}
        auth = h.get("authorization", "")
        scheme, _, bearer = auth.partition(" ")
        bearer = bearer.strip()
        if scheme.lower() != "bearer" or not bearer:
            raise _unauthorized()

        if self._store is None:
            if _token_matches(bearer, self._api_token):
                return self._legacy_principal()
            raise _unauthorized()

        if _token_matches(bearer, self._api_token):
            # Fail closed: only an absent setting or exactly True keeps the legacy token on.
            settings = self._settings_cached()
            if settings.get("legacy_token_enabled", True) is not True:
                raise _unauthorized()
            self._record_legacy_use()
            return self._legacy_principal()
        if _token_matches(bearer, self._ui_token):
            return self._ui_principal(h, path)
        parsed = parse_key(bearer)
        if parsed is not None:
            return self._key_principal(*parsed)
        raise _unauthorized()

    # ── principals ────────────────────────────────────────────────────────────
    @staticmethod
    def _legacy_principal() -> Principal:
        return Principal(kind="legacy", id="legacy", agent=None, roles={}, all_tenants=True)

    def _key_principal(self, key_id: str, secret: str) -> Principal:
        key = self._key_cached(key_id)
        now = self._now()
        if (key is None or not verify_secret(secret, key.secret_hash) or key.revoked_at is not None
                or (key.expires_at is not None and _as_utc(key.expires_at) <= now)):
            raise _unauthorized()
        self._touch_key(key.id, now)
        return Principal(
            kind="key", id=key.id, agent=key.agent,
            roles={t: ("editor" if key.access == "write" else "viewer") for t in key.tenants},
            access=key.access,
            denied_tools=frozenset(key.denied_tools),
            source_repo_patterns=key.source_repo_patterns,
        )

    def _ui_principal(self, h: Mapping[str, str], path: str | None) -> Principal:
        user = h.get("x-mt-user")
        if user is None and path == GROUPS_KNOWN_PATH:
            return Principal(kind="ui", id="", agent=None, roles={})
        name = h.get("x-mt-user-name")
        # An absent X-MT-Groups means the user is in no groups (some sign-ins send none).
        groups_raw = h.get("x-mt-groups", "")
        if user is None or name is None:
            raise _bad_ui_headers("X-MT-User and X-MT-User-Name headers are required")
        if not user or len(user) > MAX_USER_ID_LEN:
            raise _bad_ui_headers(f"X-MT-User must be 1-{MAX_USER_ID_LEN} characters")
        groups = [g.strip() for g in groups_raw.split(",") if g.strip()]
        if len(groups) > MAX_GROUPS:
            raise _bad_ui_headers(f"X-MT-Groups lists more than {MAX_GROUPS} groups")
        if any(not _GROUP_RE.match(g) for g in groups):
            raise _bad_ui_headers("X-MT-Groups contains a malformed group id")
        user_name = unquote(name)
        if len(user_name) > MAX_USER_NAME_LEN:
            raise _bad_ui_headers(f"X-MT-User-Name must be at most {MAX_USER_NAME_LEN} characters")
        return Principal(kind="ui", id=user, agent=None, roles=self._roles_for(groups),
                         user_name=user_name)

    def _roles_for(self, groups: list[str]) -> dict[str, str]:
        mine = {g.lower() for g in groups}
        # Resolves even while the store is down; admin endpoints need the store and fail there.
        if mine & self._admin_groups:
            return {"*": "admin"}
        roles: dict[str, str] = {}
        for m in self._mappings_cached():
            rank = _ROLE_RANK.get(m.role)
            if rank is None or m.group_id.lower() not in mine:
                continue
            if rank > _ROLE_RANK.get(roles.get(m.tenant, ""), 0):
                roles[m.tenant] = m.role
        return roles

    # ── cached store reads (store errors -> 503) ──────────────────────────────
    def _store_call(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as e:
            log.warning("admin store unavailable during authentication: %s", e)
            raise AuthzError(503, "store_unavailable", "Admin store unavailable") from e

    def _fresh(self, fetched_at: float) -> bool:
        return self._clock() - fetched_at < CACHE_TTL_S

    def _begin_fetch(self) -> tuple[int, float]:
        """Generation and timestamp taken before a store read, so the entry never outlives its TTL."""
        with self._lock:
            return self._generation, self._clock()

    def _key_cached(self, key_id: str) -> ApiKey | None:
        now = self._clock()
        with self._lock:
            found, key = self._keys.get(key_id, now)
            if found:
                return key
            if self._unknown_keys.get(key_id, now)[0]:
                return None
        gen, fetched_at = self._begin_fetch()
        key = self._store_call(self._store.get_key, key_id)
        with self._lock:
            if self._generation == gen:
                if key is None:
                    self._unknown_keys.put(key_id, fetched_at, True)
                else:
                    self._keys.put(key_id, fetched_at, key)
        return key

    def _settings_cached(self) -> dict[str, Any]:
        with self._lock:
            hit = self._settings
        if hit is not None and self._fresh(hit[0]):
            return hit[1]
        gen, fetched_at = self._begin_fetch()
        settings = self._store_call(self._store.get_settings)
        with self._lock:
            if self._generation == gen:
                self._settings = (fetched_at, settings)
        return settings

    def _mappings_cached(self) -> list[RoleMapping]:
        with self._lock:
            hit = self._mappings
        if hit is not None and self._fresh(hit[0]):
            return hit[1]
        gen, fetched_at = self._begin_fetch()
        mappings = self._store_call(self._store.list_mappings)
        with self._lock:
            if self._generation == gen:
                self._mappings = (fetched_at, mappings)
        return mappings

    # ── last-use bookkeeping (best effort, throttled) ─────────────────────────
    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), timezone.utc)

    def _touch_key(self, key_id: str, now: datetime) -> None:
        t = self._clock()
        with self._lock:
            last = self._key_touched.get(key_id)
            if last is not None and t - last < TOUCH_INTERVAL_S:
                return
            self._key_touched[key_id] = t
        try:
            self._store.touch_key(key_id, now)
        except Exception as e:
            log.warning("could not record last use of key %s: %s", key_id, e)

    def _record_legacy_use(self) -> None:
        t = self._clock()
        with self._lock:
            if self._legacy_touched is not None and t - self._legacy_touched < TOUCH_INTERVAL_S:
                return
            self._legacy_touched = t
        try:
            self._store.put_settings({"legacy_last_used_at": self._now().isoformat()}, by="system")
        except Exception as e:
            log.warning("could not record legacy token use: %s", e)


class PrincipalMiddleware:
    """Pure ASGI middleware: resolve the caller on every HTTP path except /health."""

    def __init__(self, app, authenticator: Authenticator):
        self.app = app
        self.authenticator = authenticator

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        path = scope.get("path")
        try:
            headers = _auth_headers(scope)
            # The admin store is synchronous (may hit Postgres): keep it off the event loop.
            principal = await anyio.to_thread.run_sync(
                lambda: self.authenticator.resolve(headers, path=path))
        except AuthzError as e:
            extra = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
            await JSONResponse({"detail": e.message}, status_code=e.status, headers=extra)(
                scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


def _auth_headers(scope) -> dict[str, str]:
    """Header map from the raw ASGI list; Authorization and X-MT-* must not repeat."""
    out: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        if name in out and (name == "authorization" or name.startswith("x-mt-")):
            raise AuthzError(400, "duplicate_header", "Duplicate header")
        out[name] = raw_value.decode("latin-1")
    return out


def principal_from_request(request: Request) -> Principal:
    """FastAPI dependency: the Principal resolved by PrincipalMiddleware."""
    principal = request.scope.get("state", {}).get("principal")
    if not isinstance(principal, Principal):
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED,
                            headers={"WWW-Authenticate": "Bearer"})
    return principal


def principal_from_ctx(ctx) -> Principal:
    """MCP tool Context -> the Starlette request the transport attached -> its Principal.

    The streamable HTTP transport hands each message's Starlette ``Request`` to the
    tool via ``ctx.request_context.request``; its scope is the one PrincipalMiddleware
    wrote the principal into, so this is per-request with no shared mutable state.
    """
    request = getattr(ctx.request_context, "request", None)
    scope = getattr(request, "scope", None) or {}
    principal = scope.get("state", {}).get("principal")
    if not isinstance(principal, Principal):
        raise AuthzError(401, "unauthorized", _UNAUTHORIZED)
    return principal

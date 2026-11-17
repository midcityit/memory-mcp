from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from memory_mcp.admin.auth import Authenticator, PrincipalMiddleware, principal_from_request
from memory_mcp.admin.keys import generate_key
from memory_mcp.admin.models import ApiKey, AuthzError, Principal, RoleMapping
from memory_mcp.admin.store import InMemoryAdminStore

API = "api-token-value"
UI = "ui-token-value"
G1 = "11111111-1111-1111-1111-111111111111"
G2 = "22222222-2222-2222-2222-222222222222"
ADMIN_G = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t: float = T0.timestamp()):
        self.t = t

    def __call__(self) -> float:
        return self.t


def cfg(**over):
    base = dict(api_token=API, ui_token=UI, admin_group_ids=(ADMIN_G,))
    base.update(over)
    return SimpleNamespace(**base)


def add_key(store, *, access="write", tenants=("alpha",), expires_at=None, revoked_at=None, agent="bot"):
    full, key_id, secret_hash = generate_key()
    store.insert_key(ApiKey(
        id=key_id, agent=agent, secret_hash=secret_hash, tenants=tenants, access=access,
        denied_tools=("delete_memory",), source_repo_patterns=("alpha/*",), expires_at=expires_at,
        created_at=T0, created_by="admin", revoked_at=revoked_at, last_used_at=None, rotated_from=None,
    ))
    return full, key_id


def bearer(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def ui_headers(user="u1", name="Ada", groups=G1):
    return bearer(UI, **{"X-MT-User": user, "X-MT-User-Name": name, "X-MT-Groups": groups})


@pytest.fixture
def store():
    return InMemoryAdminStore()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def auth(store, clock):
    return Authenticator(cfg(), store, clock=clock)


def status_of(fn):
    with pytest.raises(AuthzError) as ei:
        fn()
    return ei.value.status, ei.value.message


# ── bearer kinds ────────────────────────────────────────────────────────────

def test_key_resolves_to_key_principal(auth, store):
    full, key_id = add_key(store, tenants=("alpha", "beta"))
    p = auth.resolve(bearer(full))
    assert p == Principal(kind="key", id=key_id, agent="bot", roles={"alpha": "editor", "beta": "editor"},
                          access="write", denied_tools=frozenset({"delete_memory"}),
                          source_repo_patterns=("alpha/*",))


def test_read_key_maps_to_viewer(auth, store):
    full, _ = add_key(store, access="read")
    p = auth.resolve(bearer(full))
    assert p.roles == {"alpha": "viewer"} and p.access == "read"


def test_legacy_token_resolves_to_legacy_principal(auth):
    assert auth.resolve(bearer(API)) == Principal(kind="legacy", id="legacy", agent=None, roles={},
                                                  all_tenants=True)


def test_ui_token_resolves_to_ui_principal(auth, store):
    store.replace_mappings([RoleMapping(G1, "alpha", "editor")], by="t")
    p = auth.resolve(ui_headers(name=quote("Zoë Ü")))
    assert p == Principal(kind="ui", id="u1", agent=None, roles={"alpha": "editor"}, user_name="Zoë Ü")


def test_header_names_are_case_insensitive(auth):
    assert auth.resolve({"authorization": f"Bearer {API}"}).kind == "legacy"


# ── 401s ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "},
                                     bearer("nope")])
def test_missing_or_wrong_bearer_is_401(auth, headers):
    assert status_of(lambda: auth.resolve(headers)) == (401, "Unauthorized")


def test_revoked_expired_unknown_and_bad_secret_are_identical(auth, store, clock):
    revoked, _ = add_key(store, revoked_at=T0)
    expired, _ = add_key(store, expires_at=T0 - timedelta(seconds=1))
    good, key_id = add_key(store)
    wrong_secret = f"mt_{key_id}_notthesecret"
    unknown = "mt_ZZZZZZZZ_whatever"
    results = {status_of(lambda t=t: auth.resolve(bearer(t))) for t in (revoked, expired, wrong_secret, unknown)}
    assert results == {(401, "Unauthorized")}


async def test_http_bodies_identical_for_revoked_expired_unknown(store, clock):
    revoked, _ = add_key(store, revoked_at=T0)
    expired, _ = add_key(store, expires_at=T0)
    app = FastAPI()

    @app.get("/x")
    def x(p: Principal = Depends(principal_from_request)):
        return {"id": p.id}

    app.add_middleware(PrincipalMiddleware, authenticator=Authenticator(cfg(), store, clock=clock))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        rs = [await c.get("/x", headers=bearer(t)) for t in (revoked, expired, "mt_ZZZZZZZZ_whatever")]
    assert {r.status_code for r in rs} == {401}
    assert {r.content for r in rs} == {b'{"detail":"Unauthorized"}'}


# ── UI headers ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("drop", ["X-MT-User", "X-MT-User-Name", "X-MT-Groups"])
def test_ui_token_missing_header_is_400(auth, drop):
    h = ui_headers()
    del h[drop]
    assert status_of(lambda: auth.resolve(h))[0] == 400


def test_ui_token_without_any_headers_is_400(auth):
    assert status_of(lambda: auth.resolve(bearer(UI)))[0] == 400


@pytest.mark.parametrize("user", ["", "x" * 129])
def test_ui_user_must_be_1_to_128_chars(auth, user):
    assert status_of(lambda: auth.resolve(ui_headers(user=user)))[0] == 400


def test_ui_user_128_chars_ok(auth):
    assert auth.resolve(ui_headers(user="x" * 128)).id == "x" * 128


def test_51_groups_is_400(auth):
    groups = ",".join(f"{i:08d}-0000-0000-0000-000000000000" for i in range(51))
    assert status_of(lambda: auth.resolve(ui_headers(groups=groups)))[0] == 400


def test_50_groups_ok(auth):
    groups = ",".join(f"{i:08d}-0000-0000-0000-000000000000" for i in range(50))
    assert auth.resolve(ui_headers(groups=groups)).roles == {}


@pytest.mark.parametrize("group", ["not-a-guid", G1 + "0", "g" * 36])
def test_malformed_group_is_400(auth, group):
    assert status_of(lambda: auth.resolve(ui_headers(groups=f"{G2},{group}")))[0] == 400


def test_highest_role_wins_and_star_is_kept(auth, store):
    store.replace_mappings([
        RoleMapping(G1, "alpha", "viewer"), RoleMapping(G2, "alpha", "admin"),
        RoleMapping(G1, "beta", "editor"), RoleMapping(G2, "beta", "viewer"),
        RoleMapping(G2, "*", "viewer"),
        RoleMapping("33333333-3333-3333-3333-333333333333", "beta", "admin"),  # not my group
    ], by="t")
    p = auth.resolve(ui_headers(groups=f"{G1}, {G2}"))
    assert p.roles == {"alpha": "admin", "beta": "editor", "*": "viewer"}


def test_admin_group_gives_star_admin(auth, store):
    store.replace_mappings([RoleMapping(G1, "alpha", "viewer")], by="t")
    assert auth.resolve(ui_headers(groups=f"{G1},{ADMIN_G.upper()}")).roles == {"*": "admin"}


def test_groups_known_path_allows_ui_token_without_user(auth):
    p = auth.resolve(bearer(UI), path="/admin/groups/known")
    assert p == Principal(kind="ui", id="", agent=None, roles={})
    assert status_of(lambda: auth.resolve(bearer(UI), path="/admin/other"))[0] == 400


# ── settings / store failures ───────────────────────────────────────────────

def test_legacy_disabled_by_setting_is_401(auth, store):
    store.put_settings({"legacy_token_enabled": False}, by="t")
    assert status_of(lambda: auth.resolve(bearer(API))) == (401, "Unauthorized")


def test_legacy_enabled_setting_true_ok(auth, store):
    store.put_settings({"legacy_token_enabled": True}, by="t")
    assert auth.resolve(bearer(API)).kind == "legacy"


class BrokenStore(InMemoryAdminStore):
    def get_key(self, id):
        raise RuntimeError("db down")

    def get_settings(self):
        raise RuntimeError("db down")

    def list_mappings(self):
        raise RuntimeError("db down")


@pytest.mark.parametrize("headers", [bearer("mt_ABCDEFGH_secret"), bearer(API), ui_headers()])
def test_store_raising_is_503(clock, headers):
    auth = Authenticator(cfg(), BrokenStore(), clock=clock)
    assert status_of(lambda: auth.resolve(headers))[0] == 503


def test_legacy_mode_accepts_only_api_token(clock):
    auth = Authenticator(cfg(), None, clock=clock)
    assert auth.resolve(bearer(API)).kind == "legacy"
    for h in (ui_headers(), bearer("mt_ABCDEFGH_secret"), bearer("nope")):
        assert status_of(lambda h=h: auth.resolve(h)) == (401, "Unauthorized")


def test_non_str_config_values_are_ignored(clock):
    from unittest.mock import MagicMock
    m = MagicMock(api_token=API)
    auth = Authenticator(m, None, clock=clock)
    assert auth.resolve(bearer(API)).kind == "legacy"
    assert status_of(lambda: auth.resolve(bearer(str(m.ui_token))))[0] == 401


# ── caching ─────────────────────────────────────────────────────────────────

def test_revocation_visible_after_cache_ttl(auth, store, clock):
    full, key_id = add_key(store)
    assert auth.resolve(bearer(full)).id == key_id
    store.revoke_key(key_id, T0)
    clock.t += 29
    assert auth.resolve(bearer(full)).id == key_id  # still cached
    clock.t += 2
    assert status_of(lambda: auth.resolve(bearer(full)))[0] == 401


def test_invalidate_drops_caches(auth, store):
    full, key_id = add_key(store)
    auth.resolve(bearer(full))
    store.revoke_key(key_id, T0)
    store.put_settings({"legacy_token_enabled": False}, by="t")
    auth.invalidate()
    assert status_of(lambda: auth.resolve(bearer(full)))[0] == 401
    assert status_of(lambda: auth.resolve(bearer(API)))[0] == 401


def test_settings_and_mappings_cached_for_ttl(auth, store, clock):
    store.replace_mappings([RoleMapping(G1, "alpha", "viewer")], by="t")
    assert auth.resolve(ui_headers()).roles == {"alpha": "viewer"}
    assert auth.resolve(bearer(API)).kind == "legacy"
    store.replace_mappings([RoleMapping(G1, "alpha", "editor")], by="t")
    store.put_settings({"legacy_token_enabled": False}, by="t")
    clock.t += 29
    assert auth.resolve(ui_headers()).roles == {"alpha": "viewer"}
    assert auth.resolve(bearer(API)).kind == "legacy"
    clock.t += 2
    assert auth.resolve(ui_headers()).roles == {"alpha": "editor"}
    assert status_of(lambda: auth.resolve(bearer(API)))[0] == 401


# ── last-used bookkeeping ───────────────────────────────────────────────────

def test_touch_key_at_most_once_per_minute(auth, store, clock):
    full, key_id = add_key(store)
    auth.resolve(bearer(full))
    assert store.get_key(key_id).last_used_at == T0
    clock.t += 59
    auth.resolve(bearer(full))
    assert store.get_key(key_id).last_used_at == T0
    clock.t += 1
    auth.resolve(bearer(full))
    assert store.get_key(key_id).last_used_at == T0 + timedelta(seconds=60)


def test_touch_failure_does_not_fail_request(clock):
    class NoTouch(InMemoryAdminStore):
        def touch_key(self, id, at):
            raise RuntimeError("db down")

    store = NoTouch()
    full, key_id = add_key(store)
    assert Authenticator(cfg(), store, clock=clock).resolve(bearer(full)).id == key_id


def test_legacy_last_used_recorded_at_most_once_per_minute(auth, store, clock):
    auth.resolve(bearer(API))
    assert store.get_settings()["legacy_last_used_at"] == T0.isoformat()
    clock.t += 30
    auth.resolve(bearer(API))
    assert store.get_settings()["legacy_last_used_at"] == T0.isoformat()
    clock.t += 30
    auth.resolve(bearer(API))
    assert store.get_settings()["legacy_last_used_at"] == (T0 + timedelta(seconds=60)).isoformat()


def test_legacy_last_used_failure_swallowed(clock):
    class NoPut(InMemoryAdminStore):
        def put_settings(self, values, by):
            raise RuntimeError("db down")

    assert Authenticator(cfg(), NoPut(), clock=clock).resolve(bearer(API)).kind == "legacy"


def test_key_expiring_later_is_accepted(auth, store, clock):
    full, key_id = add_key(store, expires_at=T0 + timedelta(hours=1))
    assert auth.resolve(bearer(full)).id == key_id
    clock.t += 3600
    auth.invalidate()
    assert status_of(lambda: auth.resolve(bearer(full)))[0] == 401


# ── middleware ──────────────────────────────────────────────────────────────

async def test_middleware_skips_health_and_sets_principal(store, clock):
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/who")
    def who(p: Principal = Depends(principal_from_request)):
        return {"kind": p.kind, "id": p.id}

    app.add_middleware(PrincipalMiddleware, authenticator=Authenticator(cfg(), store, clock=clock))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        h = await c.get("/health")
        who = await c.get("/who", headers=bearer(API))
        bad = await c.get("/who", headers=bearer(UI))
    assert h.status_code == 200
    assert who.json() == {"kind": "legacy", "id": "legacy"}
    assert bad.status_code == 400 and "detail" in bad.json()


# ── review fix round 1 ──────────────────────────────────────────────────────

def test_unknown_key_flood_keeps_caches_bounded(auth, store):
    from memory_mcp.admin import auth as auth_mod
    full, key_id = add_key(store)
    assert auth.resolve(bearer(full)).id == key_id
    for i in range(10_000):
        status_of(lambda i=i: auth.resolve(bearer(f"mt_{i:08d}_x")))
    assert len(auth._keys) <= auth_mod.KEY_CACHE_MAX == 4096
    assert len(auth._unknown_keys) <= auth_mod.NEGATIVE_KEY_CACHE_MAX == 1024
    assert auth.resolve(bearer(full)).id == key_id


def test_unknown_key_negative_cache_expires_after_ttl(auth, store, clock):
    calls = []
    real = store.get_key
    store.get_key = lambda id: calls.append(id) or real(id)
    for _ in range(3):
        status_of(lambda: auth.resolve(bearer("mt_ZZZZZZZZ_x")))
    assert len(calls) == 1
    clock.t += 30
    status_of(lambda: auth.resolve(bearer("mt_ZZZZZZZZ_x")))
    assert len(calls) == 2


def test_fetch_straddling_invalidate_is_not_cached(store, clock):
    full, key_id = add_key(store)
    store.replace_mappings([RoleMapping(G1, "alpha", "viewer")], by="t")
    auth = Authenticator(cfg(), store, clock=clock)

    def racing(read):
        def wrapper(*a):
            value = read(*a)
            auth.invalidate()  # an admin write lands while this read is in flight
            return value
        return wrapper

    store.get_key = racing(store.get_key)
    store.get_settings = racing(store.get_settings)
    store.list_mappings = racing(store.list_mappings)
    auth.resolve(bearer(full))
    auth.resolve(bearer(API))
    auth.resolve(ui_headers())
    assert len(auth._keys) == 0 and len(auth._unknown_keys) == 0
    assert auth._settings is None and auth._mappings is None


def test_equal_api_and_ui_tokens_rejected(store):
    with pytest.raises(ValueError, match="API_TOKEN and UI_TOKEN must differ"):
        Authenticator(cfg(api_token="same", ui_token="same"), store)


def test_user_name_capped_at_256_after_decoding(auth):
    assert auth.resolve(ui_headers(name=quote("é" * 256))).user_name == "é" * 256
    assert status_of(lambda: auth.resolve(ui_headers(name="x" * 257)))[0] == 400


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_legacy_token_fails_closed_on_non_true_setting(auth, store, value):
    store.put_settings({"legacy_token_enabled": value}, by="t")
    assert status_of(lambda: auth.resolve(bearer(API))) == (401, "Unauthorized")


def _who_app(authenticator):
    app = FastAPI()

    @app.get("/who")
    def who(p: Principal = Depends(principal_from_request)):
        return {"id": p.id}

    app.add_middleware(PrincipalMiddleware, authenticator=authenticator)
    return app


@pytest.mark.parametrize("dup", [("authorization", f"Bearer {API}"), ("x-mt-user", "u2"),
                                 ("x-mt-groups", G2)])
async def test_duplicate_auth_headers_are_400(store, clock, dup):
    app = _who_app(Authenticator(cfg(), store, clock=clock))
    headers = [(k.lower(), v) for k, v in ui_headers().items()] + [dup]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/who", headers=headers)
    assert r.status_code == 400 and r.json() == {"detail": "Duplicate header"}


async def test_duplicate_unrelated_header_is_fine(store, clock):
    app = _who_app(Authenticator(cfg(), store, clock=clock))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/who", headers=[("authorization", f"Bearer {API}"), ("x-other", "1"), ("x-other", "2")])
    assert r.json() == {"id": "legacy"}


async def test_401_carries_www_authenticate(store, clock):
    app = _who_app(Authenticator(cfg(), store, clock=clock))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/who")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


async def test_503_comes_back_as_json_through_middleware(clock):
    app = _who_app(Authenticator(cfg(), BrokenStore(), clock=clock))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/who", headers=bearer(API))
    assert r.status_code == 503
    assert r.json() == {"detail": "Admin store unavailable"}

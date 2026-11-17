from datetime import datetime, timezone

import pytest
from qdrant_client import QdrantClient

from memory_mcp.admin.models import ApiKey, AuthzError, Tenant
from memory_mcp.admin.store import InMemoryAdminStore
from memory_mcp.admin.tenants import TenantService
from memory_mcp.kg.registry import Registry
from memory_mcp.store import MemoryStore
from tests.kg.conftest import StubEmbedder

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect")


@pytest.fixture
def mstore():
    c = QdrantClient(":memory:")
    yield MemoryStore("unused", embedder=StubEmbedder(), client=c)
    c.close()


class FakeKG:
    def __init__(self):
        self.graphs = []

    def ensure_graph(self, g):
        self.graphs.append(g)

    def count_entities(self, g, filt):
        return 7


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def admin():
    return InMemoryAdminStore()


@pytest.fixture
def kg():
    return FakeKG()


@pytest.fixture
def svc(admin, mstore, kg, clock):
    return TenantService(admin, mstore, kg, ttl=30.0, clock=clock)


def make_record(name, repo="repo"):
    from memory_mcp.store import MemoryRecord
    now = MemoryStore.now_iso()
    return MemoryRecord(id=MemoryStore.new_id(), type="project", name=name, content=f"{name} body",
                        source_repo=repo, agent="a", tags=[], created_at=now, updated_at=now)


def test_collection_isolation(mstore):
    mstore.ensure_collection("memories_alpha")
    mstore.ensure_collection("memories_beta")
    r = mstore.upsert(make_record("note"), collection="memories_alpha")
    assert mstore.get(r.id, collection="memories_alpha") is not None
    assert mstore.get(r.id, collection="memories_beta") is None
    assert mstore.get(r.id) is None
    assert mstore.search("note", collection="memories_beta") == []
    assert [m.id for m in mstore.search("note", collection="memories_alpha")] == [r.id]
    assert mstore.list_memories(collection="memories_beta")[0] == []
    assert mstore.find_by_name("note", "repo", collection="memories_beta") is None
    assert mstore.find_by_name("note", "repo", collection="memories_alpha").id == r.id
    assert mstore.count("memories_alpha") == 1 and mstore.count("memories_beta") == 0


def test_put_point_extra_payload_cannot_override_core(mstore):
    r = mstore.upsert(make_record("core"))
    record, vec = mstore.get_point(r.id, "memories")
    mstore.put_point(record, vec, "memories", extra_payload={"name": "evil", "content": "x", "origin": "o"})
    got = mstore.get_point(r.id, "memories")[0]
    assert got.name == "core" and got.content == "core body"
    assert mstore.client.retrieve("memories", [r.id], with_payload=True)[0].payload["origin"] == "o"


def test_default_collection_unchanged(mstore):
    r = mstore.upsert(make_record("legacy"))
    assert mstore.get(r.id).name == "legacy"
    assert mstore.count("memories") == 1


def test_get_put_point_roundtrip(mstore):
    mstore.ensure_collection("memories_alpha")
    r = mstore.upsert(make_record("pt"))
    got = mstore.get_point(r.id, "memories")
    assert got is not None
    record, vec = got
    mstore.put_point(record, vec, "memories_alpha", extra_payload={"origin": "x"})
    rec2, vec2 = mstore.get_point(r.id, "memories_alpha")
    assert rec2 == record and vec2 == pytest.approx(vec)
    payload = mstore.client.retrieve("memories_alpha", [r.id], with_payload=True)[0].payload
    assert payload["origin"] == "x"
    assert mstore.get_point("00000000-0000-0000-0000-000000000000", "memories") is None


def test_bootstrap_adopts_memories(svc, admin, kg, mstore):
    svc.bootstrap("alpha", None, ["alpha", "beta"])
    ts = {t.id: t for t in admin.list_tenants()}
    assert ts["alpha"].collection == "memories" and ts["alpha"].name == "alpha"
    assert ts["beta"].collection == "memories_beta" and ts["beta"].name == "beta"
    assert admin.get_settings()["default_tenant"] == "alpha"
    assert kg.graphs == ["alpha", "beta"]
    assert mstore.count("memories_beta") == 0


def test_bootstrap_name(svc, admin):
    svc.bootstrap("alpha", "Alpha Team", [])
    assert admin.get_tenant("alpha").name == "Alpha Team"


def test_bootstrap_requires_id(svc):
    with pytest.raises(RuntimeError, match="BOOTSTRAP_TENANT_ID required"):
        svc.bootstrap(None, None, [])


def test_bootstrap_noop_when_completed(svc, admin):
    svc.bootstrap("alpha", None, [])
    svc.bootstrap(None, None, ["beta"])
    assert [t.id for t in admin.list_tenants()] == ["alpha"]
    assert admin.get_settings()["default_tenant"] == "alpha"


def test_bootstrap_rejects_bad_id(svc, admin):
    with pytest.raises(ValueError):
        svc.bootstrap("Bad Id", None, [])
    assert admin.list_tenants() == []


def test_bootstrap_recovers_after_partial_failure(admin, mstore, kg, clock):
    real = mstore.ensure_collection

    def flaky(c):
        if c == "memories_gamma":
            raise RuntimeError("qdrant down")
        real(c)
    mstore.ensure_collection = flaky
    svc = TenantService(admin, mstore, kg, clock=clock)
    with pytest.raises(RuntimeError):
        svc.bootstrap("alpha", None, ["alpha", "beta", "gamma"])
    assert "default_tenant" not in admin.get_settings()
    assert "gamma" not in {t.id for t in admin.list_tenants()}
    mstore.ensure_collection = real
    svc.bootstrap("alpha", None, ["alpha", "beta", "gamma"])
    ids = [t.id for t in admin.list_tenants()]
    assert ids == ["alpha", "beta", "gamma"]
    assert admin.get_settings()["default_tenant"] == "alpha"


def test_get_miss_reload_rate_limited(admin, mstore, clock):
    calls = []
    orig = admin.list_tenants
    admin.list_tenants = lambda: (calls.append(1), orig())[1]
    svc = TenantService(admin, mstore, clock=clock)
    svc.get("alpha")
    calls.clear()
    clock.t = 1.0
    admin.insert_tenant(Tenant("alpha", "A", "", "memories_alpha", "active"), by="t")
    for _ in range(100):
        assert svc.get("nope") is None
    assert len(calls) == 1
    clock.t = 3.5
    admin.insert_tenant(Tenant("beta", "B", "", "memories_beta", "active"), by="t")
    assert svc.get("beta") is not None


def test_get_stale_miss_refreshes_once(admin, mstore, clock):
    calls = []
    orig = admin.list_tenants
    admin.list_tenants = lambda: (calls.append(1), orig())[1]
    svc = TenantService(admin, mstore, clock=clock)
    svc.all()
    calls.clear()
    clock.t = 100.0
    assert svc.get("nope") is None
    assert len(calls) == 1


def test_create_and_validation(svc, admin, kg, mstore):
    t = svc.create("beta", "Beta", "desc", by="u")
    assert t.collection == "memories_beta" and admin.get_tenant("beta") == t
    assert kg.graphs == ["beta"]
    assert mstore.count("memories_beta") == 0
    with pytest.raises(ValueError):
        svc.create("Bad Id", "x", "", by="u")
    with pytest.raises(ValueError):
        svc.create("beta", "dup", "", by="u")


def test_create_failure_removes_row(admin, mstore, clock):
    def boom(c):
        raise RuntimeError("qdrant down")
    mstore.ensure_collection = boom
    svc = TenantService(admin, mstore, clock=clock)
    with pytest.raises(RuntimeError):
        svc.create("alpha", "A", "", by="u")
    assert admin.get_tenant("alpha") is None
    assert svc.get("alpha") is None


def test_collection_for(svc, admin):
    admin.insert_tenant(Tenant("alpha", "A", "", "memories_alpha", "active"), by="t")
    assert svc.collection_for("alpha") == "memories_alpha"
    with pytest.raises(AuthzError) as e:
        svc.collection_for("nope")
    assert (e.value.status, e.value.code) == (404, "unknown_tenant")
    svc.set_status("alpha", "disabled")
    with pytest.raises(AuthzError) as e:
        svc.collection_for("alpha")
    assert (e.value.status, e.value.code) == (403, "tenant_disabled")
    with pytest.raises(ValueError):
        svc.set_status("alpha", "weird")


def test_cache_reload_on_miss_and_ttl(svc, admin, clock):
    assert svc.all() == []
    admin.insert_tenant(Tenant("alpha", "A", "", "memories_alpha", "active"), by="t")
    assert svc.all() == []  # still within ttl
    assert svc.get("alpha") is not None  # miss reloads
    admin.insert_tenant(Tenant("beta", "B", "", "memories_beta", "active"), by="t")
    assert [t.id for t in svc.all()] == ["alpha"]
    clock.t = 31.0
    assert [t.id for t in svc.all()] == ["alpha", "beta"]


def test_set_status_and_rename(svc, admin):
    admin.insert_tenant(Tenant("alpha", "A", "", "memories_alpha", "active"), by="t")
    assert svc.get("alpha").status == "active"
    assert svc.set_status("alpha", "disabled").status == "disabled"
    assert svc.get("alpha").status == "disabled"
    t = svc.rename("alpha", name="New", description="d")
    assert (t.name, t.description) == ("New", "d") and svc.get("alpha").name == "New"
    assert svc.set_status("zzz", "active") is None and svc.rename("zzz", name="x") is None


def test_stats(svc, admin, mstore):
    svc.create("alpha", "A", "", by="u")
    svc.create("beta", "B", "", by="u")
    mstore.upsert(make_record("n"), collection="memories_alpha")
    now = datetime.now(timezone.utc)

    def key(i, tenants, revoked=None):
        return ApiKey(i, "ag", "h", tenants, "write", (), (), None, now, "u", revoked, None, None)
    admin.insert_key(key("k1", ("alpha", "beta")))
    admin.insert_key(key("k2", ("alpha",), revoked=now))
    admin.insert_key(key("k3", ("beta",)))
    by_id = {s["id"]: s for s in svc.stats()}
    assert by_id["alpha"] == {"id": "alpha", "name": "A", "description": "", "status": "active",
                              "memories": 1, "entities": 7, "keys": 1}
    assert by_id["beta"]["keys"] == 2 and by_id["beta"]["memories"] == 0


def test_stats_without_kg(admin, mstore, clock):
    s = TenantService(admin, mstore, clock=clock)
    s.create("alpha", "A", "", by="u")
    assert s.stats()[0]["entities"] == 0


def test_registry_graph_source():
    reg = Registry.load()
    original = reg.graphs
    assert reg.graphs is original
    reg.set_graph_source(lambda: {"alpha": {"name": "A", "description": ""}})
    assert list(reg.graphs) == ["alpha"]
    reg.require_graph("alpha")

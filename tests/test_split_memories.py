import csv
import importlib.util
import math
import random
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from memory_mcp.embedding import VECTOR_DIM

_spec = importlib.util.spec_from_file_location(
    "split_memories", Path(__file__).resolve().parent.parent / "scripts" / "split_memories.py")
sm = importlib.util.module_from_spec(_spec)
sys.modules["split_memories"] = sm
_spec.loader.exec_module(sm)

# In-memory Qdrant ignores payload indexes and warns about it; the spy test covers them.
pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect")

SRC = "memories"
COLLS = {"alpha": SRC, "beta": "memories_beta", "gamma": "memories_gamma"}
RULES = [sm.parse_rule("source_repo=org-b/*:beta"), sm.parse_rule("tag=gamma:gamma")]


def pt(payload):
    return SimpleNamespace(id=str(uuid.uuid4()), payload=payload)


def snap_ok(_c):
    return "snap-1"


@pytest.fixture
def client():
    c = QdrantClient(":memory:")
    c.create_collection(SRC, vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE))
    rng = random.Random(7)
    def unit():  # cosine collections store normalised vectors; compare like with like
        v = [rng.random() for _ in range(VECTOR_DIM)]
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]

    pts = []
    for i, (repo, tags) in enumerate([
        ("org-a/x", []), ("org-b/svc", ["t"]), ("org-b/other", []),
        ("org-a/y", ["gamma"]), ("org-a/z", ["beta"]),
    ]):
        pts.append(PointStruct(
            id=str(uuid.UUID(int=i + 1)), vector=unit(),
            payload={"name": f"m{i}", "source_repo": repo, "tags": tags, "content": f"c{i}"}))
    c.upsert(SRC, points=pts)
    c.expected = {str(p.id): p for p in pts}
    return c


def do_plan(c, overrides=None):
    return sm.plan(list(sm.scroll_all(c, SRC)), RULES, "alpha", overrides)


def counts(r):
    return {k: v for k, v in r.items() if k not in ("changed_ids", "overwritten", "changed_after_copy")}


def ids(c, coll):
    return {str(r.id) for r in sm.scroll_all(c, coll)}


# --- pure planning -------------------------------------------------------

def test_first_matching_rule_wins():
    p = pt({"source_repo": "org-b/x", "tags": ["gamma"]})
    assert sm.plan([p], RULES, "alpha") == [(p.id, "beta")]
    assert sm.plan([p], RULES[::-1], "alpha") == [(p.id, "gamma")]


def test_override_beats_rules_and_default_applies():
    a, b = pt({"source_repo": "org-b/x"}), pt({"source_repo": "other"})
    assert sm.plan([a, b], RULES, "alpha", {a.id: "gamma"}) == [(a.id, "gamma"), (b.id, "alpha")]


def test_glob_is_case_sensitive_and_tag_exact():
    p = pt({"source_repo": "ORG-B/x", "tags": ["gamma2"]})
    assert sm.plan([p], RULES, "alpha") == [(p.id, "alpha")]


@pytest.mark.parametrize("text", [
    "", "nokind", "tag=x", "tag=:beta", "repo=x:beta", "tag=x:Beta", "tag=x:b", "tag=x:1ab",
    "tag=x:" + "a" * 33,
])
def test_bad_rules_raise(text):
    with pytest.raises(ValueError):
        sm.parse_rule(text)


def test_glob_may_contain_colon():
    assert sm.parse_rule("source_repo=a:b/*:beta") == sm.Rule("source_repo", "a:b/*", "beta")


# --- apply ---------------------------------------------------------------

def test_dry_run_writes_nothing(client, tmp_path, monkeypatch):
    out = tmp_path / "plan.csv"
    monkeypatch.setattr(sm, "QdrantClient", lambda url, **kw: client)
    monkeypatch.setattr(sm, "apply", lambda *a, **k: pytest.fail("apply called in dry run"))
    rc = sm.main(["--qdrant-url", "x", "--default", "alpha", "--rule", "source_repo=org-b/*:beta",
                  "--rule", "tag=gamma:gamma", "--out", str(out)])
    assert rc == 0
    rows = list(csv.DictReader(out.open()))
    assert {r["tenant"] for r in rows} == {"alpha", "beta", "gamma"}
    assert [r["tags"] for r in rows if r["name"] == "m3"] == ["gamma"]
    assert {c.name for c in client.get_collections().collections} == {SRC}
    assert ids(client, SRC) == set(client.expected)


def test_apply_moves_keeps_ids_and_vectors(client):
    res = sm.apply(client, SRC, do_plan(client), COLLS, snap_ok)
    assert counts(res["beta"]) == {"copied": 2, "skipped_existing": 0, "deleted": 2}
    assert counts(res["gamma"]) == {"copied": 1, "skipped_existing": 0, "deleted": 1}
    assert "alpha" not in res
    assert ids(client, SRC) == {str(uuid.UUID(int=1)), str(uuid.UUID(int=5))}
    moved = client.retrieve("memories_beta", ids=[str(uuid.UUID(int=2)), str(uuid.UUID(int=3))],
                            with_vectors=True)
    assert len(moved) == 2
    for r in moved + client.retrieve("memories_gamma", ids=[str(uuid.UUID(int=4))], with_vectors=True):
        want = client.expected[str(r.id)]
        assert r.vector == pytest.approx(want.vector)
        assert r.payload == want.payload


def test_override_moves_a_point(client):
    p = do_plan(client, {str(uuid.UUID(int=1)): "gamma"})
    sm.apply(client, SRC, p, COLLS, snap_ok)
    assert str(uuid.UUID(int=1)) in ids(client, "memories_gamma")
    assert str(uuid.UUID(int=1)) not in ids(client, SRC)


def test_rerun_is_noop(client):
    sm.apply(client, SRC, do_plan(client), COLLS, snap_ok)
    before = {c: ids(client, c) for c in COLLS.values()}
    res = sm.apply(client, SRC, do_plan(client), COLLS, snap_ok)
    assert res == {}
    assert {c: ids(client, c) for c in COLLS.values()} == before


def test_rerun_after_copy_without_delete_skips_existing(client):
    p = do_plan(client)
    sm.apply(client, SRC, [x for x in p if x[1] == "beta"] + [x for x in p if x[1] != "beta"],
             COLLS, snap_ok)
    # put the beta points back in the source, as after a crash between copy and delete
    back = client.retrieve("memories_beta", ids=list(ids(client, "memories_beta")), with_vectors=True)
    client.upsert(SRC, points=[PointStruct(id=r.id, vector=r.vector, payload=r.payload) for r in back])
    res = sm.apply(client, SRC, do_plan(client), COLLS, snap_ok)
    assert counts(res["beta"]) == {"copied": 0, "skipped_existing": 2, "deleted": 2}
    assert len(ids(client, "memories_beta")) == 2


def test_snapshot_failure_writes_and_deletes_nothing(client):
    def boom(_c):
        raise RuntimeError("no snapshot")
    with pytest.raises(RuntimeError, match="no snapshot"):
        sm.apply(client, SRC, do_plan(client), COLLS, boom)
    assert ids(client, SRC) == set(client.expected)
    assert {c.name for c in client.get_collections().collections} == {SRC}


def test_count_mismatch_deletes_nothing(client):
    class Lossy:
        """Forwards to the real client but silently drops upserts."""
        def __init__(self, c):
            self._c = c
        def upsert(self, *a, **k):
            return None
        def __getattr__(self, n):
            return getattr(self._c, n)
    with pytest.raises(sm.ApplyError, match="verify failed"):
        sm.apply(Lossy(client), SRC, do_plan(client), COLLS, snap_ok)
    assert ids(client, SRC) == set(client.expected)


def test_target_collection_matches_server_config(client):
    indexed = []
    orig = client.create_payload_index
    client.create_payload_index = lambda **k: (indexed.append((k["collection_name"], k["field_name"])), orig(**k))
    sm.apply(client, SRC, do_plan(client), COLLS, snap_ok)
    assert {("memories_beta", "name"), ("memories_beta", "source_repo")} <= set(indexed)
    info = client.get_collection("memories_beta")
    assert info.config.params.vectors.size == VECTOR_DIM
    assert info.config.params.vectors.distance == Distance.COSINE


# --- fix round 1: data safety --------------------------------------------

def _seed_target(c, name, uid, vector, payload):
    c.create_collection(name, vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE))
    c.upsert(name, points=[PointStruct(id=str(uuid.UUID(int=uid)), vector=vector, payload=payload)])


def test_preseeded_target_with_different_content_is_overwritten_then_source_deleted(client):
    other = [1.0] + [0.0] * (VECTOR_DIM - 1)
    _seed_target(client, "memories_beta", 2, other, {"name": "stale"})
    res = sm.apply(client, SRC, do_plan(client), COLLS, snap_ok)
    assert res["beta"]["overwritten"] == 1 and res["beta"]["copied"] == 1
    got = client.retrieve("memories_beta", ids=[str(uuid.UUID(int=2))], with_vectors=True)[0]
    want = client.expected[str(uuid.UUID(int=2))]
    assert got.payload == want.payload and got.vector == pytest.approx(want.vector)
    assert str(uuid.UUID(int=2)) not in ids(client, SRC)


def test_verify_fails_when_target_vector_differs(client):
    class Corrupting:
        def __init__(self, c):
            self._c = c
        def upsert(self, coll, points):
            for p in points:
                p.vector = [0.0] * (VECTOR_DIM - 1) + [1.0]
            return self._c.upsert(coll, points=points)
        def __getattr__(self, n):
            return getattr(self._c, n)
    with pytest.raises(sm.ApplyError, match="verify failed"):
        sm.apply(Corrupting(client), SRC, do_plan(client), COLLS, snap_ok)
    assert ids(client, SRC) == set(client.expected)


def _edit_source_before_confirm(monkeypatch, client, uid):
    orig = sm._confirm_unchanged
    done = []
    def hook(c, source, target, batch):
        if not done:
            done.append(1)
            c.set_payload(source, payload={"name": "edited"}, points=[str(uuid.UUID(int=uid))])
        return orig(c, source, target, batch)
    monkeypatch.setattr(sm, "_confirm_unchanged", hook)


def test_source_edit_between_copy_and_delete_is_kept_and_rerun_finishes(client, monkeypatch):
    _edit_source_before_confirm(monkeypatch, client, 2)
    res = sm.apply(client, SRC, do_plan(client), COLLS, snap_ok)
    changed = str(uuid.UUID(int=2))
    assert res["beta"]["changed_ids"] == [changed] and res["beta"]["changed_after_copy"] == 1
    assert res["beta"]["deleted"] == 1
    assert changed in ids(client, SRC)
    monkeypatch.undo()
    res = sm.apply(client, SRC, do_plan(client), COLLS, snap_ok)
    assert res["beta"]["overwritten"] == 1 and res["beta"]["deleted"] == 1
    assert changed not in ids(client, SRC)
    assert client.retrieve("memories_beta", ids=[changed])[0].payload["name"] == "edited"


def _main_args():
    return ["--qdrant-url", "x", "--default", "alpha", "--rule", "source_repo=org-b/*:beta",
            "--rule", "tag=gamma:gamma", "--apply"]


def test_main_exits_nonzero_when_something_changed(client, monkeypatch, capsys):
    monkeypatch.setattr(sm, "QdrantClient", lambda url, **kw: client)
    monkeypatch.setattr(sm, "make_snapshot_fn", lambda c: snap_ok)
    _edit_source_before_confirm(monkeypatch, client, 2)
    assert sm.main(_main_args()) == 1
    cap = capsys.readouterr()
    assert "Some memories changed during the split; rerun --apply to finish." in cap.err
    assert str(uuid.UUID(int=2)) in cap.out


def test_snapshot_skipped_when_nothing_moves(client):
    def boom(_c):
        raise AssertionError("snapshot taken")
    p = [(pid, "alpha") for pid, _ in do_plan(client)]
    assert sm.apply(client, SRC, p, COLLS, boom) == {}


def test_named_vector_source_fails_before_any_write():
    c = QdrantClient(":memory:")
    c.create_collection(SRC, vectors_config={"v": VectorParams(size=VECTOR_DIM, distance=Distance.COSINE)})
    p = [(str(uuid.UUID(int=1)), "beta")]
    with pytest.raises(sm.ApplyError, match="named vectors"):
        sm.apply(c, SRC, p, COLLS, lambda _c: pytest.fail("snapshot taken"))
    assert {x.name for x in c.get_collections().collections} == {SRC}


def test_unmatched_override_warns(client, monkeypatch, tmp_path, capsys):
    ov = tmp_path / "ov.csv"
    ov.write_text("id,tenant\nnot-a-point,gamma\n")
    monkeypatch.setattr(sm, "QdrantClient", lambda url, **kw: client)
    assert sm.main(["--qdrant-url", "x", "--default", "alpha", "--overrides", str(ov)]) == 0
    assert "override id not-a-point matches no point" in capsys.readouterr().err


# --- delete-phase failure and tenant check -----------------------------------

def test_failure_during_delete_reports_deleted_count_and_rerun_finishes(client, monkeypatch, capsys):
    monkeypatch.setattr(sm, "QdrantClient", lambda url, **kw: client)
    monkeypatch.setattr(sm, "make_snapshot_fn", lambda c: snap_ok)
    real_delete, calls = client.delete, []

    def flaky_delete(*a, **kw):
        calls.append(1)
        if len(calls) == 2:  # first tenant's delete went through, the second one fails
            raise RuntimeError("qdrant went away")
        return real_delete(*a, **kw)
    monkeypatch.setattr(client, "delete", flaky_delete)
    assert sm.main(_main_args()) == 1
    err = capsys.readouterr().err
    assert "nothing deleted" not in err
    assert "qdrant went away" in err and "already deleted" in err and "Rerunning --apply is safe" in err
    assert re.search(r"\b[12] point\(s\) were already deleted", err), err

    monkeypatch.setattr(client, "delete", real_delete)
    assert sm.main(_main_args()) == 0
    assert ids(client, SRC) == {str(uuid.UUID(int=1)), str(uuid.UUID(int=5))}


def test_failure_before_delete_still_says_nothing_deleted(client, monkeypatch, capsys):
    monkeypatch.setattr(sm, "QdrantClient", lambda url, **kw: client)

    def no_snapshot(_c):
        def snap(_coll):
            raise sm.ApplyError("snapshot failed")
        return snap
    monkeypatch.setattr(sm, "make_snapshot_fn", no_snapshot)
    assert sm.main(_main_args()) == 1
    assert "nothing deleted from 'memories'" in capsys.readouterr().err


def test_unknown_target_tenants_warn_when_admin_dsn_given(client, monkeypatch, capsys):
    from memory_mcp.admin.models import Tenant
    from memory_mcp.admin.store import InMemoryAdminStore

    admin = InMemoryAdminStore()
    admin.insert_tenant(Tenant("alpha", "Alpha", "", "memories", "active"), by="t")
    admin.insert_tenant(Tenant("beta", "Beta", "", "memories_beta", "active"), by="t")
    monkeypatch.setattr(sm, "QdrantClient", lambda url, **kw: client)
    dsns = []

    def factory(dsn):
        dsns.append(dsn)
        return admin.get_tenant
    args = ["--qdrant-url", "x", "--default", "alpha", "--rule", "source_repo=org-b/*:beta",
            "--rule", "tag=gamma:gamma", "--admin-pg-dsn", "postgresql://admin-db/x"]
    assert sm.main(args, tenant_lookup_factory=factory) == 0
    cap = capsys.readouterr()
    assert dsns == ["postgresql://admin-db/x"]
    assert "tenant 'gamma' has no row in mt_admin" in cap.err
    assert "'beta' has no row" not in cap.err and "'alpha' has no row" not in cap.err
    assert "not checked" not in cap.out


def test_tenants_not_checked_without_admin_dsn(client, monkeypatch, capsys):
    monkeypatch.delenv("ADMIN_PG_DSN", raising=False)
    monkeypatch.setattr(sm, "QdrantClient", lambda url, **kw: client)
    args = ["--qdrant-url", "x", "--default", "alpha", "--rule", "tag=gamma:gamma"]
    assert sm.main(args, tenant_lookup_factory=lambda dsn: pytest.fail("looked up tenants")) == 0
    cap = capsys.readouterr()
    assert "tenants are not checked against mt_admin" in cap.out
    assert "has no row" not in cap.err


def test_admin_dsn_defaults_to_env(monkeypatch):
    monkeypatch.setenv("ADMIN_PG_DSN", "postgresql://from-env/x")
    assert sm.build_parser().parse_args(["--default", "alpha"]).admin_pg_dsn == "postgresql://from-env/x"


def test_main_passes_timeout_to_client(monkeypatch):
    """The pre-apply snapshot can exceed qdrant-client's 5 s default; main() must pass a longer timeout."""
    seen = {}

    def fake_client(url, **kw):
        seen.update(kw, url=url)
        return QdrantClient(":memory:")

    monkeypatch.setattr(sm, "QdrantClient", fake_client)
    with pytest.raises(Exception):
        sm.main(["--qdrant-url", "http://q:6333", "--default", "alpha"])
    assert seen["url"] == "http://q:6333" and seen["timeout"] == 300

    seen.clear()
    with pytest.raises(Exception):
        sm.main(["--qdrant-url", "http://q:6333", "--default", "alpha", "--timeout", "42"])
    assert seen["timeout"] == 42

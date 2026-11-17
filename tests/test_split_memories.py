import csv
import importlib.util
import math
import random
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
    monkeypatch.setattr(sm, "QdrantClient", lambda url: client)
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
    assert res["beta"] == {"copied": 2, "skipped_existing": 0, "deleted": 2}
    assert res["gamma"] == {"copied": 1, "skipped_existing": 0, "deleted": 1}
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
    assert res["beta"] == {"copied": 0, "skipped_existing": 2, "deleted": 2}
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

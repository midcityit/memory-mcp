"""RCL-64 P5: the binary-search trim must give EXACTLY the old pop-loop result.

The old implementations are kept here verbatim as the reference, and both are
run on many random traversal-shaped results and budgets. No DB needed.
"""
import copy
import importlib.util
import json
import random

import pytest

from memory_mcp.kg.query import KGQuery, _size, fit


def _old_fit(result, max_chars, list_keys):
    if _size(result) <= max_chars:
        return result
    result["truncated"] = True
    for k in list_keys:
        lst = result.get(k)
        while isinstance(lst, list) and lst and _size(result) > max_chars:
            lst.pop()
    return result


def _old_trim(result, max_chars):
    if _size(result) <= max_chars:
        return result
    result["truncated"] = True
    nodes = result["nodes"]
    while len(nodes) > 1 and _size(result) > max_chars:
        nodes.pop()
        keep = {n["key"] for n in nodes}
        result["edges"] = [e for e in result["edges"] if e["src"] in keep and e["dst"] in keep]
    for k in ("xrefs", "edges"):
        lst = result.get(k)
        while isinstance(lst, list) and lst and _size(result) > max_chars:
            lst.pop()
    return result


def _random_traversal(rnd):
    n = rnd.randint(1, 120)
    nodes = [{"key": f"k{i}", "display_name": "x" * rnd.randint(0, 60), "kind": "compute",
              "type_short": "T", "depth": 0 if i == 0 else rnd.randint(1, 4)} for i in range(n)]
    nodes = [nodes[0]] + sorted(nodes[1:], key=lambda d: d["depth"])
    keys = [d["key"] for d in nodes]
    edges = [{"src": rnd.choice(keys), "relation": rnd.choice(["runs_on", "depends_on"]),
              "dst": rnd.choice(keys)} for _ in range(rnd.randint(0, 3 * n))]
    r = {"nodes": nodes, "edges": edges, "has_more": rnd.random() < 0.3, "truncated": False}
    if rnd.random() < 0.4:
        r["xrefs"] = [{"src": f"mcit::{rnd.choice(keys)}", "relation": "pattern_from",
                       "dst": "vtv::logical:x" * rnd.randint(1, 3)} for _ in range(rnd.randint(0, 20))]
    return r


@pytest.mark.parametrize("seed", range(2000))
def test_trim_traversal_matches_old(seed):
    rnd = random.Random(seed)
    r = _random_traversal(rnd)
    budget = rnd.randint(50, max(60, _size(r) + 50))
    q = KGQuery.__new__(KGQuery)          # _trim_traversal uses no instance state
    new = q._trim_traversal(copy.deepcopy(r), budget)
    old = _old_trim(copy.deepcopy(r), budget)
    assert json.dumps(new, sort_keys=False) == json.dumps(old, sort_keys=False)


@pytest.mark.parametrize("seed", range(2000))
def test_fit_matches_old(seed):
    rnd = random.Random(1000 + seed)
    r = {"items": [{"key": f"k{i}", "display_name": "y" * rnd.randint(0, 80)} for i in range(rnd.randint(0, 150))],
         "more": [{"v": "z" * rnd.randint(0, 40)} for _ in range(rnd.randint(0, 30))],
         "has_more": False, "cursor": None}
    budget = rnd.randint(10, max(20, _size(r) + 20))
    keys = ("items", "more")
    assert json.dumps(fit(copy.deepcopy(r), budget, keys)) == json.dumps(_old_fit(copy.deepcopy(r), budget, keys))


needs_psycopg = pytest.mark.skipif(importlib.util.find_spec("psycopg") is None,
                                   reason="postgres extra not installed")


class _Cur:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.log.append((sql, args))

    def fetchall(self):
        return self.rows


class _Pool:
    def __init__(self, rows=()):
        self.log, self.rows = [], list(rows)

    def connection(self):
        pool = self

        class _C(_Cur):
            def cursor(self, *a, **k):
                return _Cur(pool.log, pool.rows)

        return _C(pool.log, pool.rows)


def _pg(rows=()):
    from memory_mcp.kg.pg_store import PostgresGraphStore
    s = PostgresGraphStore.__new__(PostgresGraphStore)
    s.pool = _Pool(rows)
    return s


@needs_psycopg
def test_compact_entities_selects_only_compact_fields_and_keeps_order():
    rows = [{"key": "b", "provider": "logical", "type": "jira_issue", "kind": "work", "native_id": "B", "display_name": "B"},
            {"key": "a", "provider": "aws", "type": "ec2/instance", "kind": "compute", "native_id": "A", "display_name": "A"}]
    s = _pg(rows)
    out = s.compact_entities("vtv", ["a", "missing", "b"])
    sql = s.pool.log[-1][0]
    assert "properties" not in sql and "embedding" not in sql and "valid_to IS NULL" in sql
    assert [e.key for e in out] == ["a", "b"]
    assert out[0].compact() == {"key": "a", "display_name": "A", "kind": "compute", "type_short": "instance"}


@needs_psycopg
def test_edges_among_requires_both_ends():
    s = _pg()
    s.edges_among("vtv", ["a", "b", "a"], ["depends_on"], None)
    sql, args = s.pool.log[-1]
    assert "(src = ANY(%s) AND dst = ANY(%s))" in sql and "relation = ANY(%s)" in sql
    assert args[1] == ["a", "b"] and args[2] == ["a", "b"]


@needs_psycopg
def test_new_read_methods_are_not_dual_written():
    from memory_mcp.kg.dual_write import _WRITE_METHODS
    assert "compact_entities" not in _WRITE_METHODS and "edges_among" not in _WRITE_METHODS

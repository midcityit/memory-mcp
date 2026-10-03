# Knowledge Graph (Sprint 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add independent, cross-queryable knowledge graphs to memory-twin (Memory Twin MCP), exposed as 15 `kg_*` MCP tools plus `/kg/*` REST routes, behind `KG_ENABLED`, running on dev with the spec §10 acceptance checks passing.

**Architecture:** A new `memory_mcp.kg` package. `registry` (graphs, relations, type catalogs) and `ids` (native-ID validation) are pure logic. `store.QdrantGraphStore` keeps per-graph Qdrant collections. `service.KGService` handles validated writes, and `query.KGQuery` handles reads. `tools` and `rest` are thin bindings over the service and query layers. The embedding model moves into a shared `embedding.Embedder`.

**Tech Stack:** Python 3.12, FastAPI, `mcp` 2.x (`MCPServer`), qdrant-client ≥1.12 (server v1.13.6), sentence-transformers (MiniLM, 384-d), PyYAML, pytest + pytest-asyncio, OpenTelemetry.

**Spec:** `docs/superpowers/specs/2026-10-03-knowledge-graph-design.md`

**Jira:** epic RCL-30. Task → story mapping: T1→RCL-31, T2+T4→RCL-33, T3→RCL-32, T5→RCL-34, T6→RCL-35, T7→RCL-36, T8+T9→RCL-37, T10→RCL-38. This plan is RCL-41.

## Global Constraints

- `KG_ENABLED` defaults to `false`; with it false, no kg tools, routes or collections are created and the existing behavior is unchanged.
- The existing 4 memory MCP tools and `/memories` REST routes must not change signature or output.
- Embedding model `all-MiniLM-L6-v2`, 384-d, cosine; loaded **once per worker** (shared by MemoryStore and the graph).
- Graph IDs match `^[a-z][a-z0-9_]{1,31}$`. Entity key = `{provider}:{native_id}`; fully qualified key = `{graph}::{key}`.
- Providers: `azure`, `aws`, `gcp`, `vmware`, `hyperv`, `cloudflare`, `kubernetes`, `logical`.
- Collections: `kg_<graph>_entities`, `kg_<graph>_edges`, shared `kg_xrefs`.
- Point IDs: entity `uuid5(KG_NAMESPACE, key)`; edge `uuid5(KG_NAMESPACE, f"{src}|{relation}|{dst}|{valid_from}")`; xref `uuid5(KG_NAMESPACE, f"{src}|{relation}|{dst}")`.
- Current at `t` ⇔ `valid_from_ts ≤ t` and (`valid_to_ts` is null or `valid_to_ts > t`). Default `t` = now.
- `kg_traverse` `max_depth` is capped at 6 (default 3); `kg_impact` default and cap are 6; `kg_path` returns ≤ 3 paths; default response budget is `KG_MAX_CHARS=6000`; duplicate hint threshold is `KG_DUP_THRESHOLD=0.90`.
- Errors are `{"error", "message", "field", "suggestions"}` with codes: `unknown_graph`, `unknown_provider`, `unknown_type`, `invalid_native_id`, `type_id_mismatch`, `unknown_relation`, `relation_class_mismatch`, `kind_constraint`, `endpoint_not_found`, `cross_graph_edge`, `not_found`.
- Never write secret values into code, tests, seed files, Jira, Confluence or memory-twin; use `<cctech-keyvault:NAME>` references.

## Spec amendments made by this plan

These are small clarifications found while planning. Task 10 updates the spec text to match.

1. **`kg_traverse` has no `cursor`.** An over-budget traversal returns `truncated: true`, and the caller narrows with `relations`, `kinds` or a smaller `max_depth`. Paging a BFS frontier adds complexity without a use case. `kg_find` keeps its cursor.
2. **Historical backfill.** `kg_link` and `kg_batch` edge items accept an optional `valid_from`, and batch edge items also accept `valid_to` (plus `retire_reason`). This lets agents record past topology, such as the ms01 placement retired on 2026-09-27.
3. **Kubernetes types are `group/Kind`, with `core` for the core group** (`core/Service`, `apps/Deployment`). Native ID = `{cluster}/{namespace or _cluster}/{group}/{Kind}/{name}`.
4. **`kg_impact` defaults to `max_depth=6`, not 4.** The memory-twin acceptance chain (DNS record → tunnel → gateway → HTTPRoute → Service → Deployment → node) is 6 hops, and a blast-radius query that silently stops short is the dangerous failure mode. Responses stay bounded by the response budget.
5. **Acceptance checks #3 and #4 restated to match reality.** ms01-k8s still runs a warm-rollback copy (Qdrant 1/1, memory-mcp at replicas=0, PV kept until MCIT-251), so the PV *does* have current dependents. The checks become:
   - **#3:** `kg_traverse(mcit, <tunnel>, direction=out, max_depth=1, as_of=2026-09-20)` includes the ms01 memory-mcp Service and not the Envoy Gateway; without `as_of` it is the reverse.
   - **#4:** `kg_impact(mcit, <ms01 Qdrant PV>)` contains only ms01-k8s entities: never the mcit-k8s Deployment or the public hostname. This is the MCIT-251 decommission evidence.
6. **No separate `GraphStore` Protocol class.** The "interface" from spec §6 is the public method set of `QdrantGraphStore`; `KGService` and `KGQuery` depend only on those methods. A `typing.Protocol` gets extracted when a second backend actually appears (YAGNI).
7. **RCL-31 spike result (verified 2026-10-03, qdrant-client local mode):** vectorless collections (`vectors_config={}`), `MatchAny` (including array payloads), `IsNullCondition`, float `Range`, nested `should` filters, and filtered `query_points` all work in `QdrantClient(":memory:")`. Payload indexes are no-ops locally (expected). Store tests therefore use local mode. Task 10 re-verifies vectorless collections against the server (Qdrant v1.13.6) on dev.

## Review Focus

The five uncovered inputs most likely to bite a real user. Each one has a pinning test in the owning task:

1. **The same Azure resource written with different casing or a trailing slash** must resolve to one entity, not two. Pinned in Task 4 (`test_azure_case_and_trailing_slash_normalize`) and Task 6 (`test_upsert_same_arm_id_different_case_updates`).
2. **Linking to a retired entity** must fail with `endpoint_not_found`, not silently create an edge to a decommissioned resource. Pinned in Task 6 (`test_link_to_retired_entity_rejected`).
3. **Cycles** (`peers_with` A↔B, or A→B→C→A) must not loop or duplicate nodes in traverse, path or impact. Pinned in Task 7 (`test_traverse_cycle_terminates_without_duplicates`, `test_impact_cycle_terminates`).
4. **Keys containing `/` and `:`** (ARM IDs, k8s paths) must work through REST paths and survive an export/import round trip. Pinned in Task 9 (`test_get_entity_with_slashes_in_key`, `test_export_import_round_trip`).
5. **A huge neighborhood** (a hub with hundreds of edges) must return valid, truncated JSON within budget, not a cut-off string. Pinned in Task 7 (`test_traverse_truncates_to_budget_valid_json`).

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/memory_mcp/embedding.py` | create | `Embedder`: SentenceTransformer + LRU cache; one per worker |
| `src/memory_mcp/store.py` | modify | `MemoryStore` takes an optional `embedder`; exposes `client` and `embedder` |
| `src/memory_mcp/config.py` | modify | `kg_enabled`, `kg_config_dir`, `kg_dup_threshold`, `kg_max_chars` |
| `src/memory_mcp/mcp_tools.py` | modify | `_init(store, kg=None)` registers kg tools when given |
| `src/memory_mcp/server.py` | modify | Builds the kg stack when `kg_enabled`; mounts the router |
| `src/memory_mcp/kg/__init__.py` | create | `build_kg(...)` factory |
| `src/memory_mcp/kg/models.py` | create | `Entity`, `Edge`, `XRef`, `KGError`, key/time helpers |
| `src/memory_mcp/kg/registry.py` | create | Load graphs, relations, catalogs; `resolve_type` |
| `src/memory_mcp/kg/ids.py` | create | `normalize_native_id(provider, typedef, native_id)` |
| `src/memory_mcp/kg/store.py` | create | `EntityFilter`, `QdrantGraphStore` |
| `src/memory_mcp/kg/service.py` | create | `KGService`: validation pipeline and writes |
| `src/memory_mcp/kg/query.py` | create | `KGQuery`: reads, algorithms, budget |
| `src/memory_mcp/kg/tools.py` | create | `register(mcp, service, query)`: 15 tools |
| `src/memory_mcp/kg/rest.py` | create | `build_router(service, query, require_token)` |
| `src/memory_mcp/kg/config/kg_graphs.yaml` | create | Graph registry (`mcit`, `vtv`) |
| `src/memory_mcp/kg/config/kg_relations.yaml` | create | Relation registry |
| `src/memory_mcp/kg/catalog/*.json` | create | Type catalogs, 8 providers |
| `scripts/build_kg_catalog.py` | create | Generates azure/aws/gcp/cloudflare catalogs |
| `scripts/kg_acceptance.py` | create | Seeds dev and runs the 8 §10 checks over REST |
| `scripts/kg_seed/mcit.jsonl`, `scripts/kg_seed/vtv.jsonl` | create | Acceptance topology |
| `tests/kg/conftest.py` | create | `StubEmbedder`, local-Qdrant fixtures, a small registry |
| `tests/kg/test_*.py` | create | One test module per kg module |
| `tests/test_store.py`, `tests/test_server.py` | modify | Patch path for the embedder; `kg_enabled=False` in mocks |
| `pyproject.toml` | modify | Add `pyyaml>=6`; package data for `kg/config/*`, `kg/catalog/*` |

---

### Task 1: Shared embedder + kg test fixtures (RCL-31)

**Files:**
- Create: `src/memory_mcp/embedding.py`, `tests/test_embedding.py`, `tests/kg/__init__.py`, `tests/kg/conftest.py`
- Modify: `src/memory_mcp/store.py` (imports, `__init__`, `_embed`, remove `_embed_cached`/`_set_model`), `tests/test_store.py:7-14` and `tests/test_staleness.py:7-14` (both `make_store` helpers), `pyproject.toml`

**Interfaces:**
- Produces: `Embedder(model_name: str = EMBEDDING_MODEL)` with `.embed(text: str) -> list[float]` and `.dim -> int`; constants `EMBEDDING_MODEL`, `VECTOR_DIM` in `memory_mcp.embedding`. `MemoryStore(qdrant_url, stale_days=30, embedder: Embedder | None = None)` with properties `client` and `embedder`. Test fixtures `stub_embedder`, `qdrant` (a `QdrantClient(":memory:")`).

- [ ] **Step 1: Write the failing test for `Embedder`**

`tests/test_embedding.py`:
```python
from unittest.mock import patch, MagicMock
from memory_mcp.embedding import Embedder, VECTOR_DIM


def test_embedder_caches_and_returns_list():
    with patch("memory_mcp.embedding.SentenceTransformer") as st:
        model = MagicMock()
        model.encode.return_value = MagicMock(tolist=lambda: [0.5] * VECTOR_DIM)
        st.return_value = model
        e = Embedder()
        assert e.embed("hello") == [0.5] * VECTOR_DIM
        e.embed("hello")
        assert model.encode.call_count == 1  # second call served from cache
        assert e.dim == VECTOR_DIM


def test_separate_embedders_do_not_share_cache():
    with patch("memory_mcp.embedding.SentenceTransformer") as st:
        st.return_value.encode.return_value = MagicMock(tolist=lambda: [0.1] * VECTOR_DIM)
        a, b = Embedder(), Embedder()
        a.embed("x"); b.embed("x")
        assert st.return_value.encode.call_count == 2
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_embedding.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory_mcp.embedding'`

- [ ] **Step 3: Implement `embedding.py`**

```python
"""Shared sentence-embedding model. Load once per worker; reuse everywhere."""
from functools import lru_cache

from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
VECTOR_DIM = 384
_CACHE_SIZE = 512


class Embedder:
    def __init__(self, model_name: str = EMBEDDING_MODEL):
        self._model = SentenceTransformer(model_name)
        # Per-instance cache (a module-level lru_cache would be shared across instances).
        self._cached = lru_cache(maxsize=_CACHE_SIZE)(self._encode)

    def _encode(self, text: str) -> tuple[float, ...]:
        return tuple(self._model.encode(text).tolist())

    def embed(self, text: str) -> list[float]:
        return list(self._cached(text))

    @property
    def dim(self) -> int:
        return VECTOR_DIM
```

- [ ] **Step 4: Run it to verify it passes**

Run: `pytest tests/test_embedding.py -v`
Expected: 2 passed

- [ ] **Step 5: Switch `MemoryStore` to the shared embedder**

In `src/memory_mcp/store.py`:
- Remove `from functools import lru_cache`, `from sentence_transformers import SentenceTransformer`, the `EMBEDDING_MODEL`, `VECTOR_DIM` and `_EMBED_CACHE_SIZE` constants, and the `_embed_cached` / `_set_model` methods.
- Add `from memory_mcp.embedding import Embedder, VECTOR_DIM`.
- Replace `__init__`'s first lines and `_embed` with:

```python
    def __init__(self, qdrant_url: str, stale_days: int = 30, embedder: Embedder | None = None):
        self._client = QdrantClient(url=qdrant_url)
        self._embedder = embedder or Embedder()
        self._stale_days = stale_days
        self._ensure_collection()
        # (keep the existing observable-gauge registration block unchanged)

    @property
    def client(self) -> QdrantClient:
        return self._client

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    def _embed(self, text: str) -> list[float]:
        return self._embedder.embed(text)
```

- [ ] **Step 6: Update both `make_store` helpers for the new patch target**

Both `tests/test_store.py` and `tests/test_staleness.py` patch `memory_mcp.store.SentenceTransformer`, which no longer exists. Replace `tests/test_store.py::make_store` with the block below. In `tests/test_staleness.py`, apply the same change: keep its `stale_days=stale_days` parameter, and drop the two `store._model` lines.

```python
def make_store():
    with patch("memory_mcp.store.QdrantClient"), \
         patch("memory_mcp.embedding.SentenceTransformer") as st:
        st.return_value.encode.return_value = MagicMock(tolist=lambda: [0.1] * 384)
        store = MemoryStore(qdrant_url="http://localhost:6333", stale_days=30)
        store._client = MagicMock()
        return store
```

- [ ] **Step 7: Add the shared kg test fixtures and dependencies**

`tests/kg/__init__.py`: empty file.

`tests/kg/conftest.py`:
```python
import hashlib
import math

import pytest
from qdrant_client import QdrantClient

DIM = 384


class StubEmbedder:
    """Deterministic, model-free embedder: same text -> same unit vector.
    Texts sharing words get similar vectors (bag of hashed tokens)."""
    dim = DIM

    def embed(self, text: str) -> list[float]:
        v = [0.0] * DIM
        for tok in text.lower().replace("/", " ").replace("-", " ").split():
            h = int(hashlib.sha256(tok.encode()).hexdigest(), 16)
            v[h % DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


@pytest.fixture
def stub_embedder():
    return StubEmbedder()


@pytest.fixture
def qdrant():
    c = QdrantClient(":memory:")
    yield c
    c.close()
```

In `pyproject.toml`, add `"pyyaml>=6",` to `dependencies` and change package data to:
```toml
[tool.setuptools.package-data]
memory_mcp = ["templates/*", "kg/config/*.yaml", "kg/catalog/*.json"]
```

- [ ] **Step 8: Run the full suite**

Run: `pip install -e ".[dev]" && pytest -v`
Expected: all existing tests plus 2 new ones pass. `test_store.py` passes with the new patch target.

- [ ] **Step 9: Commit**

```bash
git add src/memory_mcp/embedding.py src/memory_mcp/store.py tests/test_embedding.py tests/test_store.py tests/kg pyproject.toml
git commit -m "refactor: extract shared Embedder; add kg test fixtures (RCL-31)"
```

### Task 2: Models + graph/relation registry (RCL-33, part 1)

**Files:**
- Create: `src/memory_mcp/kg/__init__.py` (empty for now), `src/memory_mcp/kg/models.py`, `src/memory_mcp/kg/registry.py`, `src/memory_mcp/kg/config/kg_graphs.yaml`, `src/memory_mcp/kg/config/kg_relations.yaml`
- Test: `tests/kg/test_models.py`, `tests/kg/test_registry.py`

**Interfaces:**
- Produces (`models`):
  - `KG_NAMESPACE: uuid.UUID`
  - `now_iso() -> str`, `iso_to_ts(s: str | None) -> float | None`
  - `make_key(provider, native_id) -> str`, `fq_key(graph, key) -> str`, `split_fq(fq) -> tuple[str, str]`
  - `entity_point_id(key) -> str`, `edge_point_id(src, relation, dst, valid_from) -> str`, `xref_point_id(src, relation, dst) -> str`
  - `is_current(valid_from: str, valid_to: str | None, at_ts: float) -> bool`
  - `KGError(code, message, field=None, suggestions=None)` with `.to_dict()`
  - dataclasses `Entity`, `Edge`, `XRef`, each with `.to_payload() -> dict` and `@classmethod from_payload(p) -> Self`. `Entity.compact(graph: str | None = None) -> dict`.
- Produces (`registry`):
  - `PROVIDERS: tuple[str, ...]`
  - `RelationDef(name, inverse, cls, impact, src_kinds, dst_kinds)` and `TypeDef(provider, type, kind, aliases)`, both frozen
  - `Registry.load(config_dir: Path | None = None, catalog_dir: Path | None = None) -> Registry`
  - `Registry.require_graph(graph)`, `Registry.graph_ids(graphs: str | list[str]) -> list[str]`, `Registry.require_provider(p)`, `Registry.relation(name) -> RelationDef`, `Registry.inverse_label(name) -> str`, `Registry.propagating_relations() -> list[str]`
  - module function `suggest(word, choices) -> list[str]`

- [ ] **Step 1: Write failing model tests**

`tests/kg/test_models.py`:
```python
import pytest
from memory_mcp.kg.models import (
    Entity, Edge, KGError, make_key, fq_key, split_fq, entity_point_id, edge_point_id,
    iso_to_ts, is_current,
)


def test_keys_and_fq_round_trip():
    k = make_key("kubernetes", "mcit-k8s/digital-twin/apps/Deployment/memory-mcp")
    assert k == "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"
    assert split_fq(fq_key("mcit", k)) == ("mcit", k)


def test_split_fq_rejects_unqualified():
    with pytest.raises(KGError) as e:
        split_fq("kubernetes:x")
    assert e.value.code == "invalid_native_id"


def test_point_ids_deterministic():
    assert entity_point_id("a:b") == entity_point_id("a:b")
    assert edge_point_id("a", "runs_on", "b", "t1") != edge_point_id("a", "runs_on", "b", "t2")


def test_iso_to_ts_handles_naive_and_none():
    assert iso_to_ts(None) is None
    assert iso_to_ts("2026-09-27T00:00:00") == iso_to_ts("2026-09-27T00:00:00+00:00")


def test_is_current_window():
    t = iso_to_ts("2026-09-20T00:00:00+00:00")
    assert is_current("2026-09-01T00:00:00+00:00", "2026-09-27T00:00:00+00:00", t)
    assert not is_current("2026-09-01T00:00:00+00:00", "2026-09-10T00:00:00+00:00", t)
    assert not is_current("2026-09-25T00:00:00+00:00", None, t)


def test_entity_payload_round_trip_and_compact():
    e = Entity(key="logical:MCIT-193", provider="logical", type="jira_issue", kind="work",
               native_id="MCIT-193", display_name="MCIT-193", valid_from="2026-09-08T00:00:00+00:00")
    p = e.to_payload()
    assert p["valid_from_ts"] == iso_to_ts(e.valid_from) and p["valid_to_ts"] is None
    assert Entity.from_payload(p) == e
    assert e.compact("mcit") == {"graph": "mcit", "key": "logical:MCIT-193",
                                 "display_name": "MCIT-193", "kind": "work", "type_short": "jira_issue"}


def test_kgerror_to_dict():
    d = KGError("unknown_type", "nope", field="type", suggestions=["x"]).to_dict()
    assert d == {"error": "unknown_type", "message": "nope", "field": "type", "suggestions": ["x"]}
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/kg/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory_mcp.kg'`

- [ ] **Step 3: Implement `models.py`** (and create an empty `src/memory_mcp/kg/__init__.py`)

```python
"""Knowledge-graph data model, keys, time helpers and errors."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

KG_NAMESPACE = uuid.UUID("6f1d3c52-2f0e-4c7a-9a43-0d6c4b3f2a11")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_to_ts(s: str | None) -> float | None:
    if s is None:
        return None
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def is_current(valid_from: str, valid_to: str | None, at_ts: float) -> bool:
    vf = iso_to_ts(valid_from)
    vt = iso_to_ts(valid_to)
    return vf <= at_ts and (vt is None or vt > at_ts)


def make_key(provider: str, native_id: str) -> str:
    return f"{provider}:{native_id}"


def fq_key(graph: str, key: str) -> str:
    return f"{graph}::{key}"


def split_fq(fq: str) -> tuple[str, str]:
    if "::" not in fq:
        raise KGError("invalid_native_id", f"'{fq}' is not a fully qualified key ({{graph}}::{{key}})", field="key")
    graph, key = fq.split("::", 1)
    return graph, key


def entity_point_id(key: str) -> str:
    return str(uuid.uuid5(KG_NAMESPACE, key))


def edge_point_id(src: str, relation: str, dst: str, valid_from: str) -> str:
    return str(uuid.uuid5(KG_NAMESPACE, f"{src}|{relation}|{dst}|{valid_from}"))


def xref_point_id(src: str, relation: str, dst: str) -> str:
    return str(uuid.uuid5(KG_NAMESPACE, f"{src}|{relation}|{dst}"))


class KGError(Exception):
    def __init__(self, code: str, message: str, field: str | None = None, suggestions: list[str] | None = None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field
        self.suggestions = suggestions or []

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "field": self.field, "suggestions": self.suggestions}


def _with_ts(d: dict) -> dict:
    d["valid_from_ts"] = iso_to_ts(d["valid_from"])
    d["valid_to_ts"] = iso_to_ts(d["valid_to"])
    return d


def _strip_ts(p: dict) -> dict:
    return {k: v for k, v in p.items() if k not in ("valid_from_ts", "valid_to_ts")}


@dataclass
class Entity:
    key: str
    provider: str
    type: str
    kind: str
    native_id: str
    display_name: str
    aliases: list[str] = field(default_factory=list)
    properties: dict = field(default_factory=dict)
    memory_ids: list[str] = field(default_factory=list)
    agent: str = "claude-code"
    created_at: str = ""
    updated_at: str = ""
    valid_from: str = ""
    valid_to: str | None = None

    def to_payload(self) -> dict:
        return _with_ts(asdict(self))

    @classmethod
    def from_payload(cls, p: dict) -> "Entity":
        return cls(**_strip_ts(p))

    def compact(self, graph: str | None = None) -> dict:
        out = {"graph": graph} if graph else {}
        out.update({"key": self.key, "display_name": self.display_name, "kind": self.kind,
                    "type_short": self.type.split("/")[-1].split("::")[-1]})
        return out


@dataclass
class Edge:
    src: str
    relation: str
    dst: str
    properties: dict = field(default_factory=dict)
    evidence_memory_ids: list[str] = field(default_factory=list)
    agent: str = "claude-code"
    created_at: str = ""
    valid_from: str = ""
    valid_to: str | None = None
    retire_reason: str | None = None

    def to_payload(self) -> dict:
        return _with_ts(asdict(self))

    @classmethod
    def from_payload(cls, p: dict) -> "Edge":
        return cls(**_strip_ts(p))


@dataclass
class XRef:
    src: str  # fully qualified
    relation: str
    dst: str  # fully qualified
    note: str
    evidence_memory_ids: list[str] = field(default_factory=list)
    agent: str = "claude-code"
    created_at: str = ""
    valid_to: str | None = None
    dangling: bool = False

    def to_payload(self) -> dict:
        d = asdict(self)
        d["src_graph"] = self.src.split("::", 1)[0]
        d["dst_graph"] = self.dst.split("::", 1)[0]
        return d

    @classmethod
    def from_payload(cls, p: dict) -> "XRef":
        return cls(**{k: v for k, v in p.items() if k not in ("src_graph", "dst_graph")})
```

- [ ] **Step 4: Run model tests**

Run: `pytest tests/kg/test_models.py -v`
Expected: 7 passed

- [ ] **Step 5: Create the config files**

`src/memory_mcp/kg/config/kg_graphs.yaml`:
```yaml
graphs:
  mcit:
    name: "Mid City IT"
    description: "MCIT internal, homelab, chriscastrotech"
  vtv:
    name: "Veritiv"
    description: "Veritiv cloud infra (AWS/Azure/GCP)"
```

`src/memory_mcp/kg/config/kg_relations.yaml` (class `topology` unless noted; `impact` defaults to `none`):
```yaml
relations:
  runs_on:          {inverse: runs,            impact: propagates, src_kinds: [compute, container], dst_kinds: [compute, container, host, org]}
  hosted_on:        {inverse: hosts,           impact: propagates, src_kinds: [compute, container], dst_kinds: [compute, host]}
  member_of:        {inverse: has_member}
  contains:         {inverse: contained_in}
  depends_on:       {inverse: dependency_of,   impact: propagates}
  in_network:       {inverse: network_for,     impact: propagates}
  attached_to:      {inverse: has_attached,    impact: propagates}
  routes_to:        {inverse: routed_from,     impact: propagates}
  resolves_to:      {inverse: resolved_from,   impact: propagates}
  exposed_via:      {inverse: exposes,         impact: propagates}
  secured_by:       {inverse: secures}
  encrypted_by:     {inverse: encrypts,        impact: propagates}
  authenticates_as: {inverse: identity_for,    impact: propagates}
  has_role_on:      {inverse: grants_role_to}
  connects_to:      {inverse: connected_from}
  peers_with:       {inverse: peers_with}
  replicates_to:    {inverse: replicated_from}
  backs_up_to:      {inverse: backup_of}
  logs_to:          {inverse: receives_logs_from}
  deployed_by:      {inverse: deploys,         dst_kinds: [work]}
  defined_in:       {inverse: defines,         dst_kinds: [work]}
  managed_by:       {inverse: manages}
  tracked_in:       {inverse: tracks,          dst_kinds: [work]}
  documented_in:    {inverse: documents,       dst_kinds: [work]}
  pattern_from:           {inverse: pattern_for,           class: reference}
  lessons_from:           {inverse: lessons_for,           class: reference}
  similar_to:             {inverse: similar_to,            class: reference}
  supersedes_approach_of: {inverse: approach_superseded_by, class: reference}
```

- [ ] **Step 6: Write failing registry tests**

`tests/kg/test_registry.py`:
```python
import pytest
from memory_mcp.kg.models import KGError
from memory_mcp.kg.registry import Registry, PROVIDERS


@pytest.fixture
def reg():
    return Registry.load()  # packaged config; catalogs may be empty at this point


def test_packaged_graphs(reg):
    assert set(reg.graphs) == {"mcit", "vtv"}
    assert reg.graph_ids("*") == ["mcit", "vtv"]
    assert reg.graph_ids("vtv") == ["vtv"]
    assert reg.graph_ids(["mcit"]) == ["mcit"]


def test_unknown_graph_suggests(reg):
    with pytest.raises(KGError) as e:
        reg.require_graph("mcti")
    assert e.value.code == "unknown_graph" and "mcit" in e.value.suggestions


def test_relations_loaded_with_defaults(reg):
    r = reg.relation("runs_on")
    assert (r.inverse, r.cls, r.impact) == ("runs", "topology", "propagates")
    assert "container" in r.src_kinds
    assert reg.relation("member_of").impact == "none" and reg.relation("member_of").src_kinds is None
    assert reg.relation("pattern_from").cls == "reference"
    assert reg.inverse_label("tracked_in") == "tracks"


def test_propagating_relations(reg):
    p = set(reg.propagating_relations())
    assert {"runs_on", "hosted_on", "depends_on", "exposed_via", "resolves_to"} <= p
    assert "member_of" not in p and "tracked_in" not in p


def test_unknown_relation_and_provider(reg):
    with pytest.raises(KGError) as e:
        reg.relation("run_on")
    assert e.value.code == "unknown_relation" and "runs_on" in e.value.suggestions
    with pytest.raises(KGError) as e:
        reg.require_provider("azrue")
    assert e.value.code == "unknown_provider" and "azure" in e.value.suggestions
    assert "cloudflare" in PROVIDERS


def test_bad_graph_id_in_config_rejected(tmp_path):
    (tmp_path / "kg_graphs.yaml").write_text("graphs:\n  Bad-ID: {name: x}\n")
    (tmp_path / "kg_relations.yaml").write_text("relations: {}\n")
    with pytest.raises(ValueError):
        Registry.load(config_dir=tmp_path, catalog_dir=tmp_path)
```

- [ ] **Step 7: Run to verify failure**

Run: `pytest tests/kg/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory_mcp.kg.registry'`

- [ ] **Step 8: Implement `registry.py`** (type resolution is added in Task 4)

```python
"""Graph, relation and type registry loaded from YAML/JSON config."""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from memory_mcp.kg.models import KGError

PROVIDERS = ("azure", "aws", "gcp", "vmware", "hyperv", "cloudflare", "kubernetes", "logical")
GRAPH_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_PKG = Path(__file__).parent


def suggest(word: str, choices) -> list[str]:
    return difflib.get_close_matches(word, list(choices), n=3, cutoff=0.5)


@dataclass(frozen=True)
class RelationDef:
    name: str
    inverse: str
    cls: str  # "topology" | "reference"
    impact: str  # "propagates" | "none"
    src_kinds: frozenset[str] | None
    dst_kinds: frozenset[str] | None


@dataclass(frozen=True)
class TypeDef:
    provider: str
    type: str
    kind: str
    aliases: tuple[str, ...] = ()


class Registry:
    def __init__(self, graphs: dict[str, dict], relations: dict[str, RelationDef],
                 types: dict[str, dict[str, TypeDef]]):
        self.graphs = graphs
        self.relations = relations
        self.types = types  # provider -> {native type -> TypeDef}
        self._build_type_index()

    @classmethod
    def load(cls, config_dir: Path | None = None, catalog_dir: Path | None = None) -> "Registry":
        config_dir = Path(config_dir) if config_dir else _PKG / "config"
        catalog_dir = Path(catalog_dir) if catalog_dir else _PKG / "catalog"
        graphs = (yaml.safe_load((config_dir / "kg_graphs.yaml").read_text()) or {}).get("graphs") or {}
        for gid in graphs:
            if not GRAPH_ID_RE.match(gid):
                raise ValueError(f"invalid graph id in kg_graphs.yaml: {gid!r}")
        raw_rel = (yaml.safe_load((config_dir / "kg_relations.yaml").read_text()) or {}).get("relations") or {}
        relations = {
            name: RelationDef(
                name=name, inverse=r["inverse"], cls=r.get("class", "topology"),
                impact=r.get("impact", "none"),
                src_kinds=frozenset(r["src_kinds"]) if r.get("src_kinds") else None,
                dst_kinds=frozenset(r["dst_kinds"]) if r.get("dst_kinds") else None,
            )
            for name, r in raw_rel.items()
        }
        types: dict[str, dict[str, TypeDef]] = {p: {} for p in PROVIDERS}
        if catalog_dir.is_dir():
            for f in sorted(catalog_dir.glob("*.json")):
                doc = json.loads(f.read_text())
                prov = doc["provider"]
                for t in doc["types"]:
                    types.setdefault(prov, {})[t["type"]] = TypeDef(prov, t["type"], t["kind"], tuple(t.get("aliases", [])))
        return cls(graphs, relations, types)

    def _build_type_index(self) -> None:
        """Placeholder hook; Task 4 fills in alias/case-insensitive indexes."""

    def require_graph(self, graph: str) -> None:
        if graph not in self.graphs:
            raise KGError("unknown_graph", f"Unknown graph '{graph}'", field="graph",
                          suggestions=suggest(graph, self.graphs))

    def graph_ids(self, graphs: str | list[str]) -> list[str]:
        if graphs == "*":
            return sorted(self.graphs)
        ids = [graphs] if isinstance(graphs, str) else list(graphs)
        for g in ids:
            self.require_graph(g)
        return ids

    def require_provider(self, provider: str) -> None:
        if provider not in PROVIDERS:
            raise KGError("unknown_provider", f"Unknown provider '{provider}'", field="provider",
                          suggestions=suggest(provider, PROVIDERS))

    def relation(self, name: str) -> RelationDef:
        r = self.relations.get(name)
        if r is None:
            raise KGError("unknown_relation", f"Unknown relation '{name}'", field="relation",
                          suggestions=suggest(name, self.relations))
        return r

    def inverse_label(self, name: str) -> str:
        return self.relation(name).inverse

    def propagating_relations(self) -> list[str]:
        return sorted(n for n, r in self.relations.items() if r.impact == "propagates")
```

- [ ] **Step 9: Run registry tests**

Run: `pytest tests/kg -v`
Expected: all model and registry tests pass

- [ ] **Step 10: Commit**

```bash
git add src/memory_mcp/kg tests/kg/test_models.py tests/kg/test_registry.py
git commit -m "feat(kg): models and graph/relation registry (RCL-33)"
```

### Task 3: Type catalogs + `build_kg_catalog.py` (RCL-32)

**Files:**
- Create: `scripts/build_kg_catalog.py`, `scripts/kg_tf_aliases.yaml`, `scripts/kg_sources/README.md`
- Create (curated): `src/memory_mcp/kg/catalog/vmware.json`, `hyperv.json`, `kubernetes.json`, `logical.json`
- Create (generated in Step 8): `src/memory_mcp/kg/catalog/azure.json`, `aws.json`, `gcp.json`, `cloudflare.json`
- Modify: `src/memory_mcp/kg/registry.py` (add `KINDS`)
- Test: `tests/kg/test_catalog.py`

**Interfaces:**
- Consumes: `TypeDef`, `Registry.load` (Task 2)
- Produces: `registry.KINDS: tuple[str, ...]`; catalog JSON shape `{"provider": str, "types": [{"type": str, "kind": str, "aliases": [str]}]}`. Script functions `classify_kind(provider, type) -> str`, `build_catalog(provider, types, aliases) -> dict`, `load_azure(doc) -> list[str]`, `load_aws_spec(doc) -> list[str]`, `load_lines(text) -> list[str]`, `load_tf_schema(doc, prefix) -> list[str]`.

**Catalog sources.** Kinds come from ordered rule tables in the script; anything unmatched becomes `other` and is listed in the build report.
| Provider | Source | How to fetch |
|---|---|---|
| azure | ARM provider registry | `az provider list --expand "resourceTypes/resourceType" -o json > scripts/kg_sources/azure_providers.json` (needs `az login`) |
| aws | CloudFormation resource specification | `curl -sL https://d1uauaxba7bl26.cloudfront.net/latest/gzip/CloudFormationResourceSpecification.json --compressed -o scripts/kg_sources/aws_cfn_spec.json` |
| gcp | Cloud Asset Inventory supported types | Copy the asset types from https://cloud.google.com/asset-inventory/docs/asset-types into `scripts/kg_sources/gcp_asset_types.txt`, one per line (e.g. `compute.googleapis.com/Instance`) |
| cloudflare | Terraform provider schema | In a temp dir with `required_providers { cloudflare = { source = "cloudflare/cloudflare" } }`: `terraform init && terraform providers schema -json > scripts/kg_sources/cloudflare_schema.json` |

Source files are inputs only: add `scripts/kg_sources/*.json` and `*.txt` to `.gitignore`; only the generated catalogs are committed.

- [ ] **Step 1: Add `KINDS` to the registry**

In `src/memory_mcp/kg/registry.py`, below `PROVIDERS`:
```python
KINDS = ("compute", "container", "network", "dns", "edge", "database", "storage", "identity",
         "security", "secret", "observability", "messaging", "analytics", "ai", "integration",
         "org", "host", "work", "other")
```

- [ ] **Step 2: Write the curated catalogs**

`src/memory_mcp/kg/catalog/vmware.json`:
```json
{"provider": "vmware", "types": [
  {"type": "VirtualMachine", "kind": "compute", "aliases": ["vsphere_virtual_machine"]},
  {"type": "HostSystem", "kind": "host", "aliases": ["vsphere_host"]},
  {"type": "ClusterComputeResource", "kind": "host", "aliases": ["vsphere_compute_cluster"]},
  {"type": "Datastore", "kind": "storage", "aliases": ["vsphere_vmfs_datastore", "vsphere_nas_datastore"]},
  {"type": "DistributedVirtualPortgroup", "kind": "network", "aliases": ["vsphere_distributed_port_group"]},
  {"type": "DistributedVirtualSwitch", "kind": "network", "aliases": ["vsphere_distributed_virtual_switch"]},
  {"type": "Network", "kind": "network", "aliases": []},
  {"type": "Datacenter", "kind": "org", "aliases": ["vsphere_datacenter"]},
  {"type": "Folder", "kind": "org", "aliases": ["vsphere_folder"]},
  {"type": "ResourcePool", "kind": "org", "aliases": ["vsphere_resource_pool"]},
  {"type": "VirtualApp", "kind": "compute", "aliases": ["vsphere_vapp_container"]},
  {"type": "Segment", "kind": "network", "aliases": ["nsxt_policy_segment"]},
  {"type": "Tier0Gateway", "kind": "network", "aliases": ["nsxt_policy_tier0_gateway"]},
  {"type": "Tier1Gateway", "kind": "network", "aliases": ["nsxt_policy_tier1_gateway"]}
]}
```

`src/memory_mcp/kg/catalog/hyperv.json`:
```json
{"provider": "hyperv", "types": [
  {"type": "Host", "kind": "host", "aliases": []},
  {"type": "VM", "kind": "compute", "aliases": ["hyperv_machine_instance"]},
  {"type": "VMSwitch", "kind": "network", "aliases": ["hyperv_network_switch"]},
  {"type": "VHD", "kind": "storage", "aliases": ["hyperv_vhd"]},
  {"type": "Checkpoint", "kind": "storage", "aliases": []},
  {"type": "FailoverCluster", "kind": "host", "aliases": []},
  {"type": "ClusterSharedVolume", "kind": "storage", "aliases": []}
]}
```

`src/memory_mcp/kg/catalog/kubernetes.json` (`core/Cluster` is a pseudo-type that represents the cluster itself inside k8s topology):
```json
{"provider": "kubernetes", "types": [
  {"type": "core/Cluster", "kind": "container", "aliases": []},
  {"type": "core/Node", "kind": "host", "aliases": []},
  {"type": "core/Namespace", "kind": "container", "aliases": ["kubernetes_namespace", "kubernetes_namespace_v1"]},
  {"type": "core/Pod", "kind": "container", "aliases": ["kubernetes_pod", "kubernetes_pod_v1"]},
  {"type": "core/Service", "kind": "network", "aliases": ["kubernetes_service", "kubernetes_service_v1"]},
  {"type": "core/ServiceAccount", "kind": "identity", "aliases": ["kubernetes_service_account", "kubernetes_service_account_v1"]},
  {"type": "core/Secret", "kind": "secret", "aliases": ["kubernetes_secret", "kubernetes_secret_v1"]},
  {"type": "core/ConfigMap", "kind": "other", "aliases": ["kubernetes_config_map", "kubernetes_config_map_v1"]},
  {"type": "core/PersistentVolume", "kind": "storage", "aliases": ["kubernetes_persistent_volume", "kubernetes_persistent_volume_v1"]},
  {"type": "core/PersistentVolumeClaim", "kind": "storage", "aliases": ["kubernetes_persistent_volume_claim", "kubernetes_persistent_volume_claim_v1"]},
  {"type": "apps/Deployment", "kind": "container", "aliases": ["kubernetes_deployment", "kubernetes_deployment_v1"]},
  {"type": "apps/StatefulSet", "kind": "container", "aliases": ["kubernetes_stateful_set", "kubernetes_stateful_set_v1"]},
  {"type": "apps/DaemonSet", "kind": "container", "aliases": ["kubernetes_daemon_set_v1", "kubernetes_daemonset"]},
  {"type": "batch/Job", "kind": "container", "aliases": ["kubernetes_job", "kubernetes_job_v1"]},
  {"type": "batch/CronJob", "kind": "container", "aliases": ["kubernetes_cron_job", "kubernetes_cron_job_v1"]},
  {"type": "networking.k8s.io/Ingress", "kind": "network", "aliases": ["kubernetes_ingress_v1"]},
  {"type": "gateway.networking.k8s.io/Gateway", "kind": "network", "aliases": []},
  {"type": "gateway.networking.k8s.io/HTTPRoute", "kind": "network", "aliases": []},
  {"type": "storage.k8s.io/StorageClass", "kind": "storage", "aliases": ["kubernetes_storage_class", "kubernetes_storage_class_v1"]}
]}
```

`src/memory_mcp/kg/catalog/logical.json`:
```json
{"provider": "logical", "types": [
  {"type": "workload", "kind": "compute", "aliases": []},
  {"type": "repo", "kind": "work", "aliases": ["github_repository"]},
  {"type": "pipeline", "kind": "work", "aliases": []},
  {"type": "tf_workspace", "kind": "work", "aliases": ["tfe_workspace"]},
  {"type": "jira_issue", "kind": "work", "aliases": []},
  {"type": "confluence_page", "kind": "work", "aliases": []},
  {"type": "person", "kind": "org", "aliases": []},
  {"type": "agent", "kind": "org", "aliases": []},
  {"type": "site", "kind": "org", "aliases": []}
]}
```

- [ ] **Step 3: Write the Terraform alias map**

`scripts/kg_tf_aliases.yaml`. This maps Terraform resource type to native type for the generated providers. Extend it in later PRs as agents hit `unknown_type`.
```yaml
azure:
  azurerm_kubernetes_cluster: Microsoft.ContainerService/managedClusters
  azurerm_virtual_network: Microsoft.Network/virtualNetworks
  azurerm_subnet: Microsoft.Network/virtualNetworks/subnets
  azurerm_network_security_group: Microsoft.Network/networkSecurityGroups
  azurerm_network_interface: Microsoft.Network/networkInterfaces
  azurerm_public_ip: Microsoft.Network/publicIPAddresses
  azurerm_application_gateway: Microsoft.Network/applicationGateways
  azurerm_lb: Microsoft.Network/loadBalancers
  azurerm_firewall: Microsoft.Network/azureFirewalls
  azurerm_virtual_hub: Microsoft.Network/virtualHubs
  azurerm_virtual_wan: Microsoft.Network/virtualWans
  azurerm_private_endpoint: Microsoft.Network/privateEndpoints
  azurerm_private_dns_zone: Microsoft.Network/privateDnsZones
  azurerm_dns_zone: Microsoft.Network/dnszones
  azurerm_linux_virtual_machine: Microsoft.Compute/virtualMachines
  azurerm_windows_virtual_machine: Microsoft.Compute/virtualMachines
  azurerm_managed_disk: Microsoft.Compute/disks
  azurerm_key_vault: Microsoft.KeyVault/vaults
  azurerm_user_assigned_identity: Microsoft.ManagedIdentity/userAssignedIdentities
  azurerm_storage_account: Microsoft.Storage/storageAccounts
  azurerm_container_app: Microsoft.App/containerApps
  azurerm_container_app_environment: Microsoft.App/managedEnvironments
  azurerm_container_registry: Microsoft.ContainerRegistry/registries
  azurerm_mssql_server: Microsoft.Sql/servers
  azurerm_mssql_database: Microsoft.Sql/servers/databases
  azurerm_log_analytics_workspace: Microsoft.OperationalInsights/workspaces
  azurerm_application_insights: Microsoft.Insights/components
  azurerm_resource_group: Microsoft.Resources/resourceGroups
  azurerm_kubernetes_fleet_manager: Microsoft.ContainerService/fleets
  azurerm_arc_kubernetes_cluster: Microsoft.Kubernetes/connectedClusters
aws:
  aws_vpc: AWS::EC2::VPC
  aws_subnet: AWS::EC2::Subnet
  aws_security_group: AWS::EC2::SecurityGroup
  aws_instance: AWS::EC2::Instance
  aws_ec2_transit_gateway: AWS::EC2::TransitGateway
  aws_ec2_transit_gateway_vpc_attachment: AWS::EC2::TransitGatewayAttachment
  aws_networkmanager_core_network: AWS::NetworkManager::CoreNetwork
  aws_lb: AWS::ElasticLoadBalancingV2::LoadBalancer
  aws_lb_target_group: AWS::ElasticLoadBalancingV2::TargetGroup
  aws_networkfirewall_firewall: AWS::NetworkFirewall::Firewall
  aws_route53_zone: AWS::Route53::HostedZone
  aws_route53_record: AWS::Route53::RecordSet
  aws_ecs_cluster: AWS::ECS::Cluster
  aws_ecs_service: AWS::ECS::Service
  aws_eks_cluster: AWS::EKS::Cluster
  aws_lambda_function: AWS::Lambda::Function
  aws_db_instance: AWS::RDS::DBInstance
  aws_s3_bucket: AWS::S3::Bucket
  aws_iam_role: AWS::IAM::Role
  aws_kms_key: AWS::KMS::Key
  aws_secretsmanager_secret: AWS::SecretsManager::Secret
  aws_ecr_repository: AWS::ECR::Repository
gcp:
  google_compute_instance: compute.googleapis.com/Instance
  google_compute_network: compute.googleapis.com/Network
  google_compute_subnetwork: compute.googleapis.com/Subnetwork
  google_compute_firewall: compute.googleapis.com/Firewall
  google_container_cluster: container.googleapis.com/Cluster
  google_sql_database_instance: sqladmin.googleapis.com/Instance
  google_storage_bucket: storage.googleapis.com/Bucket
  google_service_account: iam.googleapis.com/ServiceAccount
  google_dns_managed_zone: dns.googleapis.com/ManagedZone
cloudflare: {}  # cloudflare types are the TF names minus "cloudflare_"; the alias is added automatically
```

- [ ] **Step 4: Write failing tests for the script and the catalogs**

`tests/kg/test_catalog.py`:
```python
import importlib.util
import json
from pathlib import Path

import pytest

from memory_mcp.kg.registry import KINDS, PROVIDERS, Registry

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("build_kg_catalog", ROOT / "scripts" / "build_kg_catalog.py")
bkc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bkc)


@pytest.mark.parametrize("provider,typ,kind", [
    ("azure", "Microsoft.ContainerService/managedClusters", "container"),
    ("azure", "Microsoft.Network/networkSecurityGroups", "security"),
    ("azure", "Microsoft.Network/dnszones", "dns"),
    ("azure", "Microsoft.Network/virtualNetworks/subnets", "network"),
    ("azure", "Microsoft.KeyVault/vaults", "secret"),
    ("azure", "Microsoft.Insights/components", "observability"),
    ("azure", "Microsoft.Resources/resourceGroups", "org"),
    ("aws", "AWS::EC2::SecurityGroup", "security"),
    ("aws", "AWS::EC2::TransitGateway", "network"),
    ("aws", "AWS::EC2::Instance", "compute"),
    ("aws", "AWS::RDS::DBInstance", "database"),
    ("aws", "AWS::Route53::HostedZone", "dns"),
    ("gcp", "compute.googleapis.com/Firewall", "security"),
    ("gcp", "compute.googleapis.com/Instance", "compute"),
    ("gcp", "container.googleapis.com/Cluster", "container"),
    ("cloudflare", "dns_record", "dns"),
    ("cloudflare", "zero_trust_tunnel_cloudflared", "network"),
    ("cloudflare", "zero_trust_access_application", "security"),
    ("cloudflare", "zone", "org"),
    ("azure", "Contoso.Unknown/widgets", "other"),
])
def test_classify_kind(provider, typ, kind):
    assert bkc.classify_kind(provider, typ) == kind


def test_loaders():
    az = [{"namespace": "Microsoft.Compute", "resourceTypes": [{"resourceType": "virtualMachines"}, {"resourceType": "disks"}]}]
    assert bkc.load_azure(az) == ["Microsoft.Compute/disks", "Microsoft.Compute/virtualMachines"]
    assert bkc.load_aws_spec({"ResourceTypes": {"AWS::S3::Bucket": {}, "AWS::EC2::VPC": {}}}) == ["AWS::EC2::VPC", "AWS::S3::Bucket"]
    assert bkc.load_lines("# c\ncompute.googleapis.com/Instance\n\n") == ["compute.googleapis.com/Instance"]
    schema = {"provider_schemas": {"registry.terraform.io/cloudflare/cloudflare": {"resource_schemas": {"cloudflare_zone": {}, "cloudflare_dns_record": {}}}}}
    assert bkc.load_tf_schema(schema, "cloudflare_") == ["dns_record", "zone"]


def test_build_catalog_attaches_aliases_and_extras():
    cat = bkc.build_catalog("azure", ["Microsoft.ContainerService/managedClusters"],
                            {"azurerm_kubernetes_cluster": "Microsoft.ContainerService/managedClusters"})
    t = {x["type"]: x for x in cat["types"]}
    assert t["Microsoft.ContainerService/managedClusters"]["aliases"] == ["azurerm_kubernetes_cluster"]
    # ARM scope types are always present, even though `az provider list` does not report them
    assert "Microsoft.Resources/subscriptions" in t and "Microsoft.Resources/resourceGroups" in t


def test_build_catalog_cloudflare_auto_alias():
    cat = bkc.build_catalog("cloudflare", ["zone"], {})
    assert cat["types"][0]["aliases"] == ["cloudflare_zone"]


def test_committed_catalogs_are_valid():
    cat_dir = ROOT / "src" / "memory_mcp" / "kg" / "catalog"
    for f in cat_dir.glob("*.json"):
        doc = json.loads(f.read_text())
        assert doc["provider"] in PROVIDERS, f
        assert doc["types"], f
        for t in doc["types"]:
            assert t["kind"] in KINDS, (f, t)
    reg = Registry.load()
    assert "core/Deployment" not in reg.types["kubernetes"] and "apps/Deployment" in reg.types["kubernetes"]
```

- [ ] **Step 5: Run to verify failure**

Run: `pytest tests/kg/test_catalog.py -v`
Expected: FAIL (`scripts/build_kg_catalog.py` not found; `KINDS` exists from Step 1)

- [ ] **Step 6: Implement `scripts/build_kg_catalog.py`**

```python
#!/usr/bin/env python3
"""Generate kg type catalogs for providers with machine-readable type lists.

Usage:
  python scripts/build_kg_catalog.py azure      scripts/kg_sources/azure_providers.json
  python scripts/build_kg_catalog.py aws        scripts/kg_sources/aws_cfn_spec.json
  python scripts/build_kg_catalog.py gcp        scripts/kg_sources/gcp_asset_types.txt
  python scripts/build_kg_catalog.py cloudflare scripts/kg_sources/cloudflare_schema.json
Writes src/memory_mcp/kg/catalog/<provider>.json and prints unmapped (kind=other) types to stderr.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "src" / "memory_mcp" / "kg" / "catalog"

# Ordered (first match wins). Patterns are matched case-insensitively against the full native type.
RULES: dict[str, list[tuple[str, str]]] = {
    "azure": [
        (r"^Microsoft\.Network/(dnszones|privateDnsZones|dnsResolvers)", "dns"),
        (r"^Microsoft\.Network/(frontDoors|FrontDoorWebApplicationFirewallPolicies)|^Microsoft\.Cdn/", "edge"),
        (r"^Microsoft\.Network/(networkSecurityGroups|azureFirewalls|firewallPolicies|ApplicationGatewayWebApplicationFirewallPolicies|ddosProtectionPlans)", "security"),
        (r"^Microsoft\.Network/", "network"),
        (r"^Microsoft\.(ContainerService|ContainerInstance|ContainerRegistry|App|Kubernetes|KubernetesConfiguration|RedHatOpenShift)/", "container"),
        (r"^Microsoft\.HybridCompute/", "host"),
        (r"^Microsoft\.(Compute|Web|Batch|DesktopVirtualization)/", "compute"),
        (r"^Microsoft\.(Sql|DBforPostgreSQL|DBforMySQL|DBforMariaDB|DocumentDB|Cache)/", "database"),
        (r"^Microsoft\.(Storage|StorageSync|NetApp|RecoveryServices|DataProtection)/", "storage"),
        (r"^Microsoft\.KeyVault/", "secret"),
        (r"^Microsoft\.(ManagedIdentity|Authorization|AAD|AzureActiveDirectory)/", "identity"),
        (r"^Microsoft\.(Security|SecurityInsights)/", "security"),
        (r"^Microsoft\.(Insights|OperationalInsights|Monitor|AlertsManagement|Dashboard)/", "observability"),
        (r"^Microsoft\.(ServiceBus|EventHub|EventGrid|Relay|NotificationHubs)/", "messaging"),
        (r"^Microsoft\.(Synapse|Kusto|DataFactory|Databricks|Fabric|PowerBIDedicated|StreamAnalytics|Purview)/", "analytics"),
        (r"^Microsoft\.(CognitiveServices|MachineLearningServices|Search)/", "ai"),
        (r"^Microsoft\.(Logic|ApiManagement|Web/connections)/", "integration"),
        (r"^Microsoft\.(Resources|Management|Subscription|Billing|Portal)/", "org"),
    ],
    "aws": [
        (r"^AWS::EC2::(SecurityGroup|NetworkAcl)", "security"),
        (r"^AWS::EC2::(VPC|Subnet|RouteTable|Route|TransitGateway|NatGateway|InternetGateway|EgressOnlyInternetGateway|VPCEndpoint|EIP|NetworkInterface|VPNGateway|VPNConnection|CustomerGateway|PrefixList|IPAM)", "network"),
        (r"^AWS::(EC2|Lambda|AutoScaling|ElasticBeanstalk|Batch|Lightsail)::", "compute"),
        (r"^AWS::(ECS|EKS|ECR|AppRunner)::", "container"),
        (r"^AWS::(NetworkFirewall|WAFv2|WAF|WAFRegional|Shield|GuardDuty|SecurityHub|Inspector|Macie)::", "security"),
        (r"^AWS::(ElasticLoadBalancing|ElasticLoadBalancingV2|NetworkManager|DirectConnect|VpcLattice)::", "network"),
        (r"^AWS::Route53(Resolver)?::", "dns"),
        (r"^AWS::(CloudFront|GlobalAccelerator)::", "edge"),
        (r"^AWS::(RDS|DynamoDB|ElastiCache|DocDB|Neptune|MemoryDB)::", "database"),
        (r"^AWS::(S3|EFS|FSx|Backup|StorageGateway)::", "storage"),
        (r"^AWS::(SecretsManager|KMS|SSM::Parameter|ACMPCA)", "secret"),
        (r"^AWS::(IAM|SSO|Cognito|IdentityStore)::", "identity"),
        (r"^AWS::(CloudWatch|Logs|CloudTrail|XRay|Oam)::", "observability"),
        (r"^AWS::(SQS|SNS|Events|Kinesis|MSK|AmazonMQ|Pipes)::", "messaging"),
        (r"^AWS::(Glue|Athena|Redshift|EMR|LakeFormation|QuickSight)::", "analytics"),
        (r"^AWS::(SageMaker|Bedrock|Comprehend|Rekognition)::", "ai"),
        (r"^AWS::(StepFunctions|ApiGateway|ApiGatewayV2|AppSync|AppFlow)::", "integration"),
        (r"^AWS::(Organizations|ControlTower|Budgets|CE)::", "org"),
    ],
    "gcp": [
        (r"^dns\.googleapis\.com/", "dns"),
        (r"^compute\.googleapis\.com/(Firewall|FirewallPolicy|SecurityPolicy)", "security"),
        (r"^compute\.googleapis\.com/(Network|Subnetwork|Router|Address|GlobalAddress|ForwardingRule|GlobalForwardingRule|BackendService|TargetHttpProxy|TargetHttpsProxy|UrlMap|VpnGateway|VpnTunnel|Route|InterconnectAttachment|NetworkEndpointGroup)", "network"),
        (r"^compute\.googleapis\.com/", "compute"),
        (r"^(container|artifactregistry|run|gkehub)\.googleapis\.com/", "container"),
        (r"^(cloudfunctions|appengine)\.googleapis\.com/", "compute"),
        (r"^(sqladmin|spanner|bigtableadmin|firestore|redis|alloydb)\.googleapis\.com/", "database"),
        (r"^(storage|file)\.googleapis\.com/", "storage"),
        (r"^(secretmanager|cloudkms)\.googleapis\.com/", "secret"),
        (r"^iam\.googleapis\.com/", "identity"),
        (r"^(logging|monitoring)\.googleapis\.com/", "observability"),
        (r"^pubsub\.googleapis\.com/", "messaging"),
        (r"^(bigquery|dataflow|dataproc|composer)\.googleapis\.com/", "analytics"),
        (r"^(aiplatform|ml)\.googleapis\.com/", "ai"),
        (r"^(apigateway|apigee|workflows)\.googleapis\.com/", "integration"),
        (r"^cloudresourcemanager\.googleapis\.com/", "org"),
    ],
    "cloudflare": [
        (r"^(dns_record|record|dns_)", "dns"),
        (r"^(zero_trust_access|access_|ruleset|waf|firewall|zero_trust_gateway|zero_trust_device|zero_trust_list)", "security"),
        (r"^(zero_trust_tunnel|tunnel|magic_|load_balancer)", "network"),
        (r"^(workers?_|pages_)", "compute"),
        (r"^r2_", "storage"),
        (r"^(zone|account)", "org"),
        (r".*", "edge"),
    ],
}

# Types that `az provider list` does not report but ARM IDs use.
EXTRAS = {"azure": ["Microsoft.Resources/subscriptions", "Microsoft.Resources/resourceGroups"]}


def classify_kind(provider: str, typ: str) -> str:
    for pattern, kind in RULES.get(provider, []):
        if re.search(pattern, typ, re.IGNORECASE):
            return kind
    return "other"


def load_azure(doc: list[dict]) -> list[str]:
    return sorted({f"{p['namespace']}/{rt['resourceType']}" for p in doc for rt in p.get("resourceTypes", [])})


def load_aws_spec(doc: dict) -> list[str]:
    return sorted(doc["ResourceTypes"])


def load_lines(text: str) -> list[str]:
    return sorted({ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")})


def load_tf_schema(doc: dict, prefix: str) -> list[str]:
    out = set()
    for prov in doc["provider_schemas"].values():
        out |= {k[len(prefix):] for k in prov.get("resource_schemas", {}) if k.startswith(prefix)}
    return sorted(out)


def build_catalog(provider: str, types: list[str], aliases: dict[str, str]) -> dict:
    by_type: dict[str, list[str]] = {}
    for tf_name, native in aliases.items():
        by_type.setdefault(native, []).append(tf_name)
    all_types = sorted(set(types) | set(EXTRAS.get(provider, [])))
    entries = []
    for t in all_types:
        al = sorted(by_type.get(t, []))
        if provider == "cloudflare":
            al = sorted(set(al) | {f"cloudflare_{t}"})
        entries.append({"type": t, "kind": classify_kind(provider, t), "aliases": al})
    return {"provider": provider, "types": entries}


def main(argv: list[str]) -> int:
    provider, src = argv[1], Path(argv[2])
    raw = src.read_text()
    types = {
        "azure": lambda: load_azure(json.loads(raw)),
        "aws": lambda: load_aws_spec(json.loads(raw)),
        "gcp": lambda: load_lines(raw),
        "cloudflare": lambda: load_tf_schema(json.loads(raw), "cloudflare_"),
    }[provider]()
    aliases = (yaml.safe_load((ROOT / "scripts" / "kg_tf_aliases.yaml").read_text()) or {}).get(provider) or {}
    missing = sorted(set(aliases.values()) - set(types) - set(EXTRAS.get(provider, [])))
    if missing:
        print(f"WARNING alias targets not in source: {missing}", file=sys.stderr)
    cat = build_catalog(provider, types, aliases)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{provider}.json").write_text(json.dumps(cat, indent=0) + "\n")
    other = [t["type"] for t in cat["types"] if t["kind"] == "other"]
    print(f"{provider}: {len(cat['types'])} types, {len(other)} unmapped", file=sys.stderr)
    for t in other:
        print(f"  other: {t}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

`scripts/kg_sources/README.md`: the "Catalog sources" table from above. Append to `.gitignore`:
```
scripts/kg_sources/*.json
scripts/kg_sources/*.txt
```

- [ ] **Step 7: Run tests**

Run: `pytest tests/kg/test_catalog.py -v`
Expected: all pass (only the curated catalogs exist so far; `test_committed_catalogs_are_valid` covers them)

- [ ] **Step 8: Fetch sources and generate the four catalogs**

Run the four fetch commands in the "Catalog sources" table, then:
```bash
python scripts/build_kg_catalog.py azure      scripts/kg_sources/azure_providers.json  2> /tmp/kg_azure_report.txt
python scripts/build_kg_catalog.py aws        scripts/kg_sources/aws_cfn_spec.json     2> /tmp/kg_aws_report.txt
python scripts/build_kg_catalog.py gcp        scripts/kg_sources/gcp_asset_types.txt   2> /tmp/kg_gcp_report.txt
python scripts/build_kg_catalog.py cloudflare scripts/kg_sources/cloudflare_schema.json 2> /tmp/kg_cloudflare_report.txt
grep -h "types," /tmp/kg_*_report.txt; grep -h WARNING /tmp/kg_*_report.txt
```
Expected: one summary line per provider (`<provider>: N types, M unmapped`). Review each report. If an unmapped type is clearly common (e.g. an AWS service you use), add a rule to `RULES` and a parametrized case to `test_classify_kind`, then rerun. Fix any `WARNING alias targets not in source` by correcting the alias's native type casing to match the source.

- [ ] **Step 9: Run the full kg suite and check the catalog size**

Run: `pytest tests/kg -v && du -sh src/memory_mcp/kg/catalog`
Expected: all pass, `test_committed_catalogs_are_valid` covering 8 files; total catalog size under 2 MB.

- [ ] **Step 10: Commit**

```bash
git add scripts/build_kg_catalog.py scripts/kg_tf_aliases.yaml scripts/kg_sources/README.md .gitignore \
        src/memory_mcp/kg/catalog src/memory_mcp/kg/registry.py tests/kg/test_catalog.py
git commit -m "feat(kg): provider type catalogs + generator (RCL-32)"
```

### Task 4: Type resolution + native-ID validation (RCL-33, part 2)

**Files:**
- Modify: `src/memory_mcp/kg/registry.py` (`_build_type_index`, add `resolve_type`, `check_kinds`)
- Create: `src/memory_mcp/kg/ids.py`
- Test: `tests/kg/test_resolve.py`, `tests/kg/test_ids.py`

**Interfaces:**
- Consumes: `Registry`, `TypeDef`, `RelationDef`, `suggest`, `KGError` (Tasks 2–3)
- Produces: `Registry.resolve_type(provider: str, type_str: str) -> TypeDef`; `Registry.check_kinds(rel: RelationDef, src_kind: str, dst_kind: str) -> None`; `ids.normalize_native_id(provider: str, td: TypeDef, native_id: str) -> str`

- [ ] **Step 1: Write failing resolution tests**

`tests/kg/test_resolve.py`:
```python
import pytest
from memory_mcp.kg.models import KGError
from memory_mcp.kg.registry import Registry


@pytest.fixture(scope="module")
def reg():
    return Registry.load()


def test_exact_alias_and_case_insensitive(reg):
    assert reg.resolve_type("kubernetes", "apps/Deployment").kind == "container"
    assert reg.resolve_type("kubernetes", "kubernetes_deployment_v1").type == "apps/Deployment"
    assert reg.resolve_type("kubernetes", "APPS/deployment").type == "apps/Deployment"
    assert reg.resolve_type("azure", "azurerm_kubernetes_cluster").type == "Microsoft.ContainerService/managedClusters"
    assert reg.resolve_type("logical", "jira_issue").kind == "work"


def test_k8s_crd_pattern_accepted(reg):
    td = reg.resolve_type("kubernetes", "trident.qnap.io/TridentBackend")
    assert (td.type, td.kind) == ("trident.qnap.io/TridentBackend", "container")


def test_unknown_type_suggests(reg):
    with pytest.raises(KGError) as e:
        reg.resolve_type("kubernetes", "apps/Deploymnet")
    assert e.value.code == "unknown_type" and "apps/Deployment" in e.value.suggestions
    with pytest.raises(KGError):
        reg.resolve_type("kubernetes", "gateway.networking.k8s.io/HTTPRoutee")  # known group, unknown kind


def test_unknown_provider_before_type(reg):
    with pytest.raises(KGError) as e:
        reg.resolve_type("azzure", "x")
    assert e.value.code == "unknown_provider"


def test_check_kinds(reg):
    reg.check_kinds(reg.relation("hosted_on"), "compute", "host")
    reg.check_kinds(reg.relation("member_of"), "anything", "else")  # unconstrained
    with pytest.raises(KGError) as e:
        reg.check_kinds(reg.relation("tracked_in"), "container", "network")
    assert e.value.code == "kind_constraint"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/kg/test_resolve.py -v`
Expected: FAIL with `AttributeError: 'Registry' object has no attribute 'resolve_type'`

- [ ] **Step 3: Implement resolution in `registry.py`**

Add `K8S_CRD_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+/[A-Z][A-Za-z0-9]+$")` at module level. CRD groups are always dotted (e.g. `trident.qnap.io`). A typo in a known group such as `apps/Deploymnet` must not pass as a CRD. Replace `_build_type_index` and add the two methods:
```python
    def _build_type_index(self) -> None:
        self._alias: dict[str, dict[str, TypeDef]] = {}
        self._lower: dict[str, dict[str, TypeDef]] = {}
        for prov, types in self.types.items():
            self._alias[prov] = {a: td for td in types.values() for a in td.aliases}
            self._lower[prov] = {t.lower(): td for t, td in types.items()}

    def resolve_type(self, provider: str, type_str: str) -> TypeDef:
        self.require_provider(provider)
        types = self.types.get(provider, {})
        td = types.get(type_str) or self._alias[provider].get(type_str) or self._lower[provider].get(type_str.lower())
        if td:
            return td
        if provider == "kubernetes" and K8S_CRD_RE.match(type_str):
            known_groups = {t.split("/")[0] for t in types}
            if type_str.split("/")[0] not in known_groups:
                return TypeDef("kubernetes", type_str, "container")
        choices = list(types) + list(self._alias[provider])
        raise KGError("unknown_type", f"Unknown {provider} type '{type_str}'", field="type",
                      suggestions=suggest(type_str, choices))

    def check_kinds(self, rel: RelationDef, src_kind: str, dst_kind: str) -> None:
        if rel.src_kinds and src_kind not in rel.src_kinds:
            raise KGError("kind_constraint", f"'{rel.name}' source must be one of {sorted(rel.src_kinds)}, got '{src_kind}'",
                          field="src")
        if rel.dst_kinds and dst_kind not in rel.dst_kinds:
            raise KGError("kind_constraint", f"'{rel.name}' target must be one of {sorted(rel.dst_kinds)}, got '{dst_kind}'",
                          field="dst")
```

- [ ] **Step 4: Run resolution tests**

Run: `pytest tests/kg/test_resolve.py -v`
Expected: 5 passed

- [ ] **Step 5: Write failing ID tests**

`tests/kg/test_ids.py`:
```python
import pytest
from memory_mcp.kg.ids import normalize_native_id
from memory_mcp.kg.models import KGError
from memory_mcp.kg.registry import TypeDef

AKS = TypeDef("azure", "Microsoft.ContainerService/managedClusters", "container")
VM = TypeDef("azure", "Microsoft.Compute/virtualMachines", "compute")
SUBNET = TypeDef("azure", "Microsoft.Network/virtualNetworks/subnets", "network")
RG = TypeDef("azure", "Microsoft.Resources/resourceGroups", "org")
EC2 = TypeDef("aws", "AWS::EC2::Instance", "compute")
ALB = TypeDef("aws", "AWS::ElasticLoadBalancingV2::LoadBalancer", "network")
GCE = TypeDef("gcp", "compute.googleapis.com/Instance", "compute")
DEPLOY = TypeDef("kubernetes", "apps/Deployment", "container")
CF_DNS = TypeDef("cloudflare", "dns_record", "dns")
HV_VM = TypeDef("hyperv", "VM", "compute")
VS_VM = TypeDef("vmware", "VirtualMachine", "compute")
JIRA = TypeDef("logical", "jira_issue", "work")
REPO = TypeDef("logical", "repo", "work")
SUB = "/subscriptions/00000000-0000-0000-0000-000000000001"


def test_azure_case_and_trailing_slash_normalize():
    a = normalize_native_id("azure", AKS, f"{SUB}/resourceGroups/RG-1/providers/Microsoft.ContainerService/managedClusters/AKS-VTV-PROD/")
    b = normalize_native_id("azure", AKS, f"{SUB}/resourcegroups/rg-1/providers/microsoft.containerservice/managedclusters/aks-vtv-prod")
    assert a == b == b.lower()


def test_azure_child_and_scope_types():
    normalize_native_id("azure", SUBNET, f"{SUB}/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/v1/subnets/s1")
    normalize_native_id("azure", RG, f"{SUB}/resourceGroups/rg")


def test_azure_type_id_mismatch():
    nic = f"{SUB}/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic1"
    with pytest.raises(KGError) as e:
        normalize_native_id("azure", VM, nic)
    assert e.value.code == "type_id_mismatch" and "microsoft.network/networkinterfaces" in e.value.message


@pytest.mark.parametrize("bad", ["aks-vtv-prod", "/subscriptions/x/providers/Microsoft.Compute/virtualMachines",
                                 f"{SUB}/resourceGroups/rg/providers/Microsoft.Compute"])
def test_azure_invalid(bad):
    with pytest.raises(KGError) as e:
        normalize_native_id("azure", VM, bad)
    assert e.value.code in ("invalid_native_id", "type_id_mismatch")


def test_aws_arn_and_service_mapping():
    assert normalize_native_id("aws", EC2, "arn:aws:ec2:us-west-2:891377069618:instance/i-0abc") == \
        "arn:aws:ec2:us-west-2:891377069618:instance/i-0abc"
    normalize_native_id("aws", ALB, "arn:aws:elasticloadbalancing:us-west-2:891377069618:loadbalancer/app/x/123")
    with pytest.raises(KGError) as e:
        normalize_native_id("aws", EC2, "arn:aws:s3:::bucket")
    assert e.value.code == "type_id_mismatch"
    with pytest.raises(KGError):
        normalize_native_id("aws", EC2, "i-0abc")


def test_gcp():
    nid = "//compute.googleapis.com/projects/p/zones/us-west1-a/instances/vm1"
    assert normalize_native_id("gcp", GCE, nid) == nid
    with pytest.raises(KGError) as e:
        normalize_native_id("gcp", GCE, "//storage.googleapis.com/projects/_/buckets/b")
    assert e.value.code == "type_id_mismatch"


def test_kubernetes():
    nid = "mcit-k8s/digital-twin/apps/Deployment/memory-mcp"
    assert normalize_native_id("kubernetes", DEPLOY, nid) == nid
    with pytest.raises(KGError) as e:
        normalize_native_id("kubernetes", DEPLOY, "mcit-k8s/digital-twin/apps/StatefulSet/qdrant")
    assert e.value.code == "type_id_mismatch"
    with pytest.raises(KGError):
        normalize_native_id("kubernetes", DEPLOY, "mcit-k8s/memory-mcp")


def test_cloudflare_hyperv_vmware():
    acct = "0123456789abcdef0123456789ABCDEF"
    assert normalize_native_id("cloudflare", CF_DNS, f"{acct}/chriscastrotech.com/dns_record/memory-mcp") == \
        f"{acct.lower()}/chriscastrotech.com/dns_record/memory-mcp"
    assert normalize_native_id("hyperv", HV_VM, "MS01.int.example.net/VM/opi-5") == "ms01.int.example.net/VM/opi-5"
    with pytest.raises(KGError):
        normalize_native_id("hyperv", HV_VM, "ms01.int.example.net/VMSwitch/ext")
    assert normalize_native_id("vmware", VS_VM, "VC01.corp.local/DC1/vm/web01") == "vc01.corp.local/DC1/vm/web01"


def test_logical():
    assert normalize_native_id("logical", JIRA, "MCIT-193") == "MCIT-193"
    assert normalize_native_id("logical", REPO, "midcityit/memory-mcp") == "midcityit/memory-mcp"
    for bad, td in [("mcit 193", JIRA), ("memory-mcp", REPO), ("  ", JIRA)]:
        with pytest.raises(KGError):
            normalize_native_id("logical", td, bad)
```

- [ ] **Step 6: Run to verify failure**

Run: `pytest tests/kg/test_ids.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory_mcp.kg.ids'`

- [ ] **Step 7: Implement `ids.py`**

```python
"""Per-provider native-ID validation, normalization and type/ID consistency."""
from __future__ import annotations

import re

from memory_mcp.kg.models import KGError
from memory_mcp.kg.registry import TypeDef

_AZ = re.compile(r"^/subscriptions/[^/]+(?:/resourcegroups/[^/]+)?(?:/providers/(?P<rest>.+))?$")
_ARN = re.compile(r"^arn:(aws|aws-cn|aws-us-gov):(?P<svc>[a-z0-9-]+):[a-z0-9-]*:(\d{12})?:.+$")
_GCP = re.compile(r"^//(?P<svc>[a-z0-9.-]+\.googleapis\.com)/.+$")
_HOSTPATH = re.compile(r"^(?P<host>[A-Za-z0-9.-]+)/(?P<rest>.+)$")
_CF = re.compile(r"^[0-9a-f]{32}/[^/]+/(?P<otype>[a-z0-9_]+)/.+$")
_LOGICAL = {
    "jira_issue": re.compile(r"^[A-Z][A-Z0-9]+-\d+$"),
    "repo": re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"),
    "confluence_page": re.compile(r"^[A-Za-z0-9~]+/\d+$"),
}
_LOGICAL_DEFAULT = re.compile(r"^\S.*$")
# CloudFormation service segment -> ARN service, where they differ.
_AWS_SVC = {
    "elasticloadbalancingv2": "elasticloadbalancing", "networkfirewall": "network-firewall",
    "stepfunctions": "states", "apigatewayv2": "apigateway", "cognito": "cognito-idp",
    "msk": "kafka", "opensearchservice": "es", "elasticsearch": "es",
}


def _invalid(provider: str, nid: str, hint: str) -> KGError:
    return KGError("invalid_native_id", f"Invalid {provider} native_id '{nid}': expected {hint}", field="native_id")


def _mismatch(declared: str, found: str) -> KGError:
    return KGError("type_id_mismatch", f"native_id is a '{found}' but type is '{declared}'", field="type",
                   suggestions=[found])


def _azure(td: TypeDef, nid: str) -> str:
    n = nid.lower().rstrip("/")
    m = _AZ.match(n)
    if not m:
        raise _invalid("azure", nid, "a full ARM resource ID (/subscriptions/...)")
    rest = m.group("rest")
    if rest is None:
        found = "microsoft.resources/resourcegroups" if "/resourcegroups/" in n else "microsoft.resources/subscriptions"
    else:
        parts = rest.split("/")
        if len(parts) < 3 or len(parts) % 2 == 0:
            raise _invalid("azure", nid, "providers/{namespace}/{type}/{name}[/{childType}/{childName}...]")
        found = parts[0] + "/" + "/".join(parts[1::2])
    if found != td.type.lower():
        raise _mismatch(td.type, found)
    return n


def _aws(td: TypeDef, nid: str) -> str:
    m = _ARN.match(nid)
    if not m:
        raise _invalid("aws", nid, "an ARN (arn:aws:service:region:account:resource)")
    cfn_svc = td.type.split("::")[1].lower()
    expected = _AWS_SVC.get(cfn_svc, cfn_svc)
    if m.group("svc") != expected:
        raise _mismatch(td.type, f"arn service '{m.group('svc')}'")
    return nid


def _gcp(td: TypeDef, nid: str) -> str:
    m = _GCP.match(nid)
    if not m:
        raise _invalid("gcp", nid, "a full resource name (//service.googleapis.com/...)")
    if m.group("svc") != td.type.split("/")[0]:
        raise _mismatch(td.type, m.group("svc"))
    return nid


def _hostpath(provider: str, td: TypeDef, nid: str, check_segment: bool) -> str:
    m = _HOSTPATH.match(nid)
    if not m:
        raise _invalid(provider, nid, "{host_fqdn}/{path}")
    out = f"{m.group('host').lower()}/{m.group('rest')}"
    if check_segment:
        seg = m.group("rest").split("/", 1)
        if len(seg) < 2:
            raise _invalid(provider, nid, "{host_fqdn}/{object_type}/{name}")
        if seg[0].lower() != td.type.lower():
            raise _mismatch(td.type, seg[0])
    return out


def _cloudflare(td: TypeDef, nid: str) -> str:
    n = nid.lower()
    m = _CF.match(n)
    if not m:
        raise _invalid("cloudflare", nid, "{account_id}/{zone or -}/{object_type}/{id_or_name}")
    if m.group("otype") != td.type:
        raise _mismatch(td.type, m.group("otype"))
    return n


def _kubernetes(td: TypeDef, nid: str) -> str:
    parts = nid.split("/")
    if len(parts) != 5 or not all(parts):
        raise _invalid("kubernetes", nid, "{cluster}/{namespace or _cluster}/{group}/{Kind}/{name}")
    found = f"{parts[2]}/{parts[3]}"
    if found != td.type:
        raise _mismatch(td.type, found)
    return nid


def _logical(td: TypeDef, nid: str) -> str:
    if not _LOGICAL.get(td.type, _LOGICAL_DEFAULT).match(nid):
        raise _invalid("logical", nid, f"the {td.type} format")
    return nid


def normalize_native_id(provider: str, td: TypeDef, native_id: str) -> str:
    nid = (native_id or "").strip()
    if not nid:
        raise _invalid(provider, native_id, "a non-empty identifier")
    if provider == "azure":
        return _azure(td, nid)
    if provider == "aws":
        return _aws(td, nid)
    if provider == "gcp":
        return _gcp(td, nid)
    if provider == "vmware":
        return _hostpath("vmware", td, nid, check_segment=False)
    if provider == "hyperv":
        return _hostpath("hyperv", td, nid, check_segment=True)
    if provider == "cloudflare":
        return _cloudflare(td, nid)
    if provider == "kubernetes":
        return _kubernetes(td, nid)
    return _logical(td, nid)
```

- [ ] **Step 8: Run all kg tests**

Run: `pytest tests/kg -v`
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add src/memory_mcp/kg/registry.py src/memory_mcp/kg/ids.py tests/kg/test_resolve.py tests/kg/test_ids.py
git commit -m "feat(kg): type resolution and native-ID validation (RCL-33)"
```

### Task 5: `QdrantGraphStore` (RCL-34)

**Files:**
- Create: `src/memory_mcp/kg/store.py`
- Test: `tests/kg/test_store.py`

**Interfaces:**
- Consumes: `Entity`, `Edge`, `XRef`, `entity_point_id`, `edge_point_id`, `xref_point_id` (Task 2); an embedder with `.embed(str) -> list[float]` and `.dim` (Task 1)
- Produces:
  - `EntityFilter(provider=None, kind=None, type=None, memory_id=None, include_retired=False, at_ts=None)`
  - `QdrantGraphStore(client: QdrantClient, embedder)` with:
    - `ensure_graph(graph)`, `ensure_xrefs()`
    - `get_entity(graph, key) -> Entity | None`, `get_entities(graph, keys) -> list[Entity]`
    - `upsert_entity(graph, e) -> Entity`
    - `search_entities(graph, vector, filt, limit) -> list[tuple[Entity, float]]`
    - `scroll_entities(graph, filt, limit, cursor=None) -> tuple[list[Entity], str | None]`
    - `count_entities(graph, filt) -> int`
    - `edges_from(graph, keys, relations=None, at_ts=None, include_retired=False) -> list[Edge]`, and `edges_to(...)` with the same signature
    - `current_edge(graph, src, relation, dst) -> Edge | None`, `put_edge(graph, e) -> Edge`
    - `all_current_edges(graph) -> Iterator[Edge]`, `edges_with_memory(graph, memory_id) -> list[Edge]`
    - `iter_entities(graph) -> Iterator[Entity]` and `iter_edges(graph) -> Iterator[Edge]` (everything, retired included)
    - `delete_entity_hard(graph, key) -> int` (number of edges removed)
    - `put_xref(x) -> XRef`, `get_xref(src, relation, dst) -> XRef | None`, `xrefs_touching(fq) -> list[XRef]`, `xrefs_for_graph(graph) -> list[XRef]`, `mark_xrefs_dangling(fq) -> int`
  - Constants: `entities_collection(graph) -> str`, `edges_collection(graph) -> str`, `XREFS = "kg_xrefs"`, `FRONTIER_CHUNK = 500`.

- [ ] **Step 1: Write failing store tests**

`tests/kg/test_store.py`:
```python
import pytest
from memory_mcp.kg.models import Edge, Entity, XRef, iso_to_ts, now_iso
from memory_mcp.kg.store import EntityFilter, QdrantGraphStore, entities_collection, edges_collection


def ent(key, kind="container", typ="apps/Deployment", name=None, provider="kubernetes", vf="2026-01-01T00:00:00+00:00", vt=None, mem=None):
    return Entity(key=key, provider=provider, type=typ, kind=kind, native_id=key.split(":", 1)[1],
                  display_name=name or key, memory_ids=mem or [], valid_from=vf, valid_to=vt,
                  created_at=vf, updated_at=vf)


def edge(src, rel, dst, vf="2026-01-01T00:00:00+00:00", vt=None):
    return Edge(src=src, relation=rel, dst=dst, valid_from=vf, valid_to=vt, created_at=vf)


@pytest.fixture
def gs(qdrant, stub_embedder):
    s = QdrantGraphStore(qdrant, stub_embedder)
    s.ensure_graph("mcit")
    s.ensure_xrefs()
    return s


def test_ensure_is_idempotent_and_creates_collections(gs, qdrant):
    gs.ensure_graph("mcit")
    names = {c.name for c in qdrant.get_collections().collections}
    assert {entities_collection("mcit"), edges_collection("mcit"), "kg_xrefs"} <= names


def test_upsert_get_and_overwrite(gs):
    gs.upsert_entity("mcit", ent("kubernetes:a", name="alpha"))
    gs.upsert_entity("mcit", ent("kubernetes:a", name="alpha2"))
    assert gs.get_entity("mcit", "kubernetes:a").display_name == "alpha2"
    assert gs.get_entity("mcit", "kubernetes:missing") is None
    assert [e.key for e in gs.get_entities("mcit", ["kubernetes:a", "kubernetes:missing"])] == ["kubernetes:a"]


def test_search_with_kind_filter_and_retired_excluded(gs):
    gs.upsert_entity("mcit", ent("kubernetes:memory-mcp", name="memory mcp server"))
    gs.upsert_entity("mcit", ent("kubernetes:memory-svc", kind="network", typ="core/Service", name="memory mcp service"))
    gs.upsert_entity("mcit", ent("kubernetes:old", name="memory mcp old", vt="2026-02-01T00:00:00+00:00"))
    hits = gs.search_entities("mcit", gs.embedder.embed("memory mcp"), EntityFilter(kind="container"), 10)
    assert [e.key for e, _ in hits] == ["kubernetes:memory-mcp"]
    hits = gs.search_entities("mcit", gs.embedder.embed("memory mcp"), EntityFilter(kind="container", include_retired=True), 10)
    assert {e.key for e, _ in hits} == {"kubernetes:memory-mcp", "kubernetes:old"}


def test_scroll_paging_and_count(gs):
    for i in range(5):
        gs.upsert_entity("mcit", ent(f"kubernetes:e{i}"))
    page1, cur = gs.scroll_entities("mcit", EntityFilter(), 3)
    page2, cur2 = gs.scroll_entities("mcit", EntityFilter(), 3, cur)
    assert len(page1) == 3 and len(page2) == 2 and cur2 is None
    assert {e.key for e in page1 + page2} == {f"kubernetes:e{i}" for i in range(5)}
    assert gs.count_entities("mcit", EntityFilter()) == 5


def test_memory_id_filter(gs):
    gs.upsert_entity("mcit", ent("kubernetes:a", mem=["m1", "m2"]))
    gs.upsert_entity("mcit", ent("kubernetes:b", mem=["m3"]))
    page, _ = gs.scroll_entities("mcit", EntityFilter(memory_id="m2"), 10)
    assert [e.key for e in page] == ["kubernetes:a"]


def test_edges_time_filter(gs):
    gs.put_edge("mcit", edge("a", "runs_on", "ms01", vf="2026-05-01T00:00:00+00:00", vt="2026-09-27T00:00:00+00:00"))
    gs.put_edge("mcit", edge("a", "runs_on", "mcit", vf="2026-09-27T00:00:00+00:00"))
    now = [e.dst for e in gs.edges_from("mcit", ["a"])]
    past = [e.dst for e in gs.edges_from("mcit", ["a"], at_ts=iso_to_ts("2026-09-20T00:00:00+00:00"))]
    every = sorted(e.dst for e in gs.edges_from("mcit", ["a"], include_retired=True))
    assert now == ["mcit"] and past == ["ms01"] and every == ["mcit", "ms01"]
    assert [e.src for e in gs.edges_to("mcit", ["mcit"])] == ["a"]


def test_edges_relation_filter_and_large_frontier(gs):
    for i in range(620):
        gs.put_edge("mcit", edge(f"s{i}", "runs_on", "hub"))
    gs.put_edge("mcit", edge("s1", "tracked_in", "MCIT-1"))
    out = gs.edges_from("mcit", [f"s{i}" for i in range(620)], relations=["runs_on"])
    assert len(out) == 620  # frontier is chunked by FRONTIER_CHUNK
    assert len(gs.edges_to("mcit", ["hub"])) == 620


def test_current_edge_and_all_current(gs):
    gs.put_edge("mcit", edge("a", "runs_on", "b", vt="2026-02-01T00:00:00+00:00"))
    assert gs.current_edge("mcit", "a", "runs_on", "b") is None
    gs.put_edge("mcit", edge("a", "runs_on", "b", vf="2026-03-01T00:00:00+00:00"))
    assert gs.current_edge("mcit", "a", "runs_on", "b").valid_from == "2026-03-01T00:00:00+00:00"
    assert len(list(gs.all_current_edges("mcit"))) == 1
    assert len(list(gs.iter_edges("mcit"))) == 2


def test_delete_entity_hard(gs):
    gs.upsert_entity("mcit", ent("kubernetes:a"))
    gs.put_edge("mcit", edge("kubernetes:a", "runs_on", "x"))
    gs.put_edge("mcit", edge("y", "depends_on", "kubernetes:a"))
    assert gs.delete_entity_hard("mcit", "kubernetes:a") == 2
    assert gs.get_entity("mcit", "kubernetes:a") is None and list(gs.iter_edges("mcit")) == []


def test_xrefs(gs):
    x = XRef(src="vtv::logical:VTV-238", relation="pattern_from", dst="mcit::logical:MCIT-184", note="KV CSI", created_at=now_iso())
    gs.put_xref(x)
    gs.put_xref(x)  # idempotent
    assert gs.get_xref(x.src, x.relation, x.dst).note == "KV CSI"
    assert len(gs.xrefs_touching("mcit::logical:MCIT-184")) == 1
    assert len(gs.xrefs_for_graph("vtv")) == 1
    assert gs.mark_xrefs_dangling("mcit::logical:MCIT-184") == 1
    assert gs.xrefs_touching("mcit::logical:MCIT-184")[0].dangling is True
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/kg/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory_mcp.kg.store'`

- [ ] **Step 3: Implement `store.py`**

```python
"""Qdrant-backed graph storage: per-graph entity/edge collections + shared xrefs."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, FieldCondition, Filter, IsNullCondition, MatchAny, MatchValue, PayloadField,
    PayloadSchemaType, PointIdsList, PointStruct, Range, VectorParams,
)

from memory_mcp.kg.models import Edge, Entity, XRef, edge_point_id, entity_point_id, xref_point_id

XREFS = "kg_xrefs"
FRONTIER_CHUNK = 500
_SCROLL_PAGE = 1000


def entities_collection(graph: str) -> str:
    return f"kg_{graph}_entities"


def edges_collection(graph: str) -> str:
    return f"kg_{graph}_edges"


@dataclass
class EntityFilter:
    provider: str | None = None
    kind: str | None = None
    type: str | None = None
    memory_id: str | None = None
    include_retired: bool = False
    at_ts: float | None = None


def _time_conditions(at_ts: float | None, include_retired: bool) -> list:
    if include_retired:
        return []
    t = at_ts if at_ts is not None else time.time()
    return [
        FieldCondition(key="valid_from_ts", range=Range(lte=t)),
        Filter(should=[IsNullCondition(is_null=PayloadField(key="valid_to_ts")),
                       FieldCondition(key="valid_to_ts", range=Range(gt=t))]),
    ]


def _eq(key: str, value) -> FieldCondition:
    return FieldCondition(key=key, match=MatchValue(value=value))


class QdrantGraphStore:
    def __init__(self, client: QdrantClient, embedder):
        self.client = client
        self.embedder = embedder

    # ── setup ────────────────────────────────────────────────────────────────
    def _existing(self) -> set[str]:
        return {c.name for c in self.client.get_collections().collections}

    def _index(self, coll: str, keywords: list[str], floats: list[str]) -> None:
        for f in keywords:
            self.client.create_payload_index(coll, f, PayloadSchemaType.KEYWORD)
        for f in floats:
            self.client.create_payload_index(coll, f, PayloadSchemaType.FLOAT)

    def ensure_graph(self, graph: str) -> None:
        existing = self._existing()
        ents, edges = entities_collection(graph), edges_collection(graph)
        if ents not in existing:
            self.client.create_collection(ents, vectors_config=VectorParams(size=self.embedder.dim, distance=Distance.COSINE))
        if edges not in existing:
            self.client.create_collection(edges, vectors_config={})
        self._index(ents, ["key", "provider", "kind", "type", "memory_ids"], ["valid_from_ts", "valid_to_ts"])
        self._index(edges, ["src", "dst", "relation", "evidence_memory_ids"], ["valid_from_ts", "valid_to_ts"])

    def ensure_xrefs(self) -> None:
        if XREFS not in self._existing():
            self.client.create_collection(XREFS, vectors_config={})
        self._index(XREFS, ["src", "dst", "src_graph", "dst_graph"], [])

    # ── helpers ──────────────────────────────────────────────────────────────
    def _scroll_all(self, coll: str, flt: Filter | None) -> Iterator:
        offset = None
        while True:
            pts, offset = self.client.scroll(coll, scroll_filter=flt, limit=_SCROLL_PAGE, offset=offset, with_payload=True)
            yield from pts
            if offset is None:
                return

    def _entity_filter(self, f: EntityFilter) -> Filter:
        must = []
        if f.provider:
            must.append(_eq("provider", f.provider))
        if f.kind:
            must.append(_eq("kind", f.kind))
        if f.type:
            must.append(_eq("type", f.type))
        if f.memory_id:
            must.append(FieldCondition(key="memory_ids", match=MatchAny(any=[f.memory_id])))
        must += _time_conditions(f.at_ts, f.include_retired)
        return Filter(must=must)

    # ── entities ─────────────────────────────────────────────────────────────
    def get_entities(self, graph: str, keys: list[str]) -> list[Entity]:
        if not keys:
            return []
        pts = self.client.retrieve(entities_collection(graph), ids=[entity_point_id(k) for k in keys], with_payload=True)
        by_key = {p.payload["key"]: Entity.from_payload(p.payload) for p in pts}
        return [by_key[k] for k in keys if k in by_key]

    def get_entity(self, graph: str, key: str) -> Entity | None:
        found = self.get_entities(graph, [key])
        return found[0] if found else None

    def upsert_entity(self, graph: str, e: Entity) -> Entity:
        vector = self.embedder.embed(f"{e.display_name} {' '.join(e.aliases)} {e.type}")
        self.client.upsert(entities_collection(graph),
                           points=[PointStruct(id=entity_point_id(e.key), vector=vector, payload=e.to_payload())])
        return e

    def search_entities(self, graph: str, vector: list[float], filt: EntityFilter, limit: int) -> list[tuple[Entity, float]]:
        res = self.client.query_points(entities_collection(graph), query=vector, query_filter=self._entity_filter(filt),
                                       limit=limit, with_payload=True)
        return [(Entity.from_payload(p.payload), p.score) for p in res.points]

    def scroll_entities(self, graph: str, filt: EntityFilter, limit: int, cursor: str | None = None):
        pts, nxt = self.client.scroll(entities_collection(graph), scroll_filter=self._entity_filter(filt),
                                      limit=limit, offset=cursor, with_payload=True)
        return [Entity.from_payload(p.payload) for p in pts], (str(nxt) if nxt is not None else None)

    def count_entities(self, graph: str, filt: EntityFilter) -> int:
        return self.client.count(entities_collection(graph), count_filter=self._entity_filter(filt), exact=True).count

    def iter_entities(self, graph: str) -> Iterator[Entity]:
        for p in self._scroll_all(entities_collection(graph), None):
            yield Entity.from_payload(p.payload)

    # ── edges ────────────────────────────────────────────────────────────────
    def _edges_by(self, field: str, graph: str, keys: list[str], relations, at_ts, include_retired) -> list[Edge]:
        out: list[Edge] = []
        keys = list(dict.fromkeys(keys))
        for i in range(0, len(keys), FRONTIER_CHUNK):
            must = [FieldCondition(key=field, match=MatchAny(any=keys[i:i + FRONTIER_CHUNK]))]
            if relations:
                must.append(FieldCondition(key="relation", match=MatchAny(any=list(relations))))
            must += _time_conditions(at_ts, include_retired)
            out += [Edge.from_payload(p.payload) for p in self._scroll_all(edges_collection(graph), Filter(must=must))]
        return out

    def edges_from(self, graph, keys, relations=None, at_ts=None, include_retired=False) -> list[Edge]:
        return self._edges_by("src", graph, keys, relations, at_ts, include_retired)

    def edges_to(self, graph, keys, relations=None, at_ts=None, include_retired=False) -> list[Edge]:
        return self._edges_by("dst", graph, keys, relations, at_ts, include_retired)

    def current_edge(self, graph: str, src: str, relation: str, dst: str) -> Edge | None:
        flt = Filter(must=[_eq("src", src), _eq("relation", relation), _eq("dst", dst),
                           IsNullCondition(is_null=PayloadField(key="valid_to_ts"))])
        pts, _ = self.client.scroll(edges_collection(graph), scroll_filter=flt, limit=1, with_payload=True)
        return Edge.from_payload(pts[0].payload) if pts else None

    def put_edge(self, graph: str, e: Edge) -> Edge:
        pid = edge_point_id(e.src, e.relation, e.dst, e.valid_from)
        self.client.upsert(edges_collection(graph), points=[PointStruct(id=pid, vector={}, payload=e.to_payload())])
        return e

    def all_current_edges(self, graph: str) -> Iterator[Edge]:
        flt = Filter(must=[IsNullCondition(is_null=PayloadField(key="valid_to_ts"))])
        for p in self._scroll_all(edges_collection(graph), flt):
            yield Edge.from_payload(p.payload)

    def edges_with_memory(self, graph: str, memory_id: str) -> list[Edge]:
        flt = Filter(must=[FieldCondition(key="evidence_memory_ids", match=MatchAny(any=[memory_id]))])
        return [Edge.from_payload(p.payload) for p in self._scroll_all(edges_collection(graph), flt)]

    def iter_edges(self, graph: str) -> Iterator[Edge]:
        for p in self._scroll_all(edges_collection(graph), None):
            yield Edge.from_payload(p.payload)

    def delete_entity_hard(self, graph: str, key: str) -> int:
        flt = Filter(should=[_eq("src", key), _eq("dst", key)])
        ids = [p.id for p in self._scroll_all(edges_collection(graph), flt)]
        if ids:
            self.client.delete(edges_collection(graph), points_selector=PointIdsList(points=ids))
        self.client.delete(entities_collection(graph), points_selector=PointIdsList(points=[entity_point_id(key)]))
        return len(ids)

    # ── cross-graph references ───────────────────────────────────────────────
    def put_xref(self, x: XRef) -> XRef:
        self.client.upsert(XREFS, points=[PointStruct(id=xref_point_id(x.src, x.relation, x.dst), vector={}, payload=x.to_payload())])
        return x

    def get_xref(self, src: str, relation: str, dst: str) -> XRef | None:
        pts = self.client.retrieve(XREFS, ids=[xref_point_id(src, relation, dst)], with_payload=True)
        return XRef.from_payload(pts[0].payload) if pts else None

    def xrefs_touching(self, fq: str) -> list[XRef]:
        flt = Filter(should=[_eq("src", fq), _eq("dst", fq)])
        return [XRef.from_payload(p.payload) for p in self._scroll_all(XREFS, flt)]

    def xrefs_for_graph(self, graph: str) -> list[XRef]:
        flt = Filter(should=[_eq("src_graph", graph), _eq("dst_graph", graph)])
        return [XRef.from_payload(p.payload) for p in self._scroll_all(XREFS, flt)]

    def mark_xrefs_dangling(self, fq: str) -> int:
        xs = self.xrefs_touching(fq)
        for x in xs:
            self.client.set_payload(XREFS, payload={"dangling": True},
                                    points=[xref_point_id(x.src, x.relation, x.dst)])
        return len(xs)
```

- [ ] **Step 4: Run store tests**

Run: `pytest tests/kg/test_store.py -v`
Expected: 10 passed. Local mode prints "Payload indexes have no effect" warnings; that's expected.

- [ ] **Step 5: Commit**

```bash
git add src/memory_mcp/kg/store.py tests/kg/test_store.py
git commit -m "feat(kg): QdrantGraphStore with per-graph collections and xrefs (RCL-34)"
```

### Task 6: Write service: validation pipeline and writes (RCL-35)

**Files:**
- Create: `src/memory_mcp/kg/service.py`
- Modify: `tests/kg/conftest.py` (add the `kg_registry`, `gstore`, `svc` fixtures)
- Test: `tests/kg/test_service.py`

**Interfaces:**
- Consumes: `Registry.require_graph/require_provider/resolve_type/relation/check_kinds`, `normalize_native_id`, `QdrantGraphStore` (all methods from Task 5), `EntityFilter`, models (Task 2)
- Produces:
  - `KGService(registry, store, dup_threshold: float = 0.90)` with methods that return plain dicts and raise `KGError`:
    - `upsert_entity(graph, provider, type, native_id, display_name=None, aliases=None, properties=None, memory_ids=None, agent="claude-code", valid_from=None, valid_to=None) -> {"key", "status": "created"|"updated"|"revived"|"retired", "type", "kind", "possible_duplicates"?}`
    - `link(graph, src, relation, dst, properties=None, evidence_memory_ids=None, agent="claude-code", valid_from=None) -> {"src", "relation", "dst", "status": "created"|"updated", "valid_from"}`
    - `unlink(graph, src, relation, dst, reason) -> {"src", "relation", "dst", "status": "retired", "valid_to"}`
    - `retire_entity(graph, key, reason) -> {"key", "status": "retired", "edges_retired": int}`
    - `batch(graph, entities: list[dict], edges: list[dict], agent="claude-code") -> {"status": "ok"|"rejected"|"partial", "written": [...], "errors"?: [...], "failed_at"?: int, "error"?: str}`
    - `xref(src, relation, dst, note, evidence_memory_ids=None, agent="claude-code") -> {"src", "relation", "dst", "status": "created"|"updated"}`
  - module function `count_error(code: str) -> None` (increments `kg_validation_errors_total`)
  - `ENTITY_FIELDS`, `EDGE_FIELDS`: the keys accepted in batch items

**Semantics this task pins down** (spec §5.1 plus the plan amendments):
- Re-upserting a retired entity without `valid_to` **revives** it (`status: "revived"`). Its old edges stay retired.
- An upsert or batch item that sets `valid_to` records a retired entity (historical backfill).
- `link` endpoints must exist and be **current**. Historical batch edges (with `valid_to`) only require the endpoints to exist.
- Batch edge endpoints may be entities created earlier in the same batch.

- [ ] **Step 1: Add fixtures to `tests/kg/conftest.py`**

Append:
```python
from memory_mcp.kg.registry import Registry, TypeDef


@pytest.fixture
def kg_registry():
    """Packaged registry plus a few azure/cloudflare types, so tests don't depend on generated catalogs."""
    reg = Registry.load()
    for t, kind, aliases in [
        ("Microsoft.ContainerService/managedClusters", "container", ("azurerm_kubernetes_cluster",)),
        ("Microsoft.Compute/virtualMachines", "compute", ("azurerm_linux_virtual_machine",)),
        ("Microsoft.Network/networkInterfaces", "network", ()),
    ]:
        reg.types.setdefault("azure", {})[t] = TypeDef("azure", t, kind, aliases)
    for t, kind in [("dns_record", "dns"), ("zero_trust_tunnel_cloudflared", "network")]:
        reg.types.setdefault("cloudflare", {})[t] = TypeDef("cloudflare", t, kind, (f"cloudflare_{t}",))
    reg._build_type_index()
    return reg


@pytest.fixture
def gstore(qdrant, stub_embedder, kg_registry):
    from memory_mcp.kg.store import QdrantGraphStore
    s = QdrantGraphStore(qdrant, stub_embedder)
    for g in kg_registry.graphs:
        s.ensure_graph(g)
    s.ensure_xrefs()
    return s


@pytest.fixture
def svc(kg_registry, gstore):
    from memory_mcp.kg.service import KGService
    return KGService(kg_registry, gstore)
```

- [ ] **Step 2: Write failing service tests**

`tests/kg/test_service.py`:
```python
import pytest
from memory_mcp.kg.models import KGError

SUB = "/subscriptions/00000000-0000-0000-0000-000000000001"
AKS_ID = f"{SUB}/resourceGroups/rg-aks/providers/Microsoft.ContainerService/managedClusters/aks-vtv-prod"


def dep(svc, name, ns="digital-twin", graph="mcit", **kw):
    return svc.upsert_entity(graph, "kubernetes", "apps/Deployment", f"mcit-k8s/{ns}/apps/Deployment/{name}",
                             display_name=name, **kw)


def node(svc, name="opi-5", graph="mcit"):
    return svc.upsert_entity(graph, "kubernetes", "core/Node", f"mcit-k8s/_cluster/core/Node/{name}", display_name=name)


def jira(svc, key, graph="mcit"):
    return svc.upsert_entity(graph, "logical", "jira_issue", key)


def test_upsert_create_then_update_merges(svc, gstore):
    r = dep(svc, "memory-mcp", aliases=["memory twin"], properties={"replicas": 1, "tier": "prod"}, memory_ids=["m1"])
    assert r["status"] == "created" and r["kind"] == "container"
    r2 = dep(svc, "memory-mcp", aliases=["memory-twin prod"], properties={"replicas": 2, "tier": None}, memory_ids=["m2"])
    assert r2["status"] == "updated" and r2["key"] == r["key"]
    e = gstore.get_entity("mcit", r["key"])
    assert e.aliases == ["memory twin", "memory-twin prod"]
    assert e.properties == {"replicas": 2}
    assert e.memory_ids == ["m1", "m2"]


def test_upsert_same_arm_id_different_case_updates(svc):
    a = svc.upsert_entity("vtv", "azure", "azurerm_kubernetes_cluster", AKS_ID, display_name="aks-vtv-prod")
    b = svc.upsert_entity("vtv", "azure", "Microsoft.ContainerService/managedClusters", AKS_ID.upper() + "/",
                          display_name="aks-vtv-prod")
    assert a["status"] == "created" and b["status"] == "updated" and a["key"] == b["key"]


def test_upsert_errors(svc):
    with pytest.raises(KGError) as e:
        svc.upsert_entity("nope", "kubernetes", "apps/Deployment", "c/n/apps/Deployment/x")
    assert e.value.code == "unknown_graph"
    with pytest.raises(KGError) as e:
        svc.upsert_entity("mcit", "kubernetes", "apps/Deploymnet", "c/n/apps/Deployment/x")
    assert e.value.code == "unknown_type"
    with pytest.raises(KGError) as e:
        svc.upsert_entity("vtv", "azure", "Microsoft.Compute/virtualMachines",
                          f"{SUB}/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic1")
    assert e.value.code == "type_id_mismatch"


def test_duplicate_hint_on_create(svc):
    dep(svc, "memory-mcp")
    r = dep(svc, "memory-mcp", ns="digital-twin-dev")
    assert r["status"] == "created"
    assert [d["key"] for d in r["possible_duplicates"]] == ["kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"]


def test_link_create_update_and_constraints(svc, gstore):
    d, n, j = dep(svc, "memory-mcp")["key"], node(svc)["key"], jira(svc, "MCIT-193")["key"]
    assert svc.link("mcit", d, "runs_on", n)["status"] == "created"
    assert svc.link("mcit", d, "runs_on", n, evidence_memory_ids=["m9"])["status"] == "updated"
    assert len(list(gstore.iter_edges("mcit"))) == 1
    assert gstore.current_edge("mcit", d, "runs_on", n).evidence_memory_ids == ["m9"]
    svc.link("mcit", d, "tracked_in", j)
    with pytest.raises(KGError) as e:
        svc.link("mcit", d, "tracked_in", n)  # tracked_in target must be kind 'work'
    assert e.value.code == "kind_constraint"
    with pytest.raises(KGError) as e:
        svc.link("mcit", d, "pattern_from", j)
    assert e.value.code == "relation_class_mismatch"
    with pytest.raises(KGError) as e:
        svc.link("mcit", d, "runs_on", "vtv::" + n)
    assert e.value.code == "cross_graph_edge"
    with pytest.raises(KGError) as e:
        svc.link("mcit", d, "runs_on", "kubernetes:missing")
    assert e.value.code == "endpoint_not_found" and e.value.field == "dst"


def test_link_to_retired_entity_rejected(svc):
    d, n = dep(svc, "memory-mcp")["key"], node(svc, "ms01-k8s-wkr-01")["key"]
    svc.retire_entity("mcit", n, "decommissioned MCIT-251")
    with pytest.raises(KGError) as e:
        svc.link("mcit", d, "runs_on", n)
    assert e.value.code == "endpoint_not_found"


def test_unlink_then_relink_keeps_history(svc, gstore):
    d, n = dep(svc, "memory-mcp")["key"], node(svc)["key"]
    svc.link("mcit", d, "runs_on", n, valid_from="2026-09-01T00:00:00+00:00")
    assert svc.unlink("mcit", d, "runs_on", n, "moved")["status"] == "retired"
    svc.link("mcit", d, "runs_on", n)
    edges = list(gstore.iter_edges("mcit"))
    assert len(edges) == 2 and sum(e.valid_to is None for e in edges) == 1
    assert [e.retire_reason for e in edges if e.valid_to] == ["moved"]
    with pytest.raises(KGError) as e:
        svc.unlink("mcit", d, "depends_on", n, "x")
    assert e.value.code == "not_found"


def test_retire_entity_cascades_both_directions(svc, gstore):
    d, n, j = dep(svc, "memory-mcp")["key"], node(svc)["key"], jira(svc, "MCIT-193")["key"]
    svc.link("mcit", d, "runs_on", n)
    svc.link("mcit", d, "tracked_in", j)
    r = svc.retire_entity("mcit", n, "node rebuilt")
    assert r == {"key": n, "status": "retired", "edges_retired": 1}
    assert gstore.get_entity("mcit", n).valid_to is not None
    assert [e.relation for e in gstore.all_current_edges("mcit")] == ["tracked_in"]
    with pytest.raises(KGError) as e:
        svc.retire_entity("mcit", n, "again")
    assert e.value.code == "not_found"


def test_revive_retired_entity(svc, gstore):
    n = node(svc)["key"]
    svc.retire_entity("mcit", n, "x")
    assert node(svc)["status"] == "revived"
    assert gstore.get_entity("mcit", n).valid_to is None


BATCH_ENTS = [
    {"provider": "kubernetes", "type": "apps/Deployment", "native_id": "mcit-k8s/digital-twin/apps/Deployment/memory-mcp", "display_name": "memory-mcp"},
    {"provider": "kubernetes", "type": "core/Node", "native_id": "mcit-k8s/_cluster/core/Node/opi-5", "display_name": "opi-5"},
    {"provider": "kubernetes", "type": "core/Node", "native_id": "ms01-k8s/_cluster/core/Node/ms01-k8s-wkr-01",
     "display_name": "ms01-k8s-wkr-01", "valid_from": "2026-04-01T00:00:00+00:00", "valid_to": "2026-09-27T07:00:00+00:00"},
]
DEP = "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"
BATCH_EDGES = [
    {"src": DEP, "relation": "runs_on", "dst": "kubernetes:mcit-k8s/_cluster/core/Node/opi-5", "valid_from": "2026-09-27T07:00:00+00:00"},
    {"src": DEP, "relation": "runs_on", "dst": "kubernetes:ms01-k8s/_cluster/core/Node/ms01-k8s-wkr-01",
     "valid_from": "2026-04-01T00:00:00+00:00", "valid_to": "2026-09-27T07:00:00+00:00", "retire_reason": "MCIT-193 cutover"},
]


def test_batch_same_batch_endpoints_and_idempotent(svc, gstore):
    r = svc.batch("mcit", BATCH_ENTS, BATCH_EDGES)
    assert r["status"] == "ok"
    assert [w["status"] for w in r["written"]] == ["created", "created", "retired", "created", "recorded"]
    n_edges = len(list(gstore.iter_edges("mcit")))
    r2 = svc.batch("mcit", BATCH_ENTS, BATCH_EDGES)
    assert [w["status"] for w in r2["written"]] == ["updated", "updated", "retired", "updated", "recorded"]
    assert len(list(gstore.iter_edges("mcit"))) == n_edges == 2


def test_batch_rejects_everything_on_any_error(svc, gstore):
    bad_ents = BATCH_ENTS[:2] + [{"provider": "kubernetes", "type": "apps/Deploymnet", "native_id": "c/n/apps/Deployment/x"}]
    bad_edges = [{"src": DEP, "relation": "run_on", "dst": "kubernetes:mcit-k8s/_cluster/core/Node/opi-5"}]
    r = svc.batch("mcit", bad_ents, bad_edges)
    assert r["status"] == "rejected" and r["written"] == []
    assert {(e["item"], e["error"]) for e in r["errors"]} == {("entities[2]", "unknown_type"), ("edges[0]", "unknown_relation")}
    assert list(gstore.iter_entities("mcit")) == []


def test_xref(svc):
    jira(svc, "MCIT-184")
    svc.upsert_entity("vtv", "logical", "jira_issue", "VTV-238")
    r = svc.xref("vtv::logical:VTV-238", "pattern_from", "mcit::logical:MCIT-184", "Key Vault CSI pattern")
    assert r["status"] == "created"
    assert svc.xref("vtv::logical:VTV-238", "pattern_from", "mcit::logical:MCIT-184", "updated note")["status"] == "updated"
    with pytest.raises(KGError) as e:
        svc.xref("vtv::logical:VTV-238", "runs_on", "mcit::logical:MCIT-184", "x")
    assert e.value.code == "relation_class_mismatch"
    with pytest.raises(KGError) as e:
        svc.xref("vtv::logical:VTV-999", "pattern_from", "mcit::logical:MCIT-184", "x")
    assert e.value.code == "endpoint_not_found"
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/kg/test_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory_mcp.kg.service'`

- [ ] **Step 4: Implement `service.py`**

```python
"""Validated writes to the knowledge graph."""
from __future__ import annotations

from dataclasses import replace

from opentelemetry import metrics

from memory_mcp.kg.ids import normalize_native_id
from memory_mcp.kg.models import (
    Edge, Entity, KGError, XRef, iso_to_ts, make_key, now_iso, split_fq,
)
from memory_mcp.kg.store import EntityFilter

ENTITY_FIELDS = ("provider", "type", "native_id", "display_name", "aliases", "properties", "memory_ids",
                 "valid_from", "valid_to")
EDGE_FIELDS = ("src", "relation", "dst", "properties", "evidence_memory_ids", "valid_from", "valid_to", "retire_reason")

_meter = metrics.get_meter("memory_mcp.kg")
_writes = _meter.create_counter("kg_writes_total", description="Knowledge graph writes")
_errors = _meter.create_counter("kg_validation_errors_total", description="Knowledge graph validation errors")


def count_error(code: str) -> None:
    _errors.add(1, {"code": code})


def _union(a: list, b: list | None) -> list:
    return list(dict.fromkeys([*a, *(b or [])]))


def _check_iso(value: str | None, field: str) -> None:
    if value is None:
        return
    try:
        iso_to_ts(value)
    except (TypeError, ValueError):
        raise KGError("invalid_native_id", f"'{field}' must be an ISO-8601 timestamp, got {value!r}", field=field)


class KGService:
    def __init__(self, registry, store, dup_threshold: float = 0.90):
        self.reg = registry
        self.store = store
        self.dup_threshold = dup_threshold

    # ── entities ─────────────────────────────────────────────────────────────
    def _prepare_entity(self, graph, provider, type, native_id, display_name=None, aliases=None, properties=None,
                        memory_ids=None, agent="claude-code", valid_from=None, valid_to=None) -> Entity:
        self.reg.require_graph(graph)
        self.reg.require_provider(provider)
        td = self.reg.resolve_type(provider, type)
        nid = normalize_native_id(provider, td, native_id)
        _check_iso(valid_from, "valid_from")
        _check_iso(valid_to, "valid_to")
        now = now_iso()
        return Entity(key=make_key(provider, nid), provider=provider, type=td.type, kind=td.kind, native_id=nid,
                      display_name=display_name or nid, aliases=_union([], aliases), properties=dict(properties or {}),
                      memory_ids=_union([], memory_ids), agent=agent, created_at=now, updated_at=now,
                      valid_from=valid_from or now, valid_to=valid_to)

    def _write_entity(self, graph: str, new: Entity) -> tuple[Entity, str]:
        old = self.store.get_entity(graph, new.key)
        if old is None:
            status = "retired" if new.valid_to else "created"
            return self.store.upsert_entity(graph, new), status
        props = dict(old.properties)
        for k, v in new.properties.items():
            if v is None:
                props.pop(k, None)
            else:
                props[k] = v
        if new.valid_to:
            status, valid_to = "retired", new.valid_to
        elif old.valid_to:
            status, valid_to = "revived", None
        else:
            status, valid_to = "updated", None
        merged = replace(old, type=new.type, kind=new.kind, display_name=new.display_name,
                         aliases=_union(old.aliases, new.aliases), properties=props,
                         memory_ids=_union(old.memory_ids, new.memory_ids), agent=new.agent,
                         updated_at=new.updated_at, valid_to=valid_to)
        return self.store.upsert_entity(graph, merged), status

    def _duplicates(self, graph: str, e: Entity) -> list[dict]:
        vec = self.store.embedder.embed(f"{e.display_name} {' '.join(e.aliases)} {e.type}")
        hits = self.store.search_entities(graph, vec, EntityFilter(kind=e.kind), 5)
        return [{**h.compact(), "score": round(s, 3)} for h, s in hits if h.key != e.key and s >= self.dup_threshold]

    def upsert_entity(self, graph, provider, type, native_id, display_name=None, aliases=None, properties=None,
                      memory_ids=None, agent="claude-code", valid_from=None, valid_to=None) -> dict:
        new = self._prepare_entity(graph, provider, type, native_id, display_name, aliases, properties,
                                   memory_ids, agent, valid_from, valid_to)
        ent, status = self._write_entity(graph, new)
        _writes.add(1, {"graph": graph, "op": "upsert_entity"})
        out = {"key": ent.key, "status": status, "type": ent.type, "kind": ent.kind}
        if status == "created":
            dups = self._duplicates(graph, ent)
            if dups:
                out["possible_duplicates"] = dups
        return out

    def _require_entity(self, graph: str, key: str, field: str, current: bool, pending: dict | None = None) -> Entity:
        e = (pending or {}).get(key) or self.store.get_entity(graph, key)
        if e is None or (current and e.valid_to is not None):
            state = "does not exist" if e is None else "is retired"
            raise KGError("endpoint_not_found", f"{field} '{key}' {state} in graph '{graph}'", field=field,
                          suggestions=["kg_resolve to find the right key"])
        return e

    # ── edges ────────────────────────────────────────────────────────────────
    def _check_edge(self, graph, src, relation, dst, current=True, pending=None):
        if "::" in src or "::" in dst:
            raise KGError("cross_graph_edge", "Edges must stay within one graph; use kg_xref to reference another graph",
                          field="dst" if "::" in dst else "src")
        rel = self.reg.relation(relation)
        if rel.cls != "topology":
            raise KGError("relation_class_mismatch", f"'{relation}' is a reference relation; use kg_xref", field="relation")
        s = self._require_entity(graph, src, "src", current, pending)
        d = self._require_entity(graph, dst, "dst", current, pending)
        self.reg.check_kinds(rel, s.kind, d.kind)
        return rel

    def _write_edge(self, graph, src, relation, dst, properties=None, evidence_memory_ids=None, agent="claude-code",
                    valid_from=None, valid_to=None, retire_reason=None) -> dict:
        now = now_iso()
        if valid_to:  # historical record
            e = Edge(src=src, relation=relation, dst=dst, properties=dict(properties or {}),
                     evidence_memory_ids=_union([], evidence_memory_ids), agent=agent, created_at=now,
                     valid_from=valid_from or now, valid_to=valid_to, retire_reason=retire_reason)
            self.store.put_edge(graph, e)
            return {"src": src, "relation": relation, "dst": dst, "status": "recorded", "valid_from": e.valid_from}
        cur = self.store.current_edge(graph, src, relation, dst)
        if cur:
            e = replace(cur, properties={**cur.properties, **(properties or {})},
                        evidence_memory_ids=_union(cur.evidence_memory_ids, evidence_memory_ids), agent=agent)
            status = "updated"
        else:
            e = Edge(src=src, relation=relation, dst=dst, properties=dict(properties or {}),
                     evidence_memory_ids=_union([], evidence_memory_ids), agent=agent, created_at=now,
                     valid_from=valid_from or now)
            status = "created"
        self.store.put_edge(graph, e)
        return {"src": src, "relation": relation, "dst": dst, "status": status, "valid_from": e.valid_from}

    def link(self, graph, src, relation, dst, properties=None, evidence_memory_ids=None, agent="claude-code",
             valid_from=None) -> dict:
        self.reg.require_graph(graph)
        _check_iso(valid_from, "valid_from")
        self._check_edge(graph, src, relation, dst)
        _writes.add(1, {"graph": graph, "op": "link"})
        return self._write_edge(graph, src, relation, dst, properties, evidence_memory_ids, agent, valid_from)

    def unlink(self, graph, src, relation, dst, reason) -> dict:
        self.reg.require_graph(graph)
        cur = self.store.current_edge(graph, src, relation, dst)
        if cur is None:
            raise KGError("not_found", f"No current edge {src} -{relation}-> {dst}", field="relation")
        now = now_iso()
        self.store.put_edge(graph, replace(cur, valid_to=now, retire_reason=reason))
        _writes.add(1, {"graph": graph, "op": "unlink"})
        return {"src": src, "relation": relation, "dst": dst, "status": "retired", "valid_to": now}

    def retire_entity(self, graph, key, reason) -> dict:
        self.reg.require_graph(graph)
        e = self.store.get_entity(graph, key)
        if e is None or e.valid_to is not None:
            raise KGError("not_found", f"No current entity '{key}' in graph '{graph}'", field="key")
        now = now_iso()
        edges = self.store.edges_from(graph, [key]) + self.store.edges_to(graph, [key])
        for ed in edges:
            self.store.put_edge(graph, replace(ed, valid_to=now, retire_reason=reason))
        self.store.upsert_entity(graph, replace(e, valid_to=now, updated_at=now))
        _writes.add(1, {"graph": graph, "op": "retire_entity"})
        return {"key": key, "status": "retired", "edges_retired": len(edges)}

    # ── batch ────────────────────────────────────────────────────────────────
    def batch(self, graph, entities, edges, agent="claude-code") -> dict:
        self.reg.require_graph(graph)
        errors, prepared = [], []
        for i, item in enumerate(entities or []):
            try:
                prepared.append(self._prepare_entity(graph, agent=item.get("agent", agent),
                                                     **{k: item.get(k) for k in ENTITY_FIELDS}))
            except KGError as ex:
                errors.append({"item": f"entities[{i}]", **ex.to_dict()})
            except TypeError as ex:
                errors.append({"item": f"entities[{i}]", "error": "invalid_native_id", "message": str(ex),
                               "field": None, "suggestions": []})
        pending = {e.key: e for e in prepared}
        for i, item in enumerate(edges or []):
            try:
                _check_iso(item.get("valid_from"), "valid_from")
                _check_iso(item.get("valid_to"), "valid_to")
                self._check_edge(graph, item["src"], item["relation"], item["dst"],
                                 current=not item.get("valid_to"), pending=pending)
            except KGError as ex:
                errors.append({"item": f"edges[{i}]", **ex.to_dict()})
            except KeyError as ex:
                errors.append({"item": f"edges[{i}]", "error": "endpoint_not_found", "message": f"missing {ex}",
                               "field": str(ex).strip("'"), "suggestions": []})
        if errors:
            for er in errors:
                count_error(er["error"])
            return {"status": "rejected", "written": [], "errors": errors}
        written = []
        try:
            for e in prepared:
                ent, status = self._write_entity(graph, e)
                written.append({"key": ent.key, "status": status})
            for item in edges or []:
                written.append(self._write_edge(graph, agent=item.get("agent", agent),
                                                **{k: item.get(k) for k in EDGE_FIELDS}))
        except Exception as ex:  # infrastructure failure mid-write; retry is safe (deterministic ids)
            return {"status": "partial", "written": written, "failed_at": len(written), "error": str(ex)}
        _writes.add(len(written), {"graph": graph, "op": "batch"})
        return {"status": "ok", "written": written}

    # ── cross-graph references ───────────────────────────────────────────────
    def xref(self, src, relation, dst, note, evidence_memory_ids=None, agent="claude-code") -> dict:
        rel = self.reg.relation(relation)
        if rel.cls != "reference":
            raise KGError("relation_class_mismatch", f"'{relation}' is a topology relation; use kg_link", field="relation")
        for fq, field in ((src, "src"), (dst, "dst")):
            g, k = split_fq(fq)
            self.reg.require_graph(g)
            self._require_entity(g, k, field, current=False)
        old = self.store.get_xref(src, relation, dst)
        x = XRef(src=src, relation=relation, dst=dst, note=note,
                 evidence_memory_ids=_union(old.evidence_memory_ids if old else [], evidence_memory_ids),
                 agent=agent, created_at=old.created_at if old else now_iso())
        self.store.put_xref(x)
        _writes.add(1, {"graph": src.split("::", 1)[0], "op": "xref"})
        return {"src": src, "relation": relation, "dst": dst, "status": "updated" if old else "created"}
```

- [ ] **Step 5: Run service tests**

Run: `pytest tests/kg/test_service.py -v`
Expected: 12 passed

- [ ] **Step 6: Run all kg tests and commit**

Run: `pytest tests/kg -v`
Expected: all pass

```bash
git add src/memory_mcp/kg/service.py tests/kg/conftest.py tests/kg/test_service.py
git commit -m "feat(kg): validated write service with batch, retire, xref (RCL-35)"
```

### Task 7: Query engine (RCL-36)

**Files:**
- Create: `src/memory_mcp/kg/query.py`
- Test: `tests/kg/test_query.py`

**Interfaces:**
- Consumes: `Registry.graph_ids/require_graph/relation/inverse_label/propagating_relations`, `QdrantGraphStore` (Task 5), `KGService` for test setup only (Task 6)
- Produces: `KGQuery(registry, store, memory_lookup=None, max_chars=6000)`. `memory_lookup: Callable[[str], dict | None]` returns `{"id", "name", "type"}`. Methods return dicts and raise `KGError`:
  - `resolve(query, graphs="*", provider=None, kind=None, type=None, include_retired=False, limit=10) -> {"results": [...]}`
  - `get_entity(graph, key, as_of=None, include_history=False) -> {"entity", "out", "in", "memories", "xrefs", "history"?}`
  - `find(graph, provider=None, kind=None, type=None, missing_relation=None, direction="out", include_retired=False, limit=100, cursor=None) -> {"items", "has_more", "cursor"}`
  - `traverse(graph, start, direction="both", relations=None, kinds=None, max_depth=3, as_of=None, follow_xrefs=False, limit=200, max_chars=None) -> {"nodes", "edges", "has_more", "truncated", "xrefs"?}`
  - `path(graph, src, dst, relations=None, max_depth=6) -> {"found", "paths": [{"nodes", "edges"}]}`
  - `impact(graph, key, max_depth=6, as_of=None) -> {"root", "affected": [{"depth", "nodes"}], "tracked_in", "documented_in"}`
  - `overview(graphs="*") -> {"graphs": [...]}`
  - `related_across(graph, key, target_graphs="*", limit=10) -> {"xrefs", "similar"}`
  - `for_memory(memory_id, graphs="*") -> {"entities", "edges"}`
  - module function `fit(result, max_chars, list_keys) -> dict`
  - constants `MAX_DEPTH = 6`, `LESSON_MEMORY_TYPES = ("decision", "troubleshooting", "runbook", "architecture")`

- [ ] **Step 1: Write failing query tests**

`tests/kg/test_query.py`:
```python
import json

import pytest
from memory_mcp.kg.models import KGError
from memory_mcp.kg.query import KGQuery, fit

ACCT = "0123456789abcdef0123456789abcdef"
K = "kubernetes:mcit-k8s"
DNS = f"cloudflare:{ACCT}/chriscastrotech.com/dns_record/memory-mcp"
TUN = f"cloudflare:{ACCT}/-/zero_trust_tunnel_cloudflared/homelab"
GW = f"{K}/envoy-gateway-system/gateway.networking.k8s.io/Gateway/eg"
RT = f"{K}/digital-twin/gateway.networking.k8s.io/HTTPRoute/memory-mcp"
SVC = f"{K}/digital-twin/core/Service/memory-mcp"
DEP = f"{K}/digital-twin/apps/Deployment/memory-mcp"
OPI = f"{K}/_cluster/core/Node/opi-5"
MS01 = "kubernetes:ms01-k8s/_cluster/core/Node/ms01-k8s-wkr-01"
JIRA = "logical:MCIT-193"
CUT = "2026-09-27T07:00:00+00:00"


def ent(key, typ, name, **kw):
    provider, nid = key.split(":", 1)
    return {"provider": provider, "type": typ, "native_id": nid, "display_name": name, **kw}


@pytest.fixture
def topo(svc):
    ents = [
        ent(DNS, "dns_record", "memory-mcp.chriscastrotech.com"),
        ent(TUN, "zero_trust_tunnel_cloudflared", "homelab tunnel"),
        ent(GW, "gateway.networking.k8s.io/Gateway", "envoy gateway eg"),
        ent(RT, "gateway.networking.k8s.io/HTTPRoute", "memory-mcp route"),
        ent(SVC, "core/Service", "memory-mcp service"),
        ent(DEP, "apps/Deployment", "memory-mcp", aliases=["memory twin prod"], memory_ids=["mem-1"]),
        ent(OPI, "core/Node", "opi-5"),
        ent(MS01, "core/Node", "ms01-k8s-wkr-01", valid_from="2026-04-01T00:00:00+00:00", valid_to=CUT),
        ent(JIRA, "jira_issue", "MCIT-193"),
    ]
    edges = [
        {"src": DNS, "relation": "resolves_to", "dst": TUN},
        {"src": TUN, "relation": "routes_to", "dst": GW},
        {"src": GW, "relation": "routes_to", "dst": RT},
        {"src": RT, "relation": "routes_to", "dst": SVC},
        {"src": SVC, "relation": "routes_to", "dst": DEP},
        {"src": DEP, "relation": "runs_on", "dst": OPI, "valid_from": CUT, "evidence_memory_ids": ["mem-2"]},
        {"src": DEP, "relation": "runs_on", "dst": MS01, "valid_from": "2026-04-01T00:00:00+00:00", "valid_to": CUT,
         "retire_reason": "MCIT-193 cutover"},
        {"src": DEP, "relation": "tracked_in", "dst": JIRA},
    ]
    assert svc.batch("mcit", ents, edges)["status"] == "ok"
    return svc


MEMS = {"mem-1": {"id": "mem-1", "name": "memory-mcp-deployment", "type": "runbook"},
        "mem-2": {"id": "mem-2", "name": "MCIT-193 cutover", "type": "decision"}}


@pytest.fixture
def q(kg_registry, gstore, topo):
    return KGQuery(kg_registry, gstore, memory_lookup=MEMS.get)


def keys(nodes):
    return {n["key"] for n in nodes}


def test_resolve_exact_alias_first_and_graph_label(q):
    r = q.resolve("memory twin prod")["results"]
    assert r[0]["key"] == DEP and r[0]["score"] == 1.0 and r[0]["graph"] == "mcit"
    assert q.resolve(DEP, graphs="mcit")["results"][0]["key"] == DEP


def test_resolve_excludes_retired_unless_asked(q):
    assert MS01 not in keys(q.resolve("ms01-k8s-wkr-01", kind="host")["results"])
    hit = [x for x in q.resolve("ms01-k8s-wkr-01", kind="host", include_retired=True)["results"] if x["key"] == MS01]
    assert hit and hit[0]["retired"] is True


def test_get_entity_groups_edges_with_inverse_labels(q):
    r = q.get_entity("mcit", DEP)
    assert r["entity"]["key"] == DEP
    assert keys(r["out"]["runs_on"]) == {OPI} and keys(r["out"]["tracked_in"]) == {JIRA}
    assert keys(r["in"]["routed_from"]) == {SVC}
    assert r["memories"] == [MEMS["mem-1"]]
    past = q.get_entity("mcit", DEP, as_of="2026-09-20T00:00:00+00:00")
    assert keys(past["out"]["runs_on"]) == {MS01}
    hist = q.get_entity("mcit", DEP, include_history=True)["history"]
    assert any(h["event"] == "close" and h["dst"] == MS01 and h["reason"] == "MCIT-193 cutover" for h in hist)
    with pytest.raises(KGError) as e:
        q.get_entity("mcit", "kubernetes:nope")
    assert e.value.code == "not_found"


def test_find_paging_and_orphans(q):
    first = q.find("mcit", provider="kubernetes", limit=2)
    assert len(first["items"]) == 2 and first["has_more"] is True
    rest = q.find("mcit", provider="kubernetes", limit=50, cursor=first["cursor"])
    assert rest["has_more"] is False
    assert len(first["items"]) + len(rest["items"]) == 5  # GW, RT, SVC, DEP, OPI (MS01 retired)
    orphans = q.find("mcit", provider="kubernetes", kind="network", missing_relation="tracked_in")
    assert keys(orphans["items"]) == {GW, RT, SVC}
    assert q.find("mcit", kind="container", missing_relation="tracked_in")["items"] == []  # DEP is tracked


def test_traverse_depth_kinds_and_as_of(q):
    r = q.traverse("mcit", DEP, max_depth=1)
    assert keys(r["nodes"]) == {DEP, SVC, OPI, JIRA}
    assert {n["depth"] for n in r["nodes"] if n["key"] == DEP} == {0}
    only_net = q.traverse("mcit", DEP, max_depth=6, kinds=["network", "dns"])
    assert keys(only_net["nodes"]) == {DEP, SVC, RT, GW, TUN, DNS}
    past = q.traverse("mcit", DEP, max_depth=1, direction="out", as_of="2026-09-20T00:00:00+00:00")
    assert MS01 in keys(past["nodes"]) and OPI not in keys(past["nodes"])


def test_traverse_cycle_terminates_without_duplicates(svc, kg_registry, gstore):
    a = svc.upsert_entity("mcit", "logical", "workload", "wa", display_name="a")["key"]
    b = svc.upsert_entity("mcit", "logical", "workload", "wb", display_name="b")["key"]
    c = svc.upsert_entity("mcit", "logical", "workload", "wc", display_name="c")["key"]
    svc.link("mcit", a, "depends_on", b); svc.link("mcit", b, "depends_on", c); svc.link("mcit", c, "depends_on", a)
    svc.link("mcit", a, "peers_with", b); svc.link("mcit", b, "peers_with", a)
    r = KGQuery(kg_registry, gstore).traverse("mcit", a, max_depth=6)
    assert sorted(n["key"] for n in r["nodes"]) == sorted([a, b, c])
    assert len(r["edges"]) == len({(e["src"], e["relation"], e["dst"]) for e in r["edges"]}) == 5


def test_traverse_truncates_to_budget_valid_json(svc, kg_registry, gstore):
    hub = svc.upsert_entity("mcit", "logical", "workload", "hub", display_name="hub")["key"]
    ents = [{"provider": "logical", "type": "workload", "native_id": f"w{i:03d}", "display_name": f"worker {i:03d} with a long name"} for i in range(300)]
    edges = [{"src": f"logical:w{i:03d}", "relation": "depends_on", "dst": hub} for i in range(300)]
    assert svc.batch("mcit", ents, edges)["status"] == "ok"
    r = KGQuery(kg_registry, gstore).traverse("mcit", hub, max_depth=1, max_chars=2000)
    text = json.dumps(r)
    assert len(text) <= 2000 and json.loads(text)["truncated"] is True
    assert r["nodes"][0]["key"] == hub
    node_keys = keys(r["nodes"])
    assert all(e["src"] in node_keys and e["dst"] in node_keys for e in r["edges"])


def test_path(q):
    r = q.path("mcit", DNS, OPI)
    assert r["found"] and [n["key"] for n in r["paths"][0]["nodes"]] == [DNS, TUN, GW, RT, SVC, DEP, OPI]
    assert len(r["paths"]) <= 3
    assert q.path("mcit", DNS, OPI, max_depth=3)["found"] is False


def test_impact_reaches_public_hostname_and_reports_tickets(q):
    r = q.impact("mcit", OPI)
    affected = {n["key"]: d["depth"] for d in r["affected"] for n in d["nodes"]}
    assert affected[DEP] == 1 and affected[DNS] == 6
    assert keys(r["tracked_in"]) == {JIRA}
    assert q.impact("mcit", MS01)["affected"] == []  # only retired edges point at it


def test_impact_cycle_terminates(svc, kg_registry, gstore):
    a = svc.upsert_entity("mcit", "logical", "workload", "ca", display_name="a")["key"]
    b = svc.upsert_entity("mcit", "logical", "workload", "cb", display_name="b")["key"]
    svc.link("mcit", a, "depends_on", b); svc.link("mcit", b, "depends_on", a)
    r = KGQuery(kg_registry, gstore).impact("mcit", a)
    assert [n["key"] for d in r["affected"] for n in d["nodes"]] == [b]


def test_overview(q):
    g = q.overview("mcit")["graphs"][0]
    assert g["graph"] == "mcit" and g["entities"] == 8
    assert g["by_kind"] == {"dns": 1, "network": 4, "container": 1, "host": 1, "work": 1}
    assert g["by_provider"]["cloudflare"] == 2
    assert g["hubs"][0]["key"] == DEP and g["hubs"][0]["degree"] == 3
    assert DEP in keys(g["recent"]) and g["dangling_xrefs"] == 0


def test_related_across_and_for_memory(svc, q):
    svc.upsert_entity("mcit", "logical", "jira_issue", "MCIT-184")
    svc.upsert_entity("vtv", "logical", "jira_issue", "VTV-238")
    svc.upsert_entity("vtv", "kubernetes", "apps/Deployment", "aks-vtv-prod/cst/apps/Deployment/memory-mcp",
                      display_name="memory-mcp")
    svc.xref("vtv::logical:VTV-238", "pattern_from", "mcit::logical:MCIT-184", "Key Vault CSI pattern")
    r = q.related_across("vtv", "logical:VTV-238")
    assert r["xrefs"][0]["dst"] == "mcit::logical:MCIT-184"
    sim = q.related_across("vtv", "kubernetes:aks-vtv-prod/cst/apps/Deployment/memory-mcp")["similar"]
    assert sim[0]["graph"] == "mcit" and sim[0]["key"] == DEP and sim[0]["memories"] == [MEMS["mem-1"]]
    fm = q.for_memory("mem-2")
    assert fm["entities"] == [] and fm["edges"][0]["dst"] == OPI and fm["edges"][0]["graph"] == "mcit"
    assert keys(q.for_memory("mem-1")["entities"]) == {DEP}


def test_fit_trims_lists_and_stays_valid():
    big = {"items": [{"k": "x" * 50} for _ in range(100)]}
    out = fit(big, 500, ("items",))
    assert out["truncated"] is True and len(json.dumps(out)) <= 500
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/kg/test_query.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory_mcp.kg.query'`

- [ ] **Step 3: Implement `query.py`**

```python
"""Read-side knowledge-graph queries and graph algorithms."""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable

from memory_mcp.kg.models import Edge, Entity, KGError, fq_key, iso_to_ts
from memory_mcp.kg.store import EntityFilter

MAX_DEPTH = 6
LESSON_MEMORY_TYPES = ("decision", "troubleshooting", "runbook", "architecture")
_HUB_TTL = 60.0


def _size(obj) -> int:
    return len(json.dumps(obj, default=str))


def fit(result: dict, max_chars: int, list_keys: tuple[str, ...]) -> dict:
    """Trim the named lists from the tail until the JSON fits; mark truncated."""
    if _size(result) <= max_chars:
        return result
    result["truncated"] = True
    for k in list_keys:
        lst = result.get(k)
        while isinstance(lst, list) and lst and _size(result) > max_chars:
            lst.pop()
    return result


def _edge_dict(e: Edge, graph: str | None = None) -> dict:
    d = {"src": e.src, "relation": e.relation, "dst": e.dst}
    if graph:
        d = {"graph": graph, **d}
    if e.valid_to:
        d["valid_to"] = e.valid_to
    return d


class KGQuery:
    def __init__(self, registry, store, memory_lookup: Callable[[str], dict | None] | None = None,
                 max_chars: int = 6000):
        self.reg = registry
        self.store = store
        self.memory_lookup = memory_lookup or (lambda _id: None)
        self.max_chars = max_chars
        self._hubs: dict[str, tuple[float, list[tuple[str, int]]]] = {}

    # ── helpers ──────────────────────────────────────────────────────────────
    def _must_get(self, graph: str, key: str) -> Entity:
        self.reg.require_graph(graph)
        e = self.store.get_entity(graph, key)
        if e is None:
            raise KGError("not_found", f"No entity '{key}' in graph '{graph}'", field="key",
                          suggestions=["kg_resolve to find the right key"])
        return e

    def _compact_map(self, graph: str, keys) -> dict[str, dict]:
        return {e.key: e.compact() for e in self.store.get_entities(graph, list(keys))}

    def _memories(self, ids, types: tuple[str, ...] | None = None) -> list[dict]:
        out = []
        for i in ids:
            m = self.memory_lookup(i)
            if m and (types is None or m.get("type") in types):
                out.append(m)
        return out

    def _neighbors(self, graph, keys, direction, relations, at_ts):
        """Yield (from_key, to_key, edge) for edges touching `keys` in the given direction."""
        if direction in ("out", "both"):
            for e in self.store.edges_from(graph, keys, relations, at_ts):
                yield e.src, e.dst, e
        if direction in ("in", "both"):
            for e in self.store.edges_to(graph, keys, relations, at_ts):
                yield e.dst, e.src, e

    # ── resolve ──────────────────────────────────────────────────────────────
    def resolve(self, query, graphs="*", provider=None, kind=None, type=None, include_retired=False, limit=10) -> dict:
        q = query.strip().lower()
        vec = self.store.embedder.embed(query)
        scored: dict[tuple[str, str], dict] = {}
        for g in self.reg.graph_ids(graphs):
            filt = EntityFilter(provider=provider, kind=kind, type=type, include_retired=include_retired)
            hits = self.store.search_entities(g, vec, filt, limit * 2)
            exact = self.store.get_entity(g, query)
            if exact and (include_retired or exact.valid_to is None):
                hits.append((exact, 1.0))
            for e, s in hits:
                names = {e.key.lower(), e.native_id.lower(), e.display_name.lower(), *(a.lower() for a in e.aliases)}
                score = 1.0 if q in names else round(float(s), 4)
                prev = scored.get((g, e.key))
                if prev is None or score > prev["score"]:
                    scored[(g, e.key)] = {**e.compact(g), "score": score, "retired": e.valid_to is not None}
        results = sorted(scored.values(), key=lambda r: -r["score"])[:limit]
        return {"results": results}

    # ── get ──────────────────────────────────────────────────────────────────
    def get_entity(self, graph, key, as_of=None, include_history=False) -> dict:
        e = self._must_get(graph, key)
        at = iso_to_ts(as_of)
        outs = self.store.edges_from(graph, [key], at_ts=at)
        ins = self.store.edges_to(graph, [key], at_ts=at)
        cm = self._compact_map(graph, {x.dst for x in outs} | {x.src for x in ins})
        out_g: dict[str, list] = {}
        for x in outs:
            out_g.setdefault(x.relation, []).append(cm.get(x.dst, {"key": x.dst}))
        in_g: dict[str, list] = {}
        for x in ins:
            in_g.setdefault(self.reg.inverse_label(x.relation), []).append(cm.get(x.src, {"key": x.src}))
        result = {"entity": {**e.compact(graph), "provider": e.provider, "type": e.type, "native_id": e.native_id,
                             "aliases": e.aliases, "properties": e.properties, "valid_from": e.valid_from,
                             "valid_to": e.valid_to},
                  "out": out_g, "in": in_g, "memories": self._memories(e.memory_ids),
                  "xrefs": [x.to_payload() for x in self.store.xrefs_touching(fq_key(graph, key))]}
        if include_history:
            hist = []
            for x in self.store.edges_from(graph, [key], include_retired=True) + \
                    self.store.edges_to(graph, [key], include_retired=True):
                hist.append({"at": x.valid_from, "event": "open", "src": x.src, "relation": x.relation, "dst": x.dst})
                if x.valid_to:
                    hist.append({"at": x.valid_to, "event": "close", "src": x.src, "relation": x.relation,
                                 "dst": x.dst, "reason": x.retire_reason})
            result["history"] = sorted(hist, key=lambda h: iso_to_ts(h["at"]))
        return fit(result, self.max_chars, ("history",))

    # ── find ─────────────────────────────────────────────────────────────────
    def find(self, graph, provider=None, kind=None, type=None, missing_relation=None, direction="out",
             include_retired=False, limit=100, cursor=None) -> dict:
        self.reg.require_graph(graph)
        if missing_relation:
            self.reg.relation(missing_relation)
        filt = EntityFilter(provider=provider, kind=kind, type=type, include_retired=include_retired)
        page, nxt = self.store.scroll_entities(graph, filt, limit, cursor)
        if missing_relation and page:
            ks = [e.key for e in page]
            edges = (self.store.edges_from if direction == "out" else self.store.edges_to)(graph, ks, [missing_relation])
            have = {x.src if direction == "out" else x.dst for x in edges}
            page = [e for e in page if e.key not in have]
        result = {"items": [e.compact(graph) for e in page], "has_more": nxt is not None, "cursor": nxt}
        return fit(result, self.max_chars, ("items",))

    # ── traverse ─────────────────────────────────────────────────────────────
    def traverse(self, graph, start, direction="both", relations=None, kinds=None, max_depth=3, as_of=None,
                 follow_xrefs=False, limit=200, max_chars=None) -> dict:
        root = self._must_get(graph, start)
        at = iso_to_ts(as_of)
        max_depth = max(0, min(int(max_depth), MAX_DEPTH))
        nodes: dict[str, dict] = {start: {**root.compact(), "depth": 0}}
        edges: dict[tuple, dict] = {}
        frontier, has_more = [start], False
        for depth in range(1, max_depth + 1):
            if not frontier or has_more:
                break
            cand = list(self._neighbors(graph, frontier, direction, relations, at))
            new_keys = {other for _, other, _ in cand if other not in nodes}
            cm = {e.key: e for e in self.store.get_entities(graph, list(new_keys))}
            nxt = []
            for _, other, e in cand:
                if other not in nodes:
                    ent = cm.get(other)
                    if ent is None or (kinds and ent.kind not in kinds):
                        continue
                    if len(nodes) >= limit:
                        has_more = True
                        continue
                    nodes[other] = {**ent.compact(), "depth": depth}
                    nxt.append(other)
                edges[(e.src, e.relation, e.dst, e.valid_from)] = _edge_dict(e)
            frontier = nxt
        result = {"nodes": sorted(nodes.values(), key=lambda n: n["depth"]),
                  "edges": [d for d in edges.values() if d["src"] in nodes and d["dst"] in nodes],
                  "has_more": has_more, "truncated": False}
        if follow_xrefs:
            xs = {}
            for k in nodes:
                for x in self.store.xrefs_touching(fq_key(graph, k)):
                    xs[(x.src, x.relation, x.dst)] = x.to_payload()
            result["xrefs"] = list(xs.values())
        return self._trim_traversal(result, max_chars or self.max_chars)

    def _trim_traversal(self, result: dict, max_chars: int) -> dict:
        if _size(result) <= max_chars:
            return result
        result["truncated"] = True
        nodes = result["nodes"]  # sorted by depth: popping removes the deepest first
        while len(nodes) > 1 and _size(result) > max_chars:
            nodes.pop()
            keep = {n["key"] for n in nodes}
            result["edges"] = [e for e in result["edges"] if e["src"] in keep and e["dst"] in keep]
        for k in ("xrefs", "edges"):
            lst = result.get(k)
            while isinstance(lst, list) and lst and _size(result) > max_chars:
                lst.pop()
        return result

    # ── path ─────────────────────────────────────────────────────────────────
    def path(self, graph, src, dst, relations=None, max_depth=6) -> dict:
        self._must_get(graph, src)
        self._must_get(graph, dst)
        max_depth = max(1, min(int(max_depth), MAX_DEPTH))
        dist, parents, frontier = {src: 0}, {src: []}, [src]
        while frontier and dst not in dist:
            d = dist[frontier[0]]
            if d >= max_depth:
                break
            nxt = []
            for here, other, e in self._neighbors(graph, frontier, "both", relations, None):
                if other not in dist:
                    dist[other] = d + 1
                    parents[other] = [(here, e)]
                    nxt.append(other)
                elif dist[other] == d + 1:
                    parents[other].append((here, e))
            frontier = nxt
        if dst not in dist:
            return {"found": False, "paths": []}
        paths: list[list[tuple[str, Edge | None]]] = []

        def walk(node, acc):
            if len(paths) >= 3:
                return
            if node == src:
                paths.append(list(reversed(acc)))
                return
            for prev, e in parents[node]:
                walk(prev, acc + [(node, e)])

        walk(dst, [])
        cm = self._compact_map(graph, {k for p in paths for k, _ in p} | {src})
        out = []
        for p in paths:
            out.append({"nodes": [cm[src]] + [cm[k] for k, _ in p], "edges": [_edge_dict(e) for _, e in p]})
        return {"found": True, "paths": out}

    # ── impact ───────────────────────────────────────────────────────────────
    def impact(self, graph, key, max_depth=MAX_DEPTH, as_of=None) -> dict:
        root = self._must_get(graph, key)
        at = iso_to_ts(as_of)
        prop = self.reg.propagating_relations()
        seen, frontier, layers = {key}, [key], []
        for depth in range(1, max(1, min(int(max_depth), MAX_DEPTH)) + 1):
            nxt = sorted({e.src for e in self.store.edges_to(graph, frontier, prop, at)} - seen)
            if not nxt:
                break
            seen.update(nxt)
            layers.append((depth, nxt))
            frontier = nxt
        affected_keys = [k for _, ks in layers for k in ks]
        cm = self._compact_map(graph, affected_keys)
        work = self.store.edges_from(graph, affected_keys, ["tracked_in", "documented_in"], at) if affected_keys else []
        wm = self._compact_map(graph, {e.dst for e in work})
        result = {"root": root.compact(graph),
                  "affected": [{"depth": d, "nodes": [cm[k] for k in ks if k in cm]} for d, ks in layers],
                  "tracked_in": list({e.dst: wm[e.dst] for e in work if e.relation == "tracked_in" and e.dst in wm}.values()),
                  "documented_in": list({e.dst: wm[e.dst] for e in work if e.relation == "documented_in" and e.dst in wm}.values())}
        return fit(result, self.max_chars, ("documented_in", "tracked_in", "affected"))

    # ── overview ─────────────────────────────────────────────────────────────
    def _hub_degrees(self, graph: str) -> list[tuple[str, int]]:
        cached = self._hubs.get(graph)
        if cached and time.monotonic() - cached[0] < _HUB_TTL:
            return cached[1]
        deg = Counter()
        for e in self.store.all_current_edges(graph):
            deg[e.src] += 1
            deg[e.dst] += 1
        top = deg.most_common(10)
        self._hubs[graph] = (time.monotonic(), top)
        return top

    def overview(self, graphs="*") -> dict:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        out = []
        for g in self.reg.graph_ids(graphs):
            current = [e for e in self.store.iter_entities(g) if e.valid_to is None]
            hubs = self._hub_degrees(g)
            cm = self._compact_map(g, [k for k, _ in hubs])
            recent = sorted((e for e in current if iso_to_ts(e.updated_at) >= since.timestamp()),
                            key=lambda e: e.updated_at, reverse=True)[:10]
            out.append({"graph": g, "name": self.reg.graphs[g].get("name", g), "entities": len(current),
                        "by_kind": dict(Counter(e.kind for e in current)),
                        "by_provider": dict(Counter(e.provider for e in current)),
                        "hubs": [{**cm[k], "degree": d} for k, d in hubs if k in cm],
                        "recent": [e.compact() for e in recent],
                        "dangling_xrefs": sum(1 for x in self.store.xrefs_for_graph(g) if x.dangling)})
        return fit({"graphs": out}, self.max_chars, ("graphs",))

    # ── cross-graph ──────────────────────────────────────────────────────────
    def related_across(self, graph, key, target_graphs="*", limit=10) -> dict:
        e = self._must_get(graph, key)
        xrefs = [x.to_payload() for x in self.store.xrefs_touching(fq_key(graph, key))]
        vec = self.store.embedder.embed(f"{e.display_name} {' '.join(e.aliases)} {e.type}")
        similar = []
        for g in self.reg.graph_ids(target_graphs):
            if g == graph:
                continue
            for h, s in self.store.search_entities(g, vec, EntityFilter(kind=e.kind), limit):
                similar.append({**h.compact(g), "score": round(float(s), 4),
                                "memories": self._memories(h.memory_ids, LESSON_MEMORY_TYPES)})
        similar.sort(key=lambda r: -r["score"])
        return fit({"xrefs": xrefs, "similar": similar[:limit]}, self.max_chars, ("similar", "xrefs"))

    def for_memory(self, memory_id, graphs="*") -> dict:
        ents, edges = [], []
        for g in self.reg.graph_ids(graphs):
            page, cur = self.store.scroll_entities(g, EntityFilter(memory_id=memory_id, include_retired=True), 100)
            ents += [e.compact(g) for e in page]
            edges += [_edge_dict(x, g) for x in self.store.edges_with_memory(g, memory_id)]
        return fit({"entities": ents, "edges": edges}, self.max_chars, ("edges", "entities"))
```

- [ ] **Step 4: Run query tests**

Run: `pytest tests/kg/test_query.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add src/memory_mcp/kg/query.py tests/kg/test_query.py
git commit -m "feat(kg): query engine: resolve, traverse, path, impact, overview, cross-graph (RCL-36)"
```

### Task 8: Config, `build_kg`, 15 MCP tools, server wiring (RCL-37, part 1)

**Files:**
- Modify: `src/memory_mcp/config.py`, `src/memory_mcp/kg/__init__.py`, `src/memory_mcp/mcp_tools.py` (`_init`), `src/memory_mcp/server.py` (`create_app`), `tests/test_server.py` (`make_app`), `tests/test_config.py`
- Create: `src/memory_mcp/kg/tools.py`
- Test: `tests/kg/test_tools.py`, `tests/kg/test_build.py`

**Interfaces:**
- Consumes: `Registry.load`, `QdrantGraphStore`, `KGService`, `KGQuery`, `count_error`; `MemoryStore.client/.embedder/.get` (Task 1)
- Produces:
  - `Config` gains `kg_enabled: bool = False`, `kg_config_dir: str | None = None`, `kg_dup_threshold: float = 0.90`, `kg_max_chars: int = 6000`
  - `memory_mcp.kg.KG` dataclass with `registry`, `store`, `service`, `query`
  - `build_kg(client, embedder, memory_store=None, config_dir=None, dup_threshold=0.90, max_chars=6000) -> KG`
  - `kg.tools.register(mcp, service, query) -> None`, which registers the 15 tools named in the test below
  - `mcp_tools._init(store, kg=None)`

- [ ] **Step 1: Write failing config test**

Append to `tests/test_config.py`:
```python
def test_kg_settings_default_off(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://q:6333")
    monkeypatch.setenv("API_TOKEN", "t")
    for v in ("KG_ENABLED", "KG_CONFIG_DIR", "KG_DUP_THRESHOLD", "KG_MAX_CHARS"):
        monkeypatch.delenv(v, raising=False)
    from memory_mcp.config import load_config
    c = load_config()
    assert (c.kg_enabled, c.kg_config_dir, c.kg_dup_threshold, c.kg_max_chars) == (False, None, 0.90, 6000)


def test_kg_settings_from_env(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://q:6333")
    monkeypatch.setenv("API_TOKEN", "t")
    monkeypatch.setenv("KG_ENABLED", "True")
    monkeypatch.setenv("KG_CONFIG_DIR", "/etc/kg")
    monkeypatch.setenv("KG_DUP_THRESHOLD", "0.8")
    monkeypatch.setenv("KG_MAX_CHARS", "9000")
    from memory_mcp.config import load_config
    c = load_config()
    assert (c.kg_enabled, c.kg_config_dir, c.kg_dup_threshold, c.kg_max_chars) == (True, "/etc/kg", 0.8, 9000)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'kg_enabled'`

- [ ] **Step 3: Extend `config.py`**

Add the fields at the end of `Config` (they have defaults, so existing positional construction keeps working):
```python
    kg_enabled: bool = False
    kg_config_dir: str | None = None
    kg_dup_threshold: float = 0.90
    kg_max_chars: int = 6000
```
And in `load_config()`'s `Config(...)` call, after `default_list_limit=...`:
```python
        kg_enabled=os.environ.get("KG_ENABLED", "false").strip().lower() in ("1", "true", "yes"),
        kg_config_dir=os.environ.get("KG_CONFIG_DIR") or None,
        kg_dup_threshold=float(os.environ.get("KG_DUP_THRESHOLD", "0.90")),
        kg_max_chars=int(os.environ.get("KG_MAX_CHARS", "6000")),
```

Run: `pytest tests/test_config.py -v` and expect PASS.

- [ ] **Step 4: Write failing `build_kg` and tool tests**

`tests/kg/test_build.py`:
```python
from types import SimpleNamespace

from memory_mcp.kg import build_kg
from memory_mcp.kg.store import edges_collection, entities_collection


def test_build_kg_creates_collections_and_memory_lookup(qdrant, stub_embedder):
    rec = SimpleNamespace(id="m1", name="runbook-x", type="runbook")
    mem = SimpleNamespace(get=lambda i: rec if i == "m1" else None)
    kg = build_kg(qdrant, stub_embedder, memory_store=mem)
    names = {c.name for c in qdrant.get_collections().collections}
    assert {entities_collection("mcit"), edges_collection("vtv"), "kg_xrefs"} <= names
    assert kg.query.memory_lookup("m1") == {"id": "m1", "name": "runbook-x", "type": "runbook"}
    assert kg.query.memory_lookup("missing") is None


def test_build_kg_config_dir_override(qdrant, stub_embedder, tmp_path):
    (tmp_path / "kg_graphs.yaml").write_text("graphs:\n  lab: {name: Lab}\n")
    (tmp_path / "kg_relations.yaml").write_text("relations:\n  runs_on: {inverse: runs, impact: propagates}\n")
    kg = build_kg(qdrant, stub_embedder, config_dir=str(tmp_path))
    assert list(kg.registry.graphs) == ["lab"]
    assert "apps/Deployment" in kg.registry.types["kubernetes"]  # packaged catalogs used when no catalog/ subdir
```

`tests/kg/test_tools.py`:
```python
import asyncio
import json

import pytest
from mcp.server.mcpserver import MCPServer

from memory_mcp.kg.query import KGQuery
from memory_mcp.kg.tools import register

TOOLS = {"kg_upsert_entity", "kg_link", "kg_unlink", "kg_retire_entity", "kg_batch", "kg_xref",
         "kg_resolve", "kg_get_entity", "kg_find", "kg_traverse", "kg_path", "kg_impact", "kg_overview",
         "kg_related_across", "kg_for_memory"}


@pytest.fixture
def server(svc, kg_registry, gstore):
    s = MCPServer("test")
    register(s, svc, KGQuery(kg_registry, gstore))
    return s


def call(s, name, **args):
    r = asyncio.run(s.call_tool(name, args))
    return json.loads(r.content[0].text)


def test_registers_exactly_15_tools(server):
    assert {t.name for t in asyncio.run(server.list_tools())} == TOOLS


def test_write_then_read_round_trip(server):
    d = call(server, "kg_upsert_entity", graph="mcit", provider="kubernetes", type="kubernetes_deployment_v1",
             native_id="mcit-k8s/digital-twin/apps/Deployment/memory-mcp", display_name="memory-mcp")
    n = call(server, "kg_upsert_entity", graph="mcit", provider="kubernetes", type="core/Node",
             native_id="mcit-k8s/_cluster/core/Node/opi-5", display_name="opi-5")
    assert call(server, "kg_link", graph="mcit", src=d["key"], relation="runs_on", dst=n["key"])["status"] == "created"
    t = call(server, "kg_traverse", graph="mcit", start=d["key"], max_depth=1)
    assert {x["key"] for x in t["nodes"]} == {d["key"], n["key"]}
    assert call(server, "kg_resolve", query="opi-5", graphs=["mcit"])["results"][0]["key"] == n["key"]
    assert call(server, "kg_impact", graph="mcit", key=n["key"])["affected"][0]["nodes"][0]["key"] == d["key"]


def test_errors_are_returned_not_raised(server):
    r = call(server, "kg_upsert_entity", graph="mcit", provider="kubernetes", type="apps/Deploymnet",
             native_id="c/n/apps/Deployment/x")
    assert r["error"] == "unknown_type" and "apps/Deployment" in r["suggestions"]
    assert call(server, "kg_get_entity", graph="mcit", key="kubernetes:none")["error"] == "not_found"


def test_batch_and_overview(server):
    r = call(server, "kg_batch", graph="vtv", entities=[
        {"provider": "logical", "type": "jira_issue", "native_id": "VTV-238"}], edges=[])
    assert r["status"] == "ok"
    ov = call(server, "kg_overview", graphs="vtv")
    assert ov["graphs"][0]["entities"] == 1
```

- [ ] **Step 5: Run to verify failure**

Run: `pytest tests/kg/test_build.py tests/kg/test_tools.py -v`
Expected: FAIL (`ImportError: cannot import name 'build_kg'`; `No module named 'memory_mcp.kg.tools'`)

- [ ] **Step 6: Implement `kg/__init__.py`**

```python
"""Knowledge graph package: build the registry/store/service/query stack."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opentelemetry import metrics

from memory_mcp.kg.query import KGQuery
from memory_mcp.kg.registry import Registry
from memory_mcp.kg.service import KGService
from memory_mcp.kg.store import EntityFilter, QdrantGraphStore

_meter = metrics.get_meter("memory_mcp.kg")
_gauge_registered = False


@dataclass
class KG:
    registry: Registry
    store: QdrantGraphStore
    service: KGService
    query: KGQuery


def _memory_lookup(memory_store):
    def lookup(memory_id: str) -> dict | None:
        try:
            r = memory_store.get(memory_id)
        except Exception:  # malformed ids raise in qdrant retrieve; treat as missing
            return None
        return {"id": r.id, "name": r.name, "type": r.type} if r else None
    return lookup


def build_kg(client, embedder, memory_store=None, config_dir: str | None = None,
             dup_threshold: float = 0.90, max_chars: int = 6000) -> KG:
    if config_dir:
        d = Path(config_dir)
        registry = Registry.load(config_dir=d, catalog_dir=d / "catalog" if (d / "catalog").is_dir() else None)
    else:
        registry = Registry.load()
    store = QdrantGraphStore(client, embedder)
    for g in registry.graphs:
        store.ensure_graph(g)
    store.ensure_xrefs()
    service = KGService(registry, store, dup_threshold=dup_threshold)
    query = KGQuery(registry, store, memory_lookup=_memory_lookup(memory_store) if memory_store else None,
                    max_chars=max_chars)

    global _gauge_registered
    if not _gauge_registered:
        def observe(_options):
            for g in registry.graphs:
                try:
                    yield metrics.Observation(store.count_entities(g, EntityFilter()), {"graph": g})
                except Exception:
                    pass
        _meter.create_observable_gauge("kg_entities", callbacks=[observe], description="Current kg entities per graph")
        _gauge_registered = True
    return KG(registry, store, service, query)
```

- [ ] **Step 7: Implement `kg/tools.py`**

```python
"""MCP bindings for the knowledge graph. Thin: validation lives in service/query."""
from __future__ import annotations

import time

from opentelemetry import metrics

from memory_mcp.kg.models import KGError
from memory_mcp.kg.service import count_error

_dur = metrics.get_meter("memory_mcp.kg").create_histogram(
    "kg_query_duration_seconds", unit="s", description="kg tool latency")


def _call(tool: str, graph, fn, *args, **kwargs) -> dict:
    t0 = time.monotonic()
    try:
        return fn(*args, **kwargs)
    except KGError as e:
        count_error(e.code)
        return e.to_dict()
    finally:
        _dur.record(time.monotonic() - t0, {"tool": tool, "graph": graph if isinstance(graph, str) else ",".join(graph or [])})


def register(mcp, service, query) -> None:
    query_engine = query  # `query` is also kg_resolve's argument name; use query_engine inside the tools

    @mcp.tool()
    def kg_upsert_entity(graph: str, provider: str, type: str, native_id: str, display_name: str | None = None,
                         aliases: list[str] | None = None, properties: dict | None = None,
                         memory_ids: list[str] | None = None, agent: str = "claude-code") -> dict:
        """Create/update a knowledge-graph entity. provider: azure|aws|gcp|vmware|hyperv|cloudflare|kubernetes|logical.
        type: provider-native (e.g. Microsoft.ContainerService/managedClusters, AWS::EC2::VPC, apps/Deployment) or a
        Terraform type (azurerm_kubernetes_cluster). native_id: ARM ID / ARN / GCP full name /
        k8s '{cluster}/{ns|_cluster}/{group}/{Kind}/{name}' / Jira key. Call kg_resolve first to reuse keys."""
        return _call("kg_upsert_entity", graph, service.upsert_entity, graph, provider, type, native_id,
                     display_name, aliases, properties, memory_ids, agent)

    @mcp.tool()
    def kg_link(graph: str, src: str, relation: str, dst: str, properties: dict | None = None,
                evidence_memory_ids: list[str] | None = None, agent: str = "claude-code",
                valid_from: str | None = None) -> dict:
        """Create/refresh a topology edge between two current entities in one graph (e.g. runs_on, routes_to,
        depends_on, tracked_in). Only record verified topology; attach evidence_memory_ids."""
        return _call("kg_link", graph, service.link, graph, src, relation, dst, properties, evidence_memory_ids,
                     agent, valid_from)

    @mcp.tool()
    def kg_unlink(graph: str, src: str, relation: str, dst: str, reason: str) -> dict:
        """Soft-retire a current edge (history is kept). Use when something moves or is reconfigured."""
        return _call("kg_unlink", graph, service.unlink, graph, src, relation, dst, reason)

    @mcp.tool()
    def kg_retire_entity(graph: str, key: str, reason: str) -> dict:
        """Soft-retire an entity and all its current edges (decommission). Never hard-delete via agents."""
        return _call("kg_retire_entity", graph, service.retire_entity, graph, key, reason)

    @mcp.tool()
    def kg_batch(graph: str, entities: list[dict] | None = None, edges: list[dict] | None = None,
                 agent: str = "claude-code") -> dict:
        """Record a whole topology at once (e.g. after a verified terraform apply). Everything is validated first;
        any error rejects the batch. Entity items: provider,type,native_id,display_name,aliases,properties,
        memory_ids,valid_from,valid_to. Edge items: src,relation,dst,properties,evidence_memory_ids,valid_from,
        valid_to,retire_reason (valid_to records historical edges). Safe to re-run."""
        return _call("kg_batch", graph, service.batch, graph, entities or [], edges or [], agent)

    @mcp.tool()
    def kg_xref(src: str, relation: str, dst: str, note: str, evidence_memory_ids: list[str] | None = None,
                agent: str = "claude-code") -> dict:
        """Cross-graph reference between fully qualified keys ('{graph}::{key}'). relation: pattern_from,
        lessons_from, similar_to, supersedes_approach_of. Use when one graph's work reuses another's pattern."""
        return _call("kg_xref", None, service.xref, src, relation, dst, note, evidence_memory_ids, agent)

    @mcp.tool()
    def kg_resolve(query: str, graphs: str | list[str] = "*", provider: str | None = None, kind: str | None = None,
                   type: str | None = None, include_retired: bool = False, limit: int = 10) -> dict:
        """Find entity keys by name, alias, native id or description. Call before writing or querying."""
        return _call("kg_resolve", graphs, query_engine.resolve, query, graphs, provider, kind, type,
                     include_retired, limit)

    @mcp.tool()
    def kg_get_entity(graph: str, key: str, as_of: str | None = None, include_history: bool = False) -> dict:
        """Everything about one entity: edges both directions, linked memories, xrefs, optional history."""
        return _call("kg_get_entity", graph, query_engine.get_entity, graph, key, as_of, include_history)

    @mcp.tool()
    def kg_find(graph: str, provider: str | None = None, kind: str | None = None, type: str | None = None,
                missing_relation: str | None = None, direction: str = "out", include_retired: bool = False,
                limit: int = 100, cursor: str | None = None) -> dict:
        """List entities by filter. missing_relation finds orphans (e.g. workloads without tracked_in).
        has_more=false means the list is complete."""
        return _call("kg_find", graph, query_engine.find, graph, provider, kind, type, missing_relation, direction,
                     include_retired, limit, cursor)

    @mcp.tool()
    def kg_traverse(graph: str, start: str, direction: str = "both", relations: list[str] | None = None,
                    kinds: list[str] | None = None, max_depth: int = 3, as_of: str | None = None,
                    follow_xrefs: bool = False, limit: int = 200) -> dict:
        """Neighborhood subgraph around an entity (BFS, max_depth<=6). as_of='YYYY-MM-DD' shows past topology."""
        return _call("kg_traverse", graph, query_engine.traverse, graph, start, direction, relations, kinds,
                     max_depth, as_of, follow_xrefs, limit)

    @mcp.tool()
    def kg_path(graph: str, src: str, dst: str, relations: list[str] | None = None, max_depth: int = 6) -> dict:
        """Up to 3 shortest paths between two entities (edges followed in either direction)."""
        return _call("kg_path", graph, query_engine.path, graph, src, dst, relations, max_depth)

    @mcp.tool()
    def kg_impact(graph: str, key: str, max_depth: int = 6, as_of: str | None = None) -> dict:
        """Blast radius: everything that depends on this entity, by depth, plus linked Jira/Confluence."""
        return _call("kg_impact", graph, query_engine.impact, graph, key, max_depth, as_of)

    @mcp.tool()
    def kg_overview(graphs: str | list[str] = "*") -> dict:
        """Short orientation per graph: counts, hubs (likely single points of failure), recent changes."""
        return _call("kg_overview", graphs, query_engine.overview, graphs)

    @mcp.tool()
    def kg_related_across(graph: str, key: str, target_graphs: str | list[str] = "*", limit: int = 10) -> dict:
        """Lessons/patterns from other graphs: explicit xrefs plus similar entities with their
        decision/troubleshooting/runbook memories."""
        return _call("kg_related_across", graph, query_engine.related_across, graph, key, target_graphs, limit)

    @mcp.tool()
    def kg_for_memory(memory_id: str, graphs: str | list[str] = "*") -> dict:
        """Entities and edges that reference a memory (memory_ids / evidence_memory_ids)."""
        return _call("kg_for_memory", graphs, query_engine.for_memory, memory_id, graphs)
```
- [ ] **Step 8: Wire into `mcp_tools._init` and `server.create_app`**

`src/memory_mcp/mcp_tools.py`: change the signature to `def _init(store: MemoryStore, kg=None) -> None:` and append at the end of the function body:
```python
    if kg is not None:
        from memory_mcp.kg.tools import register
        register(mcp, kg.service, kg.query)
```

`src/memory_mcp/server.py`: replace `mcp_tools._init(store)` with:
```python
    kg = None
    if cfg.kg_enabled:
        from memory_mcp.kg import build_kg
        kg = build_kg(store.client, store.embedder, memory_store=store, config_dir=cfg.kg_config_dir,
                      dup_threshold=cfg.kg_dup_threshold, max_chars=cfg.kg_max_chars)
    mcp_tools._init(store, kg)
```
(The REST router is mounted in Task 9.)

`tests/test_server.py::make_app`: add `kg_enabled=False,` to the `MagicMock(...)` config. Without it, the MagicMock attribute is truthy and the kg path would run against a mocked client.

- [ ] **Step 9: Run tests**

Run: `pytest -v`
Expected: everything passes, including `tests/kg/test_build.py` (2) and `tests/kg/test_tools.py` (4).

- [ ] **Step 10: Commit**

```bash
git add src/memory_mcp/config.py src/memory_mcp/kg/__init__.py src/memory_mcp/kg/tools.py src/memory_mcp/mcp_tools.py \
        src/memory_mcp/server.py tests/test_config.py tests/test_server.py tests/kg/test_build.py tests/kg/test_tools.py
git commit -m "feat(kg): KG_ENABLED config, build_kg, 15 kg_* MCP tools (RCL-37)"
```

### Task 9: REST router, export/import (RCL-37, part 2)

**Files:**
- Create: `src/memory_mcp/kg/rest.py`
- Modify: `src/memory_mcp/server.py` (mount the router after `require_token` is defined)
- Test: `tests/kg/test_rest.py`

**Interfaces:**
- Consumes: `KGService` (incl. `.reg`, `.store`), `KGQuery`, `count_error`, `fq_key`, store `iter_entities/iter_edges/xrefs_for_graph/delete_entity_hard/mark_xrefs_dangling`
- Produces: `build_router(service, query, require_token: Callable) -> APIRouter` (prefix `/kg`). The NDJSON export line shapes are `{...Entity.to_payload(), "record": "entity"}`, `{...Edge.to_payload(), "record": "edge"}` and `{...XRef.to_payload(), "record": "xref"}`. The marker is `record`, not `kind`, because entities already have a `kind` field.

HTTP status for `KGError`: `not_found`, `endpoint_not_found` and `unknown_graph` → 404; every other code → 422. The body is `{"detail": KGError.to_dict()}`.

- [ ] **Step 1: Write failing REST tests**

`tests/kg/test_rest.py`:
```python
import json
from urllib.parse import quote

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from qdrant_client import QdrantClient

from memory_mcp.kg.query import KGQuery
from memory_mcp.kg.rest import build_router
from memory_mcp.kg.service import KGService
from memory_mcp.kg.store import QdrantGraphStore

SUB = "/subscriptions/00000000-0000-0000-0000-000000000001"
AKS = f"azure:{SUB}/resourcegroups/rg-aks/providers/microsoft.containerservice/managedclusters/aks-vtv-prod"
DEP = "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"
OPI = "kubernetes:mcit-k8s/_cluster/core/Node/opi-5"
OLD = "kubernetes:ms01-k8s/_cluster/core/Node/ms01-k8s-wkr-01"


def no_auth():
    return None


def make_app(svc, kg_registry, gstore, dep=no_auth):
    app = FastAPI()
    app.include_router(build_router(svc, KGQuery(kg_registry, gstore), dep))
    return app


def client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def seeded(svc):
    svc.upsert_entity("vtv", "azure", "azurerm_kubernetes_cluster", AKS.split(":", 1)[1], display_name="aks-vtv-prod")
    svc.upsert_entity("vtv", "logical", "jira_issue", "VTV-238")
    svc.batch("mcit", [
        {"provider": "kubernetes", "type": "apps/Deployment", "native_id": DEP.split(":", 1)[1], "display_name": "memory-mcp"},
        {"provider": "kubernetes", "type": "core/Node", "native_id": OPI.split(":", 1)[1], "display_name": "opi-5"},
        {"provider": "kubernetes", "type": "core/Node", "native_id": OLD.split(":", 1)[1], "display_name": "ms01-k8s-wkr-01",
         "valid_from": "2026-04-01T00:00:00+00:00", "valid_to": "2026-09-27T07:00:00+00:00"},
        {"provider": "logical", "type": "jira_issue", "native_id": "MCIT-184"},
    ], [
        {"src": DEP, "relation": "runs_on", "dst": OPI},
        {"src": DEP, "relation": "runs_on", "dst": OLD, "valid_from": "2026-04-01T00:00:00+00:00",
         "valid_to": "2026-09-27T07:00:00+00:00", "retire_reason": "MCIT-193"},
    ])
    svc.xref("vtv::logical:VTV-238", "pattern_from", "mcit::logical:MCIT-184", "Key Vault CSI pattern")
    return svc


async def test_graphs_and_overview(seeded, kg_registry, gstore):
    async with client(make_app(seeded, kg_registry, gstore)) as c:
        assert set((await c.get("/kg/graphs")).json()["graphs"]) == {"mcit", "vtv"}
        ov = (await c.get("/kg/overview", params={"graphs": "mcit"})).json()
        assert ov["graphs"][0]["entities"] == 3


async def test_get_entity_with_slashes_in_key(seeded, kg_registry, gstore):
    async with client(make_app(seeded, kg_registry, gstore)) as c:
        raw = await c.get(f"/kg/vtv/entities/{AKS}")
        enc = await c.get(f"/kg/vtv/entities/{quote(AKS, safe='')}")
        k8s = await c.get(f"/kg/mcit/entities/{quote(DEP, safe='')}")
    assert raw.status_code == enc.status_code == k8s.status_code == 200
    assert raw.json()["entity"]["key"] == enc.json()["entity"]["key"] == AKS
    assert k8s.json()["out"]["runs_on"][0]["key"] == OPI


async def test_traverse_path_impact_resolve(seeded, kg_registry, gstore):
    async with client(make_app(seeded, kg_registry, gstore)) as c:
        t = (await c.post("/kg/mcit/traverse", json={"start": DEP, "max_depth": 1, "as_of": "2026-09-20"})).json()
        assert {n["key"] for n in t["nodes"]} == {DEP, OLD}
        p = (await c.post("/kg/mcit/path", json={"src": DEP, "dst": OPI})).json()
        assert p["found"] is True
        i = (await c.post("/kg/mcit/impact", json={"key": OPI})).json()
        assert i["affected"][0]["nodes"][0]["key"] == DEP
        r = (await c.post("/kg/resolve", json={"query": "aks-vtv-prod"})).json()
        assert r["results"][0]["key"] == AKS and r["results"][0]["graph"] == "vtv"


async def test_errors_map_to_http(seeded, kg_registry, gstore):
    async with client(make_app(seeded, kg_registry, gstore)) as c:
        nf = await c.get("/kg/mcit/entities/kubernetes:nope")
        ug = await c.post("/kg/nope/traverse", json={"start": DEP})
    assert nf.status_code == 404 and nf.json()["detail"]["error"] == "not_found"
    assert ug.status_code == 404 and ug.json()["detail"]["error"] == "unknown_graph"


async def test_auth_dependency_applies(seeded, kg_registry, gstore):
    def deny():
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    async with client(make_app(seeded, kg_registry, gstore, dep=deny)) as c:
        assert (await c.get("/kg/graphs")).status_code == 401


async def test_export_import_round_trip(seeded, kg_registry, gstore, stub_embedder):
    async with client(make_app(seeded, kg_registry, gstore)) as c:
        mcit = (await c.get("/kg/mcit/export")).text
        vtv = (await c.get("/kg/vtv/export")).text
    lines = [json.loads(x) for x in mcit.splitlines() if x]
    assert sum(x["record"] == "edge" for x in lines) == 2  # retired edge included
    # import into a brand-new instance
    fresh = QdrantGraphStore(QdrantClient(":memory:"), stub_embedder)
    for g in kg_registry.graphs:
        fresh.ensure_graph(g)
    fresh.ensure_xrefs()
    svc2 = KGService(kg_registry, fresh)
    async with client(make_app(svc2, kg_registry, fresh)) as c:
        r1 = (await c.post("/kg/mcit/import", content=mcit)).json()
        r2 = (await c.post("/kg/vtv/import", content=vtv)).json()
        again = (await c.post("/kg/mcit/import", content=mcit)).json()
    assert r1["status"] == "ok" and r2["status"] == "ok"
    assert r2["xrefs"] == {"imported": 1, "errors": []}
    assert {e.key for e in fresh.iter_entities("mcit")} == {e.key for e in gstore.iter_entities("mcit")}
    assert len(list(fresh.iter_edges("mcit"))) == 2
    assert fresh.get_entity("mcit", OLD).valid_to == "2026-09-27T07:00:00+00:00"
    assert all(w["status"] in ("updated", "retired", "recorded") for w in again["written"])


async def test_hard_delete_requires_flag_and_marks_xrefs(seeded, kg_registry, gstore):
    async with client(make_app(seeded, kg_registry, gstore)) as c:
        soft = await c.delete("/kg/mcit/entities/logical:MCIT-184")
        hard = await c.delete("/kg/mcit/entities/logical:MCIT-184", params={"hard": "true"})
    assert soft.status_code == 400
    assert hard.status_code == 200 and hard.json()["deleted"] == "logical:MCIT-184"
    assert gstore.xrefs_touching("mcit::logical:MCIT-184")[0].dangling is True
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/kg/test_rest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory_mcp.kg.rest'`

- [ ] **Step 3: Implement `rest.py`**

```python
"""FastAPI router for the knowledge graph (bearer-protected via the injected dependency)."""
from __future__ import annotations

import json
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from memory_mcp.kg.models import KGError, fq_key
from memory_mcp.kg.service import EDGE_FIELDS, ENTITY_FIELDS, count_error

_404 = {"not_found", "endpoint_not_found", "unknown_graph"}


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KGError as e:
        count_error(e.code)
        raise HTTPException(status_code=404 if e.code in _404 else 422, detail=e.to_dict())


def _graphs_param(graphs: str) -> str | list[str]:
    return "*" if graphs in ("", "*") else [g for g in graphs.split(",") if g]


class ResolveReq(BaseModel):
    query: str
    graphs: str | list[str] = "*"
    provider: str | None = None
    kind: str | None = None
    type: str | None = None
    include_retired: bool = False
    limit: int = 10


class TraverseReq(BaseModel):
    start: str
    direction: str = "both"
    relations: list[str] | None = None
    kinds: list[str] | None = None
    max_depth: int = 3
    as_of: str | None = None
    follow_xrefs: bool = False
    limit: int = 200


class PathReq(BaseModel):
    src: str
    dst: str
    relations: list[str] | None = None
    max_depth: int = 6


class ImpactReq(BaseModel):
    key: str
    max_depth: int = 6
    as_of: str | None = None


def build_router(service, query, require_token: Callable) -> APIRouter:
    r = APIRouter(prefix="/kg", dependencies=[Depends(require_token)])
    store = service.store

    @r.get("/graphs")
    def graphs():
        return {"graphs": service.reg.graphs}

    @r.get("/overview")
    def overview(graphs: str = "*"):
        return _run(query.overview, _graphs_param(graphs))

    @r.post("/resolve")
    def resolve(req: ResolveReq):
        return _run(query.resolve, req.query, req.graphs, req.provider, req.kind, req.type, req.include_retired, req.limit)

    @r.get("/{graph}/entities/{key:path}")
    def get_entity(graph: str, key: str, as_of: str | None = None, include_history: bool = False):
        return _run(query.get_entity, graph, key, as_of, include_history)

    @r.post("/{graph}/traverse")
    def traverse(graph: str, req: TraverseReq):
        return _run(query.traverse, graph, req.start, req.direction, req.relations, req.kinds, req.max_depth,
                    req.as_of, req.follow_xrefs, req.limit)

    @r.post("/{graph}/path")
    def path(graph: str, req: PathReq):
        return _run(query.path, graph, req.src, req.dst, req.relations, req.max_depth)

    @r.post("/{graph}/impact")
    def impact(graph: str, req: ImpactReq):
        return _run(query.impact, graph, req.key, req.max_depth, req.as_of)

    @r.get("/{graph}/export")
    def export(graph: str):
        _run(service.reg.require_graph, graph)

        def lines():
            for e in store.iter_entities(graph):
                yield json.dumps({**e.to_payload(), "record": "entity"}) + "\n"
            for e in store.iter_edges(graph):
                yield json.dumps({**e.to_payload(), "record": "edge"}) + "\n"
            for x in store.xrefs_for_graph(graph):
                if x.src.startswith(f"{graph}::"):  # each xref is exported once, by its source graph
                    yield json.dumps({**x.to_payload(), "record": "xref"}) + "\n"
        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @r.post("/{graph}/import")
    async def import_(graph: str, request: Request):
        _run(service.reg.require_graph, graph)
        ents, edges, xrefs = [], [], []
        for n, line in enumerate((await request.body()).decode().splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise HTTPException(422, detail={"error": "invalid_json", "message": f"line {n}: {e}"})
            record = rec.get("record")
            if record == "entity":
                ents.append({k: rec.get(k) for k in ENTITY_FIELDS})
            elif record == "edge":
                edges.append({k: rec.get(k) for k in EDGE_FIELDS})
            elif record == "xref":
                xrefs.append(rec)
        result = _run(service.batch, graph, ents, edges, "import")
        if result["status"] != "ok":
            raise HTTPException(422, detail=result)
        xr = {"imported": 0, "errors": []}
        for x in xrefs:
            try:
                service.xref(x["src"], x["relation"], x["dst"], x["note"], x.get("evidence_memory_ids"),
                             x.get("agent", "import"))
                xr["imported"] += 1
            except KGError as e:
                xr["errors"].append({"xref": f"{x['src']} -{x['relation']}-> {x['dst']}", **e.to_dict()})
        return {**result, "xrefs": xr}

    @r.delete("/{graph}/entities/{key:path}")
    def delete_entity(graph: str, key: str, hard: bool = False):
        _run(service.reg.require_graph, graph)
        if not hard:
            raise HTTPException(400, detail={"error": "hard_required",
                                             "message": "Use kg_retire_entity to decommission; pass hard=true to purge"})
        if store.get_entity(graph, key) is None:
            raise HTTPException(404, detail={"error": "not_found", "message": key})
        removed = store.delete_entity_hard(graph, key)
        store.mark_xrefs_dangling(fq_key(graph, key))
        return {"deleted": key, "edges_removed": removed}

    return r
```

- [ ] **Step 4: Mount the router in `server.py`**

In `create_app`, immediately after the `require_token` function definition, add:
```python
    if kg is not None:
        from memory_mcp.kg.rest import build_router
        app.include_router(build_router(kg.service, kg.query, require_token))
```

- [ ] **Step 5: Run tests**

Run: `pytest -v`
Expected: all pass, including 7 in `tests/kg/test_rest.py`

- [ ] **Step 6: Commit**

```bash
git add src/memory_mcp/kg/rest.py src/memory_mcp/server.py tests/kg/test_rest.py
git commit -m "feat(kg): /kg REST routes with NDJSON export/import (RCL-37)"
```

### Task 10: Acceptance seed + script, dev rollout, spec/README updates (RCL-38)

**Files:**
- Create: `scripts/kg_seed/mcit.jsonl.tmpl`, `scripts/kg_seed/vtv.jsonl.tmpl`, `scripts/kg_acceptance.py`
- Test: `tests/kg/test_acceptance_script.py` (runs the full acceptance in-process)
- Modify: `docs/superpowers/specs/2026-10-03-knowledge-graph-design.md` (amendments), `README.md` (kg section)
- Modify (separate repo, separate PR): `midcityit/infra-tf`: the digital-twin-dev memory-mcp env (`KG_ENABLED=true`) and, if needed, the Qdrant backup CronJob

**Interfaces:**
- Consumes: the `/kg/*` REST routes (Task 9)
- Produces: `kg_acceptance.render_seed(name: str, env: Mapping[str, str]) -> str`, `kg_acceptance.run(client: httpx.Client, env: Mapping[str, str], check_latency: bool = True) -> list[tuple[str, bool, str]]`, and a CLI `python scripts/kg_acceptance.py` (exit 0 only if all checks pass)

**Seed placeholders**, all filled from the environment at run time. No IDs are guessed, and the values are resource IDs, not secrets:
| Variable | How to get it |
|---|---|
| `CF_ACCOUNT_ID` | Cloudflare dashboard → account ID (32 hex), or `terraform -chdir=<infra-tf>/terraform output` if exported |
| `MCIT_APPI_ID` | `az resource list --name appi-memory-twin-mcit --query "[0].id" -o tsv` |
| `CCTECH_KV_ID` | `az keyvault show -n cctech-keyvault --query id -o tsv` |
| `VTV_AKS_ID` | In the Veritiv tenant: `az aks list --query "[?name=='aks-vtv-prod'].id" -o tsv` |
| `MCIT_GATEWAY_NS`, `MCIT_GATEWAY_NAME` | `kubectl --context mcit-k8s get gateway -A` (the gateway the memory-mcp HTTPRoute attaches to) |
| `MS01_QDRANT_PV` | `kubectl --context ms01-k8s get pv` (the hostPath `/data/qdrant` PV) |
| `KG_BASE_URL` | dev endpoint for digital-twin-dev (e.g. its HTTPRoute hostname) |
| `MEMORY_TWIN_BEARER` | dev API token, fetched at run time from `<cctech-keyvault:memory-mcp-api-token>` (dev instance secret); never echoed |

- [ ] **Step 1: Write the mcit seed template**

`scripts/kg_seed/mcit.jsonl.tmpl`, one JSON object per line (the same NDJSON format as export, so it goes through `/kg/mcit/import`). The `memory_ids` are the real memory-twin records for the MCIT-193 migration (`d73c22c3-…`) and the deployment reference (`0eb13387-…`).
```
{"record": "entity", "provider": "cloudflare", "type": "dns_record", "native_id": "${CF_ACCOUNT_ID}/chriscastrotech.com/dns_record/memory-mcp", "display_name": "memory-mcp.chriscastrotech.com"}
{"record": "entity", "provider": "cloudflare", "type": "zero_trust_tunnel_cloudflared", "native_id": "${CF_ACCOUNT_ID}/-/zero_trust_tunnel_cloudflared/homelab", "display_name": "homelab tunnel"}
{"record": "entity", "provider": "kubernetes", "type": "core/Cluster", "native_id": "mcit-k8s/_cluster/core/Cluster/mcit-k8s", "display_name": "mcit-k8s"}
{"record": "entity", "provider": "kubernetes", "type": "core/Node", "native_id": "mcit-k8s/_cluster/core/Node/opi-5", "display_name": "opi-5", "properties": {"arch": "arm64"}}
{"record": "entity", "provider": "kubernetes", "type": "core/Namespace", "native_id": "mcit-k8s/_cluster/core/Namespace/digital-twin", "display_name": "digital-twin"}
{"record": "entity", "provider": "kubernetes", "type": "gateway.networking.k8s.io/Gateway", "native_id": "mcit-k8s/${MCIT_GATEWAY_NS}/gateway.networking.k8s.io/Gateway/${MCIT_GATEWAY_NAME}", "display_name": "mcit envoy gateway"}
{"record": "entity", "provider": "kubernetes", "type": "gateway.networking.k8s.io/HTTPRoute", "native_id": "mcit-k8s/digital-twin/gateway.networking.k8s.io/HTTPRoute/memory-mcp", "display_name": "memory-mcp httproute"}
{"record": "entity", "provider": "kubernetes", "type": "core/Service", "native_id": "mcit-k8s/digital-twin/core/Service/memory-mcp", "display_name": "memory-mcp service"}
{"record": "entity", "provider": "kubernetes", "type": "apps/Deployment", "native_id": "mcit-k8s/digital-twin/apps/Deployment/memory-mcp", "display_name": "memory-mcp", "aliases": ["memory-twin prod", "digital-twin memory-mcp"], "memory_ids": ["d73c22c3-9a7d-4fe2-a5b8-5668341cc9d3", "0eb13387-4e5a-43c2-a4d8-1fbc40d6e1a9"]}
{"record": "entity", "provider": "kubernetes", "type": "apps/StatefulSet", "native_id": "mcit-k8s/digital-twin/apps/StatefulSet/qdrant", "display_name": "qdrant (prod)"}
{"record": "entity", "provider": "azure", "type": "Microsoft.Insights/components", "native_id": "${MCIT_APPI_ID}", "display_name": "appi-memory-twin-mcit"}
{"record": "entity", "provider": "azure", "type": "Microsoft.KeyVault/vaults", "native_id": "${CCTECH_KV_ID}", "display_name": "cctech-keyvault"}
{"record": "entity", "provider": "logical", "type": "repo", "native_id": "midcityit/memory-mcp", "display_name": "memory-mcp repo"}
{"record": "entity", "provider": "logical", "type": "repo", "native_id": "midcityit/infra-tf", "display_name": "infra-tf repo"}
{"record": "entity", "provider": "logical", "type": "jira_issue", "native_id": "MCIT-193", "display_name": "MCIT-193 prod memory-twin migration"}
{"record": "entity", "provider": "logical", "type": "jira_issue", "native_id": "MCIT-251", "display_name": "MCIT-251 decommission ms01 digital-twin"}
{"record": "entity", "provider": "logical", "type": "jira_issue", "native_id": "MCIT-184", "display_name": "MCIT-184 Key Vault CSI on mcit-k8s"}
{"record": "entity", "provider": "kubernetes", "type": "core/Cluster", "native_id": "ms01-k8s/_cluster/core/Cluster/ms01-k8s", "display_name": "ms01-k8s"}
{"record": "entity", "provider": "kubernetes", "type": "apps/Deployment", "native_id": "ms01-k8s/digital-twin/apps/Deployment/memory-mcp", "display_name": "memory-mcp (ms01 warm rollback)", "properties": {"replicas": 0}}
{"record": "entity", "provider": "kubernetes", "type": "core/Service", "native_id": "ms01-k8s/digital-twin/core/Service/memory-mcp", "display_name": "memory-mcp service (ms01)"}
{"record": "entity", "provider": "kubernetes", "type": "apps/StatefulSet", "native_id": "ms01-k8s/digital-twin/apps/StatefulSet/qdrant", "display_name": "qdrant (ms01 warm rollback)"}
{"record": "entity", "provider": "kubernetes", "type": "core/PersistentVolume", "native_id": "ms01-k8s/_cluster/core/PersistentVolume/${MS01_QDRANT_PV}", "display_name": "ms01 qdrant PV /data/qdrant", "properties": {"hostPath": "/data/qdrant"}}
{"record": "edge", "src": "cloudflare:${CF_ACCOUNT_ID_LC}/chriscastrotech.com/dns_record/memory-mcp", "relation": "resolves_to", "dst": "cloudflare:${CF_ACCOUNT_ID_LC}/-/zero_trust_tunnel_cloudflared/homelab"}
{"record": "edge", "src": "cloudflare:${CF_ACCOUNT_ID_LC}/-/zero_trust_tunnel_cloudflared/homelab", "relation": "routes_to", "dst": "kubernetes:mcit-k8s/${MCIT_GATEWAY_NS}/gateway.networking.k8s.io/Gateway/${MCIT_GATEWAY_NAME}", "valid_from": "2026-09-27T07:00:00+00:00", "evidence_memory_ids": ["d73c22c3-9a7d-4fe2-a5b8-5668341cc9d3"]}
{"record": "edge", "src": "cloudflare:${CF_ACCOUNT_ID_LC}/-/zero_trust_tunnel_cloudflared/homelab", "relation": "routes_to", "dst": "kubernetes:ms01-k8s/digital-twin/core/Service/memory-mcp", "valid_from": "2026-04-30T00:00:00+00:00", "valid_to": "2026-09-27T07:00:00+00:00", "retire_reason": "MCIT-193 cutover to mcit-k8s"}
{"record": "edge", "src": "kubernetes:mcit-k8s/${MCIT_GATEWAY_NS}/gateway.networking.k8s.io/Gateway/${MCIT_GATEWAY_NAME}", "relation": "routes_to", "dst": "kubernetes:mcit-k8s/digital-twin/gateway.networking.k8s.io/HTTPRoute/memory-mcp"}
{"record": "edge", "src": "kubernetes:mcit-k8s/digital-twin/gateway.networking.k8s.io/HTTPRoute/memory-mcp", "relation": "routes_to", "dst": "kubernetes:mcit-k8s/digital-twin/core/Service/memory-mcp"}
{"record": "edge", "src": "kubernetes:mcit-k8s/digital-twin/core/Service/memory-mcp", "relation": "routes_to", "dst": "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"}
{"record": "edge", "src": "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp", "relation": "runs_on", "dst": "kubernetes:mcit-k8s/_cluster/core/Node/opi-5", "valid_from": "2026-09-27T07:00:00+00:00"}
{"record": "edge", "src": "kubernetes:mcit-k8s/digital-twin/apps/StatefulSet/qdrant", "relation": "runs_on", "dst": "kubernetes:mcit-k8s/_cluster/core/Node/opi-5"}
{"record": "edge", "src": "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp", "relation": "depends_on", "dst": "kubernetes:mcit-k8s/digital-twin/apps/StatefulSet/qdrant"}
{"record": "edge", "src": "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp", "relation": "depends_on", "dst": "azure:${CCTECH_KV_ID_LC}"}
{"record": "edge", "src": "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp", "relation": "logs_to", "dst": "azure:${MCIT_APPI_ID_LC}"}
{"record": "edge", "src": "kubernetes:mcit-k8s/_cluster/core/Namespace/digital-twin", "relation": "contains", "dst": "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"}
{"record": "edge", "src": "kubernetes:mcit-k8s/_cluster/core/Namespace/digital-twin", "relation": "member_of", "dst": "kubernetes:mcit-k8s/_cluster/core/Cluster/mcit-k8s"}
{"record": "edge", "src": "kubernetes:mcit-k8s/_cluster/core/Node/opi-5", "relation": "member_of", "dst": "kubernetes:mcit-k8s/_cluster/core/Cluster/mcit-k8s"}
{"record": "edge", "src": "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp", "relation": "deployed_by", "dst": "logical:midcityit/memory-mcp"}
{"record": "edge", "src": "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp", "relation": "defined_in", "dst": "logical:midcityit/infra-tf"}
{"record": "edge", "src": "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp", "relation": "tracked_in", "dst": "logical:MCIT-193"}
{"record": "edge", "src": "kubernetes:ms01-k8s/digital-twin/core/Service/memory-mcp", "relation": "routes_to", "dst": "kubernetes:ms01-k8s/digital-twin/apps/Deployment/memory-mcp"}
{"record": "edge", "src": "kubernetes:ms01-k8s/digital-twin/apps/Deployment/memory-mcp", "relation": "runs_on", "dst": "kubernetes:ms01-k8s/_cluster/core/Cluster/ms01-k8s"}
{"record": "edge", "src": "kubernetes:ms01-k8s/digital-twin/apps/Deployment/memory-mcp", "relation": "depends_on", "dst": "kubernetes:ms01-k8s/digital-twin/apps/StatefulSet/qdrant"}
{"record": "edge", "src": "kubernetes:ms01-k8s/digital-twin/apps/StatefulSet/qdrant", "relation": "depends_on", "dst": "kubernetes:ms01-k8s/_cluster/core/PersistentVolume/${MS01_QDRANT_PV}"}
{"record": "edge", "src": "kubernetes:ms01-k8s/_cluster/core/PersistentVolume/${MS01_QDRANT_PV}", "relation": "tracked_in", "dst": "logical:MCIT-251"}
```
`*_LC` variables are the lowercased forms, because Azure and Cloudflare keys are normalized to lowercase. The script derives them; you never set them yourself.

- [ ] **Step 2: Write the vtv seed template**

`scripts/kg_seed/vtv.jsonl.tmpl`. The AKS entity uses the **Terraform alias** type on purpose, so check #6 exercises alias resolution:
```
{"record": "entity", "provider": "azure", "type": "azurerm_kubernetes_cluster", "native_id": "${VTV_AKS_ID}", "display_name": "aks-vtv-prod"}
{"record": "entity", "provider": "logical", "type": "workload", "native_id": "customer-solutions-tools", "display_name": "customer-solutions-tools (CMA Tool)"}
{"record": "entity", "provider": "logical", "type": "jira_issue", "native_id": "VTV-238", "display_name": "VTV-238 customer-solutions-tools-prod to aks-vtv-prod + Key Vault CSI"}
{"record": "edge", "src": "logical:customer-solutions-tools", "relation": "runs_on", "dst": "azure:${VTV_AKS_ID_LC}"}
{"record": "edge", "src": "logical:customer-solutions-tools", "relation": "tracked_in", "dst": "logical:VTV-238"}
{"record": "xref", "src": "vtv::logical:VTV-238", "relation": "pattern_from", "dst": "mcit::logical:MCIT-184", "note": "Key Vault CSI SecretProviderClass pattern first proven on mcit-k8s (MCIT-184), reused for aks-vtv-prod (VTV-238)"}
```

- [ ] **Step 3: Write the failing in-process acceptance test**

`tests/kg/test_acceptance_script.py`:
```python
import importlib.util
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory_mcp.kg.query import KGQuery
from memory_mcp.kg.registry import TypeDef
from memory_mcp.kg.rest import build_router

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("kg_acceptance", ROOT / "scripts" / "kg_acceptance.py")
acc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acc)

ENV = {
    "CF_ACCOUNT_ID": "0123456789ABCDEF0123456789abcdef",
    "MCIT_APPI_ID": "/subscriptions/00000000-0000-0000-0000-00000000000a/resourceGroups/rg-mon/providers/Microsoft.Insights/components/appi-memory-twin-mcit",
    "CCTECH_KV_ID": "/subscriptions/00000000-0000-0000-0000-00000000000a/resourceGroups/rg-kv/providers/Microsoft.KeyVault/vaults/cctech-keyvault",
    "VTV_AKS_ID": "/subscriptions/00000000-0000-0000-0000-00000000000b/resourceGroups/RG-AKS/providers/Microsoft.ContainerService/managedClusters/aks-vtv-prod",
    "MCIT_GATEWAY_NS": "envoy-gateway-system",
    "MCIT_GATEWAY_NAME": "eg",
    "MS01_QDRANT_PV": "qdrant-data",
}


def test_render_seed_lowercases_derived_vars():
    text = acc.render_seed("vtv", ENV)
    assert "azure:/subscriptions/00000000-0000-0000-0000-00000000000b/resourcegroups/rg-aks/" in text
    assert "${" not in text


def test_full_acceptance_in_process(svc, kg_registry, gstore):
    for t, kind in [("Microsoft.Insights/components", "observability"), ("Microsoft.KeyVault/vaults", "secret")]:
        kg_registry.types["azure"][t] = TypeDef("azure", t, kind)
    kg_registry.types["kubernetes"]["core/PersistentVolume"] = TypeDef("kubernetes", "core/PersistentVolume", "storage")
    kg_registry._build_type_index()
    app = FastAPI()
    app.include_router(build_router(svc, KGQuery(kg_registry, gstore), lambda: None))
    results = acc.run(TestClient(app), ENV, check_latency=False)
    failed = [r for r in results if not r[1]]
    assert not failed, failed
    assert len(results) == 8
```

Run: `pytest tests/kg/test_acceptance_script.py -v` and expect FAIL (script missing).

- [ ] **Step 4: Implement `scripts/kg_acceptance.py`**

```python
#!/usr/bin/env python3
"""Seed the mcit/vtv acceptance topology over REST and run the 8 spec §10 checks.

Env: KG_BASE_URL, MEMORY_TWIN_BEARER, plus the seed variables in the plan (CF_ACCOUNT_ID, MCIT_APPI_ID,
CCTECH_KV_ID, VTV_AKS_ID, MCIT_GATEWAY_NS, MCIT_GATEWAY_NAME, MS01_QDRANT_PV). Exit 0 iff all checks pass.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path
from string import Template
from typing import Mapping
from urllib.parse import quote

import httpx

SEED_DIR = Path(__file__).resolve().parent / "kg_seed"
REQUIRED = ("CF_ACCOUNT_ID", "MCIT_APPI_ID", "CCTECH_KV_ID", "VTV_AKS_ID", "MCIT_GATEWAY_NS",
            "MCIT_GATEWAY_NAME", "MS01_QDRANT_PV")


def _vars(env: Mapping[str, str]) -> dict[str, str]:
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}")
    v = {k: env[k] for k in REQUIRED}
    v["CF_ACCOUNT_ID_LC"] = env["CF_ACCOUNT_ID"].lower()
    for k in ("MCIT_APPI_ID", "CCTECH_KV_ID", "VTV_AKS_ID"):
        v[f"{k}_LC"] = env[k].lower().rstrip("/")
    return v


def render_seed(name: str, env: Mapping[str, str]) -> str:
    return Template((SEED_DIR / f"{name}.jsonl.tmpl").read_text()).substitute(_vars(env))


def _keys(nodes) -> set[str]:
    return {n["key"] for n in nodes}


def run(client: httpx.Client, env: Mapping[str, str], check_latency: bool = True) -> list[tuple[str, bool, str]]:
    v = _vars(env)
    cf = v["CF_ACCOUNT_ID_LC"]
    DNS = f"cloudflare:{cf}/chriscastrotech.com/dns_record/memory-mcp"
    TUN = f"cloudflare:{cf}/-/zero_trust_tunnel_cloudflared/homelab"
    GW = f"kubernetes:mcit-k8s/{v['MCIT_GATEWAY_NS']}/gateway.networking.k8s.io/Gateway/{v['MCIT_GATEWAY_NAME']}"
    DEP = "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"
    OPI = "kubernetes:mcit-k8s/_cluster/core/Node/opi-5"
    MS01SVC = "kubernetes:ms01-k8s/digital-twin/core/Service/memory-mcp"
    PV = f"kubernetes:ms01-k8s/_cluster/core/PersistentVolume/{v['MS01_QDRANT_PV']}"
    AKS = f"azure:{v['VTV_AKS_ID_LC']}"
    out: list[tuple[str, bool, str]] = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:  # report, don't abort the remaining checks
            ok, detail = False, f"{type(e).__name__}: {e}"
        out.append((name, bool(ok), detail))

    def imp(graph):
        r = client.post(f"/kg/{graph}/import", content=render_seed(graph, env))
        r.raise_for_status()
        return r.json()

    def export_count(graph):
        return sum(1 for ln in client.get(f"/kg/{graph}/export").text.splitlines() if ln.strip())

    seeded = [imp("mcit"), imp("vtv")]
    if any(s["status"] != "ok" for s in seeded) or seeded[1]["xrefs"]["errors"]:
        raise SystemExit(f"seed failed: {json.dumps(seeded)[:2000]}")

    def c1():
        r = client.post("/kg/mcit/impact", json={"key": OPI}).json()
        aff = {n["key"] for layer in r["affected"] for n in layer["nodes"]}
        return DEP in aff and DNS in aff, f"affected={len(aff)}"
    check("1 impact(opi-5) reaches deployment and public hostname", c1)

    def c2():
        r = client.post("/kg/mcit/path", json={"src": DNS, "dst": DEP}).json()
        nodes = [n["key"] for n in r["paths"][0]["nodes"]] if r["found"] else []
        return nodes[:3] == [DNS, TUN, GW] and nodes[-1] == DEP and len(nodes) == 6, " -> ".join(n.split("/")[-1] for n in nodes)
    check("2 path(hostname -> deployment)", c2)

    def c3():
        body = {"start": TUN, "direction": "out", "max_depth": 1}
        past = _keys(client.post("/kg/mcit/traverse", json={**body, "as_of": "2026-09-20"}).json()["nodes"])
        now = _keys(client.post("/kg/mcit/traverse", json=body).json()["nodes"])
        return MS01SVC in past and GW not in past and GW in now and MS01SVC not in now, "as_of 2026-09-20 vs now"
    check("3 as_of shows ms01 routing before MCIT-193 cutover", c3)

    def c4():
        r = client.post("/kg/mcit/impact", json={"key": PV}).json()
        aff = {n["key"] for layer in r["affected"] for n in layer["nodes"]}
        bad = {k for k in aff if "mcit-k8s/" in k} | ({DNS} & aff)
        return bool(aff) and not bad, f"affected={sorted(k.split('/')[-1] for k in aff)}"
    check("4 ms01 PV blast radius is ms01-only (MCIT-251 evidence)", c4)

    def c5():
        r = client.post("/kg/vtv/impact", json={"key": AKS})  # warm-up / sanity: AKS exists
        r.raise_for_status()
        rel = client.get(f"/kg/vtv/entities/{quote('logical:VTV-238', safe='')}").json()
        dsts = {x["dst"] for x in rel["xrefs"]}
        return "mcit::logical:MCIT-184" in dsts, f"xrefs={sorted(dsts)}"
    check("5 VTV-238 references MCIT-184 pattern", c5)

    def c6():
        bad = json.dumps({"record": "entity", "provider": "azure", "type": "Microsoft.Compute/virtualMachines",
                          "native_id": env["MCIT_APPI_ID"].rsplit("/providers/", 1)[0]
                          + "/providers/Microsoft.Network/networkInterfaces/nic-acceptance"})
        r = client.post("/kg/mcit/import", content=bad)
        mismatch = r.status_code == 422 and r.json()["detail"]["errors"][0]["error"] == "type_id_mismatch"
        t = client.get(f"/kg/vtv/entities/{quote(AKS, safe='')}").json()["entity"]["type"]
        return mismatch and t == "Microsoft.ContainerService/managedClusters", f"alias->{t}"
    check("6 type/ID mismatch rejected; TF alias resolved", c6)

    def c7():
        before = (export_count("mcit"), export_count("vtv"))
        again = [imp("mcit"), imp("vtv")]
        statuses = {w["status"] for s in again for w in s["written"]}
        after = (export_count("mcit"), export_count("vtv"))
        return statuses <= {"updated", "retired", "recorded"} and before == after, f"statuses={sorted(statuses)} lines={after}"
    check("7 re-seed is idempotent", c7)

    def c8():
        if not check_latency:
            return True, "skipped (in-process)"
        samples = []
        for _ in range(30):
            t0 = time.perf_counter()
            client.post("/kg/mcit/traverse", json={"start": DEP, "max_depth": 3}).raise_for_status()
            samples.append((time.perf_counter() - t0) * 1000)
        p95 = statistics.quantiles(samples, n=20)[18]
        return p95 < 150, f"p95={p95:.1f}ms (RSS: check `kubectl top pod` separately)"
    check("8 traverse depth-3 p95 < 150 ms", c8)
    return out


def main() -> int:
    base, token = os.environ.get("KG_BASE_URL"), os.environ.get("MEMORY_TWIN_BEARER")
    if not base or not token:
        print("set KG_BASE_URL and MEMORY_TWIN_BEARER", file=sys.stderr)
        return 2
    with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30) as c:
        results = run(c, os.environ)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  [{detail}]")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the in-process acceptance**

Run: `pytest tests/kg/test_acceptance_script.py -v && pytest -v`
Expected: 2 passed; then the full suite passes.

- [ ] **Step 6: Apply the spec amendments and the README section**

In `docs/superpowers/specs/2026-10-03-knowledge-graph-design.md`:
- §5.1: add to `kg_link` "optional `valid_from` for backfill", and to `kg_batch` "edge items accept `valid_from`, `valid_to`, `retire_reason`; entity items accept `valid_from`, `valid_to`".
- §5.2: in the `kg_traverse` row, remove `cursor?` and add "over budget → `truncated: true`; narrow with `relations`/`kinds`/`max_depth`". In the `kg_impact` row, set the default to `max_depth=6`.
- §2.4: kubernetes native ID becomes `{cluster}/{namespace or _cluster}/{group}/{Kind}/{name}` with `core` as the core group.
- §8: NDJSON line marker `record` (`entity`/`edge`/`xref`).
- §10: replace checks #3 and #4 with the restated versions from this plan's "Spec amendments" item 5. Also note amendment 6 (no Protocol class) in §6.

In `README.md`, add a "Knowledge graph (KG_ENABLED)" section listing the four env vars from spec §9, the 15 tool names with one-line purposes (copy the tool docstrings), and the agent rules from spec §11.5.

Commit:
```bash
git add scripts/kg_seed scripts/kg_acceptance.py tests/kg/test_acceptance_script.py docs/superpowers/specs README.md
git commit -m "feat(kg): acceptance seed + script; spec amendments; README (RCL-38)"
```

- [ ] **Step 7: Ship to dev** (outward-facing: get explicit approval before each bullet)

1. `git push -u origin feat/knowledge-graph`, then open a PR into `dev`. CI (`pytest -v`) must pass. Merge after review; `publish.yml` builds `ghcr.io/midcityit/memory-mcp:dev` (amd64 + arm64).
2. In `midcityit/infra-tf`, open a PR that adds `KG_ENABLED = "true"` to the **digital-twin-dev** memory-mcp container env (dev only; prod is untouched until RCL-39). Merge, apply via CI, and confirm the pod restarted on the new `:dev` digest.
3. **Backup coverage:** open the Qdrant backup CronJob script in infra-tf. If it snapshots only `collections/memories`, replace that single call with a loop over all collections:
   ```sh
   for c in $(curl -s "$QDRANT_URL/collections" | jq -r '.result.collections[].name'); do
     curl -sf -X POST "$QDRANT_URL/collections/$c/snapshots" || exit 1
   done
   ```
   Keep the existing upload step but apply it to every snapshot produced. Ship that in the same infra-tf PR or a follow-up one.
4. Server smoke test (verifies vectorless collections on Qdrant v1.13.6): `curl -sf -H "Authorization: Bearer $MEMORY_TWIN_BEARER" "$KG_BASE_URL/kg/graphs"` returns `mcit` and `vtv`, and the pod logs show no collection-creation errors.

- [ ] **Step 8: Run acceptance against dev and record evidence**

```bash
# dev hostname from the digital-twin-dev HTTPRoute
export KG_BASE_URL="https://$(kubectl --context mcit-k8s get httproute -n digital-twin-dev -o jsonpath='{.items[0].spec.hostnames[0]}')"
# dev token secret: find its name once with
#   az keyvault secret list --vault-name cctech-keyvault --query "[?contains(name,'memory')].name" -o tsv
# then (value is never printed):
export MEMORY_TWIN_BEARER="$(az keyvault secret show --vault-name cctech-keyvault --name "$DEV_TOKEN_SECRET_NAME" --query value -o tsv)"
export CF_ACCOUNT_ID=... MCIT_APPI_ID=... CCTECH_KV_ID=... VTV_AKS_ID=... MCIT_GATEWAY_NS=... MCIT_GATEWAY_NAME=... MS01_QDRANT_PV=...
python scripts/kg_acceptance.py | tee /tmp/kg_acceptance_dev.txt
kubectl --context mcit-k8s top pod -n digital-twin-dev
```
Expected: 8 `PASS` lines; memory-mcp RSS at least 25% below the 2Gi limit. Post the output (no secrets) and the `kubectl top` line as a checkpoint comment on RCL-38. Transition RCL-31…RCL-38 and RCL-41 to Done, and update the memory-twin project memory `memory-mcp-knowledge-graph` (source_repo `memory-mcp`) with the result.


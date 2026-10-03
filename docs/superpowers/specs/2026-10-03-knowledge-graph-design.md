# memory-mcp Knowledge Graph — Design Spec

**Date:** 2026-10-03
**Status:** Approved design, pending spec review
**Repo:** midcityit/memory-mcp (branch `feat/knowledge-graph`)
**Tracking:** RCL (new epic, to be created)
**Author:** Claude Code (with Chris Castro)

---

## 1. Purpose

memory-twin is the shared knowledge layer for every agent in the fleet: Claude Code, Kiro, Copilot and OpenClaw. It stores about 715 prose memories that already describe infrastructure: clusters, nodes, namespaces, repos, PRs and Jira keys. The relationships between those things exist only as text, though. Semantic search finds similar wording, but it cannot answer structural questions:

- "What breaks if `opi-5` goes down?"
- "How does `memory-mcp.chriscastrotech.com` reach its pod?"
- "Is anything still attached to ms01-k8s's Qdrant PV?" (MCIT-251)
- "Have we solved something like this VTV problem before in MCIT?"

This feature adds **independent, cross-queryable knowledge graphs** of cloud and infrastructure topology. Agents write them explicitly, they link to existing memories, and they keep a history of how the topology changed.

### Success criteria

An agent can, through MCP tools:
1. Record verified topology (entities and relationships) idempotently, with server-side validation that catches typos and misfiled resources.
2. Get structured answers to blast-radius, path, neighborhood, orphan and "what did it look like on date X" questions.
3. Keep Veritiv (`vtv`) and Mid City IT (`mcit`) graphs separate, while still finding patterns and lessons across them.
4. Move between the graph and the existing memories in both directions.

The acceptance scenario in §10 is the definition of done.

### Decisions made during brainstorming

| Topic | Decision |
|---|---|
| How data gets in | **Agents write it** through explicit tools. No LLM extraction, no live ingestion in v1 |
| Schema strictness | **Controlled and extensible**: server-side registry, unknown values rejected with suggestions, extended through config |
| History | **Soft-retire**: `valid_from` / `valid_to` on edges and entities; queries default to now and support `as_of` |
| Storage | **Qdrant-native** collections behind a `GraphStore` interface (no new infrastructure) |
| Type coverage | **Every resource type** for Azure, AWS, GCP, VMware, Hyper-V and Cloudflare, plus Kubernetes and logical types, using provider-native types from generated catalogs |
| Observability & inventory (amended 2026-10-03) | **Grafana, Prometheus and NetBox are first-class providers**, keyed by their own native IDs; self-hosted and managed (Azure/AWS) instances use the same model. Managed services (CloudWatch, Azure Monitor/Managed Grafana/Prometheus, AWS Managed Prometheus/Grafana) come from the cloud catalogs as `observability`. NetBox sync stays out of v1 |
| Multiple graphs | **Independent graphs** (`vtv`, `mcit`, …), physically isolated, queryable together, linked only by reference-class cross-graph links |
| Access control | **Organizational boundary only in v1**: one API token, as today |
| OpenTrace (opentrace/opentrace) | Evaluated and **rejected**: it is a read-only, per-repo code graph with no infra ontology and no shared write path |

---

## 2. Data model

### 2.1 Graphs

Graphs are declared in `kg_graphs.yaml`. Adding one requires no code change.

```yaml
graphs:
  mcit: { name: "Mid City IT", description: "MCIT internal, homelab, chriscastrotech" }
  vtv:  { name: "Veritiv",     description: "Veritiv cloud infra (AWS/Azure/GCP)" }
```

Each graph `<g>` gets its own Qdrant collections, `kg_<g>_entities` and `kg_<g>_edges`. Cross-graph references live in one shared `kg_xrefs` collection. Physical separation means one missing filter can never mix graphs, and a graph can be exported, deleted or handed over as a unit.

Graph IDs must match `^[a-z][a-z0-9_]{1,31}$`.

### 2.2 Entity (`kg_<g>_entities`)

| Field | Type | Notes |
|---|---|---|
| `key` | str | `{provider}:{native_id}`, unique within the graph. The point ID is `uuid5(NAMESPACE, key)` |
| `provider` | str | `azure`, `aws`, `gcp`, `vmware`, `hyperv`, `cloudflare`, `kubernetes`, `grafana`, `prometheus`, `netbox`, `logical` |
| `type` | str | Provider-native type, as resolved from the catalog (§3) |
| `kind` | str | Cross-cloud category, **derived** from the type and never supplied by agents |
| `native_id` | str | Normalized native identifier (§2.4) |
| `display_name` | str | Human name, e.g. `aks-vtv-prod` |
| `aliases` | list[str] | Merged as a union on upsert |
| `properties` | dict | Shallow, JSON-serializable. Merged on upsert; a value of `null` deletes the key |
| `memory_ids` | list[str] | Links to records in `memories`. Merged as a union |
| `agent` | str | Last writer |
| `created_at`, `updated_at` | ISO-8601 | |
| `valid_from` | ISO-8601 | Creation time unless supplied |
| `valid_to` | ISO-8601 \| null | Set when the entity is retired |
| vector | 384-d | `embed(display_name + " " + " ".join(aliases) + " " + type)` |

Payload indexes: `key`, `provider`, `kind`, `type`, `memory_ids`, `valid_to`.

**Kinds:** `compute`, `container`, `network`, `dns`, `edge`, `database`, `storage`, `identity`, `security`, `secret`, `observability`, `messaging`, `analytics`, `ai`, `integration`, `org`, `host`, `work` (Jira, Confluence, repos, pipelines), `inventory` (NetBox records: the record, not the device), `other`.

### 2.3 Edge (`kg_<g>_edges`)

| Field | Type | Notes |
|---|---|---|
| `src`, `dst` | str | Entity keys in the same graph |
| `relation` | str | From the relation registry (§4) |
| `properties` | dict | e.g. `{"port": 30800}` |
| `evidence_memory_ids` | list[str] | Memories that justify this edge |
| `agent`, `created_at` | | |
| `valid_from` | ISO-8601 | |
| `valid_to` | ISO-8601 \| null | `null` means the edge is current |
| `retire_reason` | str \| null | |

The point ID is `uuid5(NAMESPACE, f"{src}|{relation}|{dst}|{valid_from}")`. Edges have no vector; the collection is created with an empty vectors config. If the installed Qdrant version rejects that, a 1-dimensional placeholder vector is used instead (plan step).

Payload indexes: `src`, `dst`, `relation`, `valid_to`.

Each edge is stored once, in its canonical direction. The registry supplies an inverse label for reads.

### 2.4 Native identity

| Provider | `native_id` | Normalization |
|---|---|---|
| azure | Full ARM resource ID | lowercase; trailing `/` stripped |
| aws | ARN | as-is (ARNs are case-sensitive) |
| gcp | Full resource name, `//service.googleapis.com/...` | as-is |
| vmware | `{vcenter_fqdn}/{inventory_path}` (not the moref) | lowercase FQDN |
| hyperv | `{host_or_cluster_fqdn}/{object_type}/{name}` | lowercase FQDN |
| cloudflare | `{account_id}/{zone or -}/{object_type}/{id_or_name}` | lowercase |
| kubernetes | `{cluster}/{namespace or _cluster}/{group/kind}/{name}` | as-is |
| grafana | `{grafana_host}/{type}/{uid}` (`Instance` uses the host as uid) | lowercase host |
| prometheus | `{prometheus_host}/{type}/{name}` | lowercase host |
| netbox | `{netbox_host}/{app.model}/{id}` (numeric id) | lowercase host |
| logical | Per-type convention: Jira key `MCIT-193`, repo `org/repo`, Confluence `space/page_id`, etc. | per-type regex |

`ids.py` validates the format per provider. For Azure, AWS and GCP it also checks that **the type embedded in the ID matches the declared type**, for example rejecting an ARM ID containing `/networkInterfaces/` filed as `Microsoft.Compute/virtualMachines`.

### 2.5 Cross-graph reference (`kg_xrefs`)

| Field | Notes |
|---|---|
| `src`, `dst` | Fully qualified keys: `{graph}::{provider}:{native_id}` |
| `relation` | Reference-class only: `pattern_from`, `lessons_from`, `similar_to`, `supersedes_approach_of` |
| `note` | Required free text: why they are related |
| `evidence_memory_ids`, `agent`, `created_at`, `valid_to` | |
| `dangling` | bool; set when an endpoint's graph or entity is deleted |

---

## 3. Type catalogs

Agents never type `kind`. The server derives it from a catalog entry.

`scripts/build_kg_catalog.py` generates `src/memory_mcp/kg/catalog/<provider>.json`:

| Provider | Source | Method |
|---|---|---|
| azure | ARM provider registry (`az provider list --expand resourceTypes`) | generated |
| aws | CloudFormation resource specification (`AWS::*::*`) | generated |
| gcp | Cloud Asset Inventory supported asset types | generated |
| cloudflare | Terraform provider schema, `cloudflare_` prefix stripped | generated |
| vmware | vSphere managed object types (`VirtualMachine`, `HostSystem`, `ClusterComputeResource`, `Datastore`, `DistributedVirtualPortgroup`, `Datacenter`, `ResourcePool`, `VirtualApp`, `Network`, `Folder`) + NSX (`Segment`, `Tier0Gateway`, `Tier1Gateway`) | curated |
| hyperv | `Host`, `VM`, `VMSwitch`, `VHD`, `Checkpoint`, `FailoverCluster`, `ClusterSharedVolume` | curated |
| kubernetes | Core and common group/kinds; CRDs accepted by the pattern `^[a-z0-9.-]+/[A-Z][A-Za-z0-9]+$` | curated + pattern |
| grafana | `Instance`, `Folder`, `Dashboard`, `Datasource`, `AlertRule`, `ContactPoint`, `NotificationPolicy` (aliases from the `grafana/grafana` Terraform provider) | curated |
| prometheus | `Server`, `ScrapeJob`, `RuleGroup`, `Alertmanager`, `Receiver`, `RemoteWrite` (aliases from Prometheus-operator CRD kinds) | curated |
| netbox | `dcim.site`, `dcim.rack`, `dcim.device`, `dcim.interface`, `ipam.prefix`, `ipam.ipaddress`, `ipam.vlan`, `ipam.vrf`, `virtualization.cluster`, `virtualization.virtualmachine`, `tenancy.tenant`, `circuits.circuit` (all kind `inventory`; aliases from the `e-breuninger/netbox` provider) | curated |
| logical | `workload`, `repo`, `pipeline`, `tf_workspace`, `jira_issue`, `confluence_page`, `person`, `agent`, `site` | curated |

Catalog entry shape: `{"type": "...", "kind": "...", "aliases": ["azurerm_kubernetes_cluster", ...]}`.

- **Kind mapping** for generated catalogs uses an ordered table of prefix and namespace rules kept in the script (e.g. `Microsoft.Network/dnsZones*` → `dns`, `Microsoft.Network/*` → `network`, `AWS::RDS::*` → `database`, `AWS::(APS|Grafana|CloudWatch|Logs)::*` → `observability`). Unmatched types become `other`, and the build reports them.
- **Terraform alias maps** are attached to entries: `azurerm_*`, `aws_*`, `google_*`, `vsphere_*`, `cloudflare_*`, and `hyperv_*` (taliesins provider).
- A CI workflow can rebuild the catalogs. Committed catalogs are the source of truth at runtime.

**Type resolution order:** exact native type → Terraform alias → case-insensitive native match → error `unknown_type` with up to 3 `difflib` suggestions.

---

## 4. Relation registry (`kg_relations.yaml`)

Each relation declares `inverse`, `class` (`topology` | `reference`), `impact` (`propagates` | `none`), and optional `src_kinds` / `dst_kinds`.

| Relation | Inverse | Impact | Typical use |
|---|---|---|---|
| `runs_on` | `runs` | propagates | workload → cluster/node/host |
| `hosted_on` | `hosts` | propagates | VM → ESXi/Hyper-V host/node |
| `member_of` | `has_member` | none | resource → RG/account/project/datacenter |
| `contains` | `contained_in` | none | namespace → workload |
| `depends_on` | `dependency_of` | propagates | generic dependency |
| `in_network` | `network_for` | propagates | resource → subnet/VPC/portgroup/vSwitch |
| `attached_to` | `has_attached` | propagates | disk/NIC/EIP → VM |
| `routes_to` | `routed_from` | propagates | gateway/route → service |
| `resolves_to` | `resolved_from` | propagates | DNS record → endpoint |
| `exposed_via` | `exposes` | propagates | service → LB/App GW/tunnel/ingress |
| `secured_by` | `secures` | none | → NSG/SG/firewall/WAF/Access policy |
| `encrypted_by` | `encrypts` | propagates | → Key Vault key/KMS key |
| `authenticates_as` | `identity_for` | propagates | workload → managed identity/role |
| `has_role_on` | `grants_role_to` | none | identity → resource |
| `connects_to` | `connected_from` | none | |
| `peers_with` | `peers_with` | none | symmetric |
| `replicates_to` | `replicated_from` | none | |
| `backs_up_to` | `backup_of` | none | |
| `logs_to` | `receives_logs_from` | none | |
| `deployed_by` | `deploys` | none | → pipeline/repo |
| `defined_in` | `defines` | none | → repo/tf_workspace |
| `managed_by` | `manages` | none | → person/agent/tool |
| `tracked_in` | `tracks` | none | → jira_issue |
| `documented_in` | `documents` | none | → confluence_page |
| `pattern_from` | `pattern_for` | none | **reference class, `kg_xref` only** |
| `lessons_from` | `lessons_for` | none | reference class |
| `similar_to` | `similar_to` | none | reference class, symmetric |
| `supersedes_approach_of` | `approach_superseded_by` | none | reference class |
| `monitors` | `monitored_by` | none | scrape job / alarm / alert rule → monitored target |
| `sends_metrics_to` | `receives_metrics_from` | none | workload/collector → metrics backend (e.g. otel-collector → `amw-mcit`) |
| `sends_traces_to` | `receives_traces_from` | none | workload/collector → trace backend |
| `queries` | `queried_by` | propagates | dashboard / alert rule → datasource or workspace |
| `reads_from` | `read_by` | propagates | Grafana datasource → Prometheus / Azure Monitor workspace / Log Analytics / CloudWatch |
| `notifies` | `notified_by` | propagates | alert rule → contact point / receiver / action group / SNS topic |
| `served_by` | `serves` | propagates | Grafana/Prometheus `Instance`/`Server` → its k8s workload or managed cloud resource |
| `visualizes` | `visualized_by` | none | dashboard → the entities it charts |
| `recorded_in` | `records` | none | any resource → its NetBox record (`dst_kinds: [inventory]`) |

**Observability queries enabled by these relations** (no new tools): `kg_impact(<metrics workspace>)` lists the datasources, dashboards and alert rules that go blind; `kg_find(kind=..., missing_relation="monitors", direction="in")` lists unmonitored entities (observability gaps); `kg_get_entity(<resource>)` shows its monitors, dashboards and NetBox record.

`impact: propagates` means that if the **dst** fails, the **src** is affected. `kg_impact` walks these relations in reverse.

Kind constraints are declared for the topology relations where they are unambiguous (e.g. `hosted_on: src_kinds [compute, container], dst_kinds [compute, host]`). Relations without constraints accept any kind.

---

## 5. MCP tools

All tools are prefixed `kg_` and registered next to the existing 4 memory tools, which do not change. That makes 15 new tools.

### 5.1 Write tools (`graph` required, idempotent)

| Tool | Behavior |
|---|---|
| `kg_upsert_entity(graph, provider, type, native_id, display_name, aliases=[], properties={}, memory_ids=[], agent="claude-code")` | Create or update. Returns `{key, status: created\|updated, type, kind, possible_duplicates?}` |
| `kg_link(graph, src, relation, dst, properties={}, evidence_memory_ids=[], agent)` | Both endpoints must exist and be current. If the same `(src, relation, dst)` edge is already current, it is updated in place; otherwise a new edge is created with `valid_from=now` |
| `kg_unlink(graph, src, relation, dst, reason)` | Sets `valid_to=now` and `retire_reason` on the current edge. Error `not_found` if there is none |
| `kg_retire_entity(graph, key, reason)` | Retires the entity and all of its current edges, both directions |
| `kg_batch(graph, entities=[], edges=[], agent)` | Validates every item first; on any validation error nothing is written and every error is returned. Then writes entities, then edges, where edge endpoints may refer to entities in the same batch. If a write fails partway, returns `{written: [...], failed_at, error}`; retrying is safe |
| `kg_xref(src, relation, dst, note, evidence_memory_ids=[], agent)` | Fully qualified keys; reference-class relations only; both endpoints must exist |

**Validation pipeline** (also applied per item in a batch): graph exists → provider known → type resolves (§3) → native ID valid, normalized and consistent with the type (§2.4) → relation known and of the right class → src/dst kinds satisfy constraints → endpoints exist, are current, and are in the same graph.

**Error shape:** `{"error": "<code>", "message": "...", "field": "...", "suggestions": [...]}`. Codes: `unknown_graph`, `unknown_provider`, `unknown_type`, `invalid_native_id`, `type_id_mismatch`, `unknown_relation`, `relation_class_mismatch`, `kind_constraint`, `endpoint_not_found`, `cross_graph_edge`, `not_found`.

**Duplicate hint:** when an upsert creates a new key, the server runs a vector search over the graph's current entities of the same `kind`. Matches with cosine similarity ≥ 0.90 (configurable via `KG_DUP_THRESHOLD`) come back as `possible_duplicates`. This never blocks the write.

**Concurrency:** properties are last-writer-wins. Alias and memory_id unions are read-modify-write, so a rare lost alias under a concurrent write is an accepted risk.

### 5.2 Read tools

`graphs` accepts a single ID, a list, or `"*"`.

| Tool | Returns |
|---|---|
| `kg_resolve(query, graphs="*", provider?, kind?, type?, include_retired=false, limit=10)` | Ranked `{graph, key, display_name, type, kind, score, retired}`. An exact match on key, native_id or alias gets score 1.0 and ranks first |
| `kg_get_entity(graph, key, as_of?, include_history=false)` | Full entity; edges in both directions grouped by relation (inverse label for incoming edges); memory refs `{id, name, type}`; xrefs; with `include_history`, a timeline of edge open/close events |
| `kg_find(graph, provider?, kind?, type?, missing_relation?, direction="out", include_retired=false, limit=100, cursor?)` | Paged compact entities with `has_more`. `missing_relation` returns entities that lack a current edge of that relation (orphan finder) |
| `kg_traverse(graph, start, direction="both", relations?, kinds?, max_depth=3, as_of?, follow_xrefs=false, limit=200, cursor?)` | BFS subgraph `{nodes, edges, has_more, truncated}`. `max_depth` capped at 6 |
| `kg_path(graph, src, dst, relations?, max_depth=6)` | Up to 3 shortest paths (bidirectional BFS, undirected over the allowed relations) |
| `kg_impact(graph, key, max_depth=4, as_of?)` | Reverse walk over `propagates` relations, grouped by depth and kind, plus `tracked_in` / `documented_in` targets of affected nodes |
| `kg_overview(graphs="*")` | Per graph: counts by kind and provider, top 10 hubs by current degree, entities changed in the last 7 days, dangling xref count. Target < 500 tokens |
| `kg_related_across(graph, key, target_graphs="*", limit=10)` | Explicit xrefs touching the entity, plus same-`kind` similar entities in other graphs (vector similarity), each with linked memory refs filtered to types `decision`, `troubleshooting`, `runbook`, `architecture` |
| `kg_for_memory(memory_id, graphs="*")` | Entities and edges that reference the memory (`memory_ids` / `evidence_memory_ids`) |

### 5.3 Time semantics

An edge or entity is **current at `t`** when `valid_from ≤ t` and (`valid_to` is null or `valid_to > t`). Every read defaults to `t = now`. `include_retired=true` drops the time filter entirely.

### 5.4 Response budget

- Compact node form: `{graph?, key, display_name, kind, type_short}`.
- Default `max_chars` is 6000 (overridable per call).
- When over budget, results are trimmed deepest-layer-first for traversals and from the tail for lists; the response gets `truncated: true` and a `cursor`, and is always valid JSON.

---

## 6. Algorithms (`kg/query.py`)

Algorithms depend only on the `GraphStore` interface:

```python
class GraphStore(Protocol):
    def get_entities(self, graph: str, keys: list[str]) -> list[Entity]: ...
    def upsert_entity(self, graph: str, e: Entity) -> Entity: ...
    def search_entities(self, graph: str, vector: list[float], filters: EntityFilter, limit: int) -> list[tuple[Entity, float]]: ...
    def scroll_entities(self, graph: str, filters: EntityFilter, limit: int, cursor: str | None) -> tuple[list[Entity], str | None]: ...
    def edges_from(self, graph: str, keys: list[str], relations: list[str] | None, at: datetime | None) -> list[Edge]: ...
    def edges_to(self, graph: str, keys: list[str], relations: list[str] | None, at: datetime | None) -> list[Edge]: ...
    def put_edge(self, graph: str, e: Edge) -> Edge: ...
    def current_edge(self, graph: str, src: str, relation: str, dst: str) -> Edge | None: ...
    def all_current_edges(self, graph: str) -> Iterable[Edge]: ...
    # + xref and admin methods
```

- **One BFS hop** is one `edges_from` / `edges_to` call. On Qdrant that is a single `scroll` with `src`/`dst` `MatchAny(frontier)`, a relation filter, and the time filter. Frontiers larger than 500 keys are chunked.
- **Time filter on Qdrant:** `valid_from ≤ t` AND (`valid_to` IsNull OR `valid_to > t`). Timestamps are also stored as epoch-second floats (`valid_from_ts`, `valid_to_ts`) so range filters work.
- **Hubs** are computed from `all_current_edges`, cached for 60 s per worker per graph.
- `QdrantGraphStore` is the only implementation in v1. The interface exists so a FalkorDB backend could replace it later without changing the tools.

---

## 7. Code structure

```
src/memory_mcp/
  embedding.py          # NEW — extracted from store.py: SentenceTransformer + LRU cache, shared
  store.py              # CHANGED — uses embedding.py; behavior unchanged
  mcp_tools.py          # CHANGED — _init(store, graph_store=None); registers kg tools when enabled
  server.py             # CHANGED — builds registry + QdrantGraphStore when KG_ENABLED; mounts kg router
  config.py             # CHANGED — kg_enabled, kg_config_dir, kg_dup_threshold
  kg/
    __init__.py
    models.py           # Entity, Edge, XRef dataclasses; FQ key helpers
    registry.py         # graphs, relations, catalogs; type resolution + suggestions
    ids.py              # per-provider native-ID validation/normalization/type extraction
    store.py            # GraphStore Protocol + QdrantGraphStore
    service.py          # validation pipeline + write operations (used by tools and REST)
    query.py            # resolve, traverse, path, impact, overview, related_across, budget trimming
    tools.py            # MCP tool bindings (thin)
    rest.py             # FastAPI router (thin)
    config/kg_graphs.yaml
    config/kg_relations.yaml
    catalog/{azure,aws,gcp,cloudflare,vmware,hyperv,kubernetes,logical}.json
scripts/build_kg_catalog.py
tests/kg/...
```

Tools and REST both call `service.py` and `query.py`, so the two surfaces cannot drift apart.

---

## 8. REST API

All routes require the bearer token, like the existing routes.

| Method | Route | Purpose |
|---|---|---|
| GET | `/kg/graphs` | Registry listing |
| GET | `/kg/overview?graphs=` | Same as `kg_overview` |
| POST | `/kg/resolve` | Same as `kg_resolve` |
| GET | `/kg/{graph}/entities/{key:path}` | Same as `kg_get_entity`. Keys contain `/` (ARM IDs, k8s paths), so the route uses FastAPI's `path` converter and clients URL-encode the key |
| POST | `/kg/{graph}/traverse` · `/path` · `/impact` | Same as the tools |
| GET | `/kg/{graph}/export` | JSONL stream (entities, edges, and xrefs touching the graph) |
| POST | `/kg/{graph}/import` | JSONL; validated; idempotent |
| DELETE | `/kg/{graph}/entities/{key:path}?hard=true` | Admin hard delete of an entity and its edges; marks affected xrefs `dangling` |

---

## 9. Configuration, operations and telemetry

| Env var | Default | Purpose |
|---|---|---|
| `KG_ENABLED` | `false` | Feature flag. When false, no kg tools, routes or collections. Keeps Ziese unaffected |
| `KG_CONFIG_DIR` | packaged `kg/config` + `kg/catalog` | ConfigMap override for graphs, relations and catalogs |
| `KG_DUP_THRESHOLD` | `0.90` | Duplicate-hint similarity |
| `KG_MAX_CHARS` | `6000` | Default response budget |

**Startup:** for each registered graph, ensure the collections and payload indexes exist (idempotent, like `_ensure_collection`).

**Telemetry** (existing OTel → App Insights):
- `kg_writes_total{graph,op}`
- `kg_validation_errors_total{code}`
- `kg_query_duration_seconds{tool,graph}`
- observable gauge `kg_entities{graph}`

**Memory:** catalogs load once per worker. Peak RSS is measured on dev and must stay at least 25% below the 2Gi limit with both workers running.

**Backups:** confirm whether the existing daily Qdrant backup CronJob (→ Azure Blob `cctechinttf/memory-backups`) snapshots all collections. If it covers only `memories`, extend it in infra-tf. `/kg/{graph}/export` provides a logical backup and a handover path.

---

## 10. Testing and acceptance

### Automated
- **Unit:** type resolution (exact, alias, case-insensitive, suggestions); `ids.py` table tests per provider with realistic IDs, including type/ID mismatches; relation class and kind constraints; budget trimming.
- **Store and algorithms:** run against `QdrantClient(":memory:")` with a deterministic stub embedder, so tests download no model. The first plan task confirms local mode supports `IsNull`, `MatchAny`, range filters and vectorless collections; if it doesn't, those tests use a Qdrant service container in CI.
- **Service:** batch all-or-nothing validation, idempotent re-run, partial-failure report, retire cascades, soft-retire then re-link history.
- **MCP integration:** call the kg tools through an in-process MCP client, matching the existing `test_server.py` patterns.
- **Regression:** the existing test suite passes unchanged with `KG_ENABLED=false` and with it `true`.

### Acceptance on dev (`digital-twin-dev`, mcit-k8s)

Seed the **`mcit`** graph with memory-twin's own topology:
- `memory-mcp.chriscastrotech.com` (Cloudflare DNS) → Cloudflare tunnel → Envoy Gateway → HTTPRoute `digital-twin/memory-mcp` → Service → Deployment `memory-mcp` → namespace `digital-twin` → cluster `mcit-k8s` → node `opi-5`
- Qdrant StatefulSet and PVC; `appi-memory-twin-mcit`; `cctech-keyvault`; repos `midcityit/memory-mcp` and `midcityit/infra-tf`; Jira MCIT-193 and MCIT-251
- The ms01-k8s placement (Deployment `memory-mcp` on `ms01-k8s`, Qdrant PV `/data/qdrant`), **retired as of 2026-09-27**

Seed the **`vtv`** graph with a thin slice: `aks-vtv-prod` and the customer-solutions-tools workload (VTV-238). Add the xref `vtv::logical:VTV-238 —pattern_from→ mcit::logical:MCIT-184` (Key Vault CSI pattern).

All of the following must hold (seed also includes the monitoring path memory-mcp → otel-collector → `amw-mcit` ← Grafana datasource ← dashboard):
1. `kg_impact(mcit, <opi-5>)` includes the `memory-mcp` Deployment and the public hostname.
2. `kg_path(mcit, <hostname>, <memory-mcp deployment>)` returns the chain above.
3. `kg_traverse(mcit, <memory-mcp deployment>, as_of=2026-09-20)` shows `ms01-k8s` and not `mcit-k8s`.
4. `kg_impact(mcit, <ms01 qdrant PV>)` returns no current dependents.
5. `kg_related_across(vtv, <VTV-238>)` returns MCIT-184 through the xref.
6. `kg_upsert_entity` with an ARM ID of a NIC typed as a VM returns `type_id_mismatch`; `type="azurerm_kubernetes_cluster"` resolves to `Microsoft.ContainerService/managedClusters`.
7. Re-running the full seed batch reports only `updated`: no new entities and no new edges.
8. Memory and latency: p95 `kg_traverse` (depth 3) < 150 ms via the public endpoint; RSS within the §9 limit.
9. `kg_impact(mcit, <amw-mcit>)` includes the Grafana datasource (depth 1) and the dashboard that queries it (depth 2).

---

## 11. Rollout

1. PR(s) to `memory-mcp` → `:dev` image → set `KG_ENABLED=true` on `digital-twin-dev` (infra-tf).
2. Seed and run the §10 acceptance checks on dev.
3. Merge to `main` → infra-tf PR enables `KG_ENABLED=true` on prod `digital-twin`. Ziese stays disabled.
4. Seed prod `mcit` and `vtv` with the acceptance topology, which then becomes real data.
5. **Agent adoption** (required for success, because agents are the only writers). Update the global memory "Memory-Twin Agent Integration Guide" and the CastroTech Jira/Confluence/Memory Twin runbook (memory and `~/CASTROTECH_AGENT_RUNBOOK.md`) with these rules:
   - Call `kg_resolve` before writing; reuse existing keys.
   - Call `kg_batch` after a verified `terraform apply`, migration or deployment.
   - Call `kg_retire_entity` / `kg_unlink` on decommission or move; never hard-delete.
   - Record only topology you have verified; attach `evidence_memory_ids`.
   - Use `kg_xref` when one graph's work reuses another's pattern.
6. Tracking: epic RCL-30. **Sprint 1** (2026-10-05 → 10-16) covers steps 1–2: built and passing on dev (RCL-41, RCL-31…RCL-38). **Sprint 2** (2026-10-19 → 10-30) covers steps 3–5 and 7: prod (RCL-39), agent adoption (RCL-40), visualization (RCL-42).
7. **memory-ui visualization (RCL-42):** a read-only `/graph` explorer in `midcityit/memory-ui` using the §8 REST routes server-side. It has explore, impact, path and `as_of` views, an entity panel linking to memories, and an overview landing page. **Library: Cytoscape.js** (decided 2026-10-03: compound nodes for containment, dagre/fcose layouts). Layout/styling details and any UI-specific endpoint are settled in a short design pass at the start of RCL-42.

---

## 12. Out of scope (v1)

- LLM auto-extraction of entities from memory content
- Live ingestion (Terraform state, Azure Resource Graph, AWS Config, GCP Asset Inventory, vCenter, k8s API)
- Per-graph tokens or read/write scopes. The data model already supports adding these later
- Editing the graph from a UI. A **read-only** explorer in `memory-ui` was added on 2026-10-03 as RCL-42 (Sprint 2); see §11.7
- Code/image renames. As of 2026-10-03 the product is named **memory-twin-mcp**, superseding the earlier `recall` plan (RCL-4, RCL-27). Renaming the repo, package and image is separate work; `kg_*` tool names are rename-neutral

## 13. Risks

| Risk | Mitigation |
|---|---|
| Agents don't write to the graph, so it stays empty or stale | Adoption step 11.5; `kg_overview` "changed in last 7d" makes staleness visible; live ingestion is a v2 option |
| Duplicate entities from inconsistent IDs | Native-ID keys, normalization, `kg_resolve`-first rule, duplicate hints |
| Catalog generation drifts or misclassifies kinds | Build reports unmapped types; catalogs are committed and reviewed in PRs |
| Qdrant local mode differs from server in tests | First plan task verifies; fall back to a service container |
| Worker memory regression (history: PR #100, MCIT-168) | Shared embedder; catalog footprint measured on dev before prod |

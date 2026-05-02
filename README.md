# memory-mcp

A self-hosted MCP (Model Context Protocol) server providing persistent, semantic memory for AI agents. Uses Qdrant as the vector store and `all-MiniLM-L6-v2` for embeddings.

## Quick Start

```bash
# Required env vars
export QDRANT_URL="http://localhost:6333"
export API_TOKEN="your-secret-token"

# Optional
export STALE_DAYS=30
export OTLP_ENDPOINT="http://otel-collector.monitoring.svc.cluster.local:4317"

# Run
uvicorn memory_mcp.server:app --host 0.0.0.0 --port 8000
```

## MCP Tools

Mounted at `/mcp` (streamable HTTP transport, bearer-token protected):

| Tool | Description |
|------|-------------|
| `save_memory` | Upsert by name + source_repo |
| `search_memories` | Semantic vector search |
| `list_memories` | Filtered list |
| `delete_memory` | Delete by UUID |

## REST API

All endpoints (except `/health`) require `Authorization: Bearer <API_TOKEN>`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/memories` | List (with query filters) |
| GET | `/memories/{id}` | Get single |
| POST | `/memories/search` | Semantic search |
| POST | `/memories` | Save / upsert |
| PATCH | `/memories/{id}` | Update |
| DELETE | `/memories/{id}` | Delete |

## Observability

memory-mcp ships with OpenTelemetry instrumentation that pushes metrics and traces to an OTLP-compatible collector.

### Configuration

Set `OTLP_ENDPOINT` to your collector's gRPC endpoint. Defaults to `http://otel-collector.monitoring.svc.cluster.local:4317` (the in-cluster OTel Collector on ms01-k8s).

To disable telemetry, leave `OTLP_ENDPOINT` unset or empty — the app will still start but won't export metrics/traces.

### Metrics Exposed

| Metric | Type | Source |
|--------|------|--------|
| `http_server_request_duration_seconds` | histogram | FastAPIInstrumentor (auto) |
| `http_server_active_requests` | gauge | FastAPIInstrumentor (auto) |
| `memory_mcp_upsert_total` | counter | store.py (custom) |
| `memory_mcp_search_duration_seconds` | histogram | store.py (custom) |
| `memory_mcp_memory_count` | gauge | store.py (observable, polls Qdrant) |
| `qdrant_*` | various | Qdrant native `/metrics` (scraped by collector) |

### Dashboards

Two Grafana dashboards are provisioned via Terraform (`tf-int/dev/grafana-dashboard-memory-mcp.tf`):

1. **memory-mcp Overview** — HTTP request rate, latency percentiles (p50/p95/p99), 5xx error rate, memory count, upsert rate, search latency
2. **Qdrant** — collection size, vector count, indexing queue, query latency

Access at: `https://grafana.chriscastrotech.com`

### Alerts

Azure Monitor Prometheus alert rules (`tf-int/dev/azure-monitor-memory-mcp-alerts.tf`):

| Alert | Condition | Severity |
|-------|-----------|----------|
| MemoryMcpHighErrorRate | >5% 5xx over 5 min | 2 |
| MemoryMcpHighLatency | p95 > 2s over 5 min | 2 |
| QdrantDown | No metrics for 5 min | 1 |

Notifications go to `chris@chriscastrotech.com`.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Docker

```bash
docker build -t memory-mcp .
docker run -e QDRANT_URL=http://qdrant:6333 -e API_TOKEN=secret -p 8000:8000 memory-mcp
```

Published to `ghcr.io/midcityit/memory-mcp:latest` on push to `main`.

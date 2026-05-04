# memory-mcp: Orchestration Implementation Plan

**Author:** Kiro CLI personal agent (mbp-server) + Orion (OpenClaw)  
**Date:** 2026-04-30  
**Branch:** kiro/orchestration-plan  
**Status:** Draft — ready for review

---

## Background

memory-mcp is a FastAPI + Qdrant server exposing four MCP tools (`save_memory`, `search_memories`, `list_memories`, `delete_memory`) and a set of REST endpoints, all protected by bearer token auth. It is deployed on ms01-k8s and used by all agents in the environment (Claude Code, Claude Desktop, VS Code Copilot, Amazon Q, Kiro CLI, OpenClaw/Orion).

The next major milestone is an **Agent Orchestration Platform** — the ability for one agent to delegate work to another, track task state, and coordinate multi-agent workflows. Rather than building a separate service, orchestration will be added directly to memory-mcp.

---

## Design Principles

1. **Composability** — orchestrator agents call subordinate agents via MCP tools, not custom protocols
2. **Idempotency** — task creation is safe to retry; same task ID for same (dispatcher, task_key) pair
3. **Auth scoping** — `agent_scope` is stored as metadata for future use but is *never enforced*; all agents can read all memories to maximize continuity across sessions and tools
4. **Observability** — all task state transitions emit structured log fields; tasks have a `status` lifecycle
5. **Phased delivery** — Phase 1 ships a working MVP; Phase 2 adds advanced workflow features
6. **Small team realism** — avoid distributed systems complexity; use Qdrant as the task store (already present), not a separate queue

---

## Data Model Changes

### New Qdrant Collection: `agents`

Tracks registered agents and their capabilities.

```python
@dataclass
class AgentRecord:
    id: str                    # UUID
    agent_id: str              # e.g. "kiro-personal", "orion", "claude-code-mbp"
    display_name: str
    capabilities: list[str]   # e.g. ["shell", "aws", "git", "memory-twin"]
    host: str                  # e.g. "mbp-server.chriscastrotech.internal"
    status: str                # "active" | "idle" | "offline"
    last_seen: str             # ISO 8601
    metadata: dict             # free-form agent config notes
    created_at: str
    updated_at: str
```

### New Qdrant Collection: `tasks`

Tracks dispatched tasks and their lifecycle.

```python
@dataclass
class TaskRecord:
    id: str                    # UUID (stable, idempotency key)
    task_key: str              # caller-supplied deduplication key (optional)
    title: str
    description: str           # full task prompt/spec
    dispatcher_agent: str      # who created it
    target_agent: str          # who should execute it
    status: str                # "queued" | "in_progress" | "completed" | "failed" | "cancelled"
    priority: int              # 0=urgent, 1=high, 3=normal, 5=low
    context: dict              # passed-through memory IDs or inline context
    result: str | None         # agent's output/summary on completion
    error: str | None          # error message if failed
    callback_url: str | None   # optional HTTP webhook for async completion
    created_at: str
    updated_at: str
    completed_at: str | None
```

### Memory Record Changes (existing `memories` collection)

Add optional `agent_scope` payload field — stored as metadata for observability and future use, but **never enforced**. All agents can read and write all memories regardless of this field.

- `agent_scope: null` → public (default for all existing and new records)
- `agent_scope: "kiro-personal"` → annotated as agent-specific, but still readable by all agents

Rationale: maximum continuity across agents and sessions is the priority. Enforcement can be opted into later if isolation requirements change.

---

## New MCP Tools

### Phase 1 (MVP)

#### `register_agent`
Register this agent in the agent registry. Called at session start by any agent.

```python
register_agent(
    agent_id: str,
    display_name: str,
    capabilities: list[str],
    host: str,
    metadata: dict = {}
) -> dict  # AgentRecord
```

#### `delegate_task`
Dispatch a task to another agent. Returns task ID immediately (async).

```python
delegate_task(
    title: str,
    description: str,
    target_agent: str,
    priority: int = 3,
    context: dict = {},
    task_key: str | None = None,   # for idempotency
    callback_url: str | None = None
) -> dict  # TaskRecord with status="queued"
```

#### `poll_tasks`
Called by an agent to check for work assigned to it. Primary polling mechanism.

```python
poll_tasks(
    agent_id: str,
    status: str = "queued",
    limit: int = 5
) -> list[dict]  # list of TaskRecord
```

#### `get_task_status`
Get current status and result for a task.

```python
get_task_status(task_id: str) -> dict  # TaskRecord
```

#### `update_task`
Update task status — called by the executing agent.

```python
update_task(
    task_id: str,
    status: str,               # "in_progress" | "completed" | "failed"
    result: str | None = None,
    error: str | None = None
) -> dict  # TaskRecord
```

### Phase 2 (Advanced Orchestration)

#### `list_agents`
Query the agent registry.

```python
list_agents(
    status: str | None = None,
    capability: str | None = None
) -> list[dict]
```

#### `create_workflow`
Define a multi-step task chain (sequential or parallel).

```python
create_workflow(
    name: str,
    steps: list[WorkflowStep],  # each step: target_agent, task description, depends_on
    context: dict = {}
) -> dict  # WorkflowRecord
```

#### `get_workflow_status`
Get status of all steps in a workflow.

```python
get_workflow_status(workflow_id: str) -> dict
```

---

## New REST Endpoints

### Phase 1

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/orchestrate/agents` | List registered agents |
| `GET` | `/orchestrate/agents/{agent_id}` | Get agent details |
| `GET` | `/orchestrate/tasks` | List tasks (filterable by status, target_agent, dispatcher) |
| `GET` | `/orchestrate/tasks/{task_id}` | Get task details |
| `POST` | `/orchestrate/tasks/{task_id}/result` | Agent posts completion result |

### Phase 2

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/orchestrate/workflows` | List workflows |
| `GET` | `/orchestrate/workflows/{id}` | Get workflow + step statuses |
| `POST` | `/orchestrate/tasks/{task_id}/cancel` | Cancel a queued/in-progress task |
| `GET` | `/orchestrate/queue/{agent_id}` | Shortcut: queued tasks for a specific agent |

All endpoints require bearer token auth (same middleware as existing routes).

---

## Agent Namespacing

`agent_scope` is an **annotation-only** field — it is stored on memory records for observability and future filtering, but is never used to restrict reads or writes by any agent in any phase.

```python
# All memories are visible to all agents regardless of agent_scope
save_memory(type="reference", name="foo", content="...", source_repo="global")

# Annotate as agent-specific for tracking, but still fully readable by all agents
save_memory(type="reference", name="bar", content="...", source_repo="global",
            agent_scope="kiro-personal")
```

This design maximizes continuity — any agent can pick up context from any other agent's memories without permission gaps. If isolation is ever needed in the future, enforcement can be added as an explicit opt-in behind a feature flag.

**All phases:** `agent_scope` = annotation only. No enforcement, ever, unless explicitly requested.

---

## Event / Callback System

For async task completion, `delegate_task` accepts an optional `callback_url`. When `update_task` sets status to `completed` or `failed`, memory-mcp fires a POST to that URL:

```json
{
  "task_id": "abc123",
  "status": "completed",
  "result": "Done — pushed branch kiro/orchestration-plan",
  "completed_at": "2026-04-30T20:00:00Z"
}
```

**Phase 1:** Best-effort HTTP POST (fire and forget, log failures).  
**Phase 2:** Retry with exponential backoff, dead-letter queue in Qdrant.

OpenClaw/Orion can register its local webhook endpoint so Kiro can complete a task and signal back without polling.

---

## Migration Path

### From Current Single-Agent Schema

1. **New collections are additive** — `agents` and `tasks` collections are created alongside `memories`. No changes to existing memories.
2. **`agent_scope` field** — add as nullable payload field on all existing memory records via `migrate.py` (set to `null`). One-time migration, safe to re-run.
3. **Backward compatibility** — all existing MCP tools (`save_memory`, etc.) continue to work unchanged. New tools are additions.
4. **No breaking API changes in Phase 1.**

Migration script additions to `migrate.py`:
```python
# Add agent_scope=null to all existing memories
def migrate_agent_scope(store: MemoryStore):
    records = store.list_memories(limit=10000)
    for r in records:
        if "agent_scope" not in r.metadata:
            store.set_payload_field(r.id, "agent_scope", None)
```

---

## Implementation Phases

### Phase 1 — MVP (estimated: 2–3 days)

**Goal:** Working task delegation between agents via poll-based queue.

- [ ] `AgentRecord` dataclass + `agents` Qdrant collection
- [ ] `TaskRecord` dataclass + `tasks` Qdrant collection
- [ ] `AgentStore` class (similar to `MemoryStore`)
- [ ] `TaskStore` class
- [ ] MCP tools: `register_agent`, `delegate_task`, `poll_tasks`, `get_task_status`, `update_task`
- [ ] REST endpoints: GET /orchestrate/agents, GET/POST /orchestrate/tasks, GET /orchestrate/tasks/{id}
- [ ] Migration: add `agent_scope=null` to existing memory records
- [ ] Unit tests for AgentStore and TaskStore
- [ ] Integration test: dispatch task → poll → complete → verify status
- [ ] Update README with orchestration section

### Phase 2 — Full Orchestration (estimated: 3–5 days)

**Goal:** Workflow chains, callback system. Auth scoping enforcement intentionally omitted — annotation-only by design.

- [ ] `WorkflowRecord` dataclass + `workflows` collection
- [ ] MCP tools: `create_workflow`, `get_workflow_status`, `list_agents`
- [ ] Workflow engine: sequential step execution, parallel fan-out
- [ ] Per-agent token support (optional, Key Vault backed) — only if isolation is explicitly requested
- [ ] `agent_scope` enforcement remains **off** unless Chris requests it
- [ ] Callback system: HTTP POST on task completion with retry
- [ ] REST: /orchestrate/workflows, /orchestrate/queue/{agent_id}, /orchestrate/tasks/{id}/cancel
- [ ] Dashboard integration: expose orchestration state in memory-ui
- [ ] Load test: 10 concurrent agents polling

---

## Test Strategy

### Unit Tests (pytest, existing `tests/` structure)

- `test_agent_store.py` — CRUD for AgentRecord, list/filter, staleness
- `test_task_store.py` — create/update/poll task lifecycle
- `test_orchestration_tools.py` — MCP tool layer (mocked store)
- `test_namespacing.py` — verify agent_scope field is stored and returned correctly (no enforcement testing needed)

### Integration Tests

- `test_orchestration_e2e.py` — full flow: register agent A and B → A delegates to B → B polls → B completes → A checks status → verify callback fires
- `test_idempotency.py` — same task_key submitted twice → only one task created

### Manual Smoke Test

1. Start memory-mcp locally with test Qdrant
2. Use `kiro-cli --agent personal` to call `register_agent` and `delegate_task`
3. From a second terminal (simulating target agent), call `poll_tasks` and `update_task`
4. Verify GET /orchestrate/tasks/{id} shows completed

---

## File Changes Summary

```
src/memory_mcp/
  orchestration/
    __init__.py
    agent_store.py       # AgentRecord, AgentStore
    task_store.py        # TaskRecord, TaskStore
    workflow_store.py    # Phase 2: WorkflowRecord, WorkflowStore
    mcp_tools.py         # New MCP tools (register_agent, delegate_task, etc.)
    routes.py            # New REST endpoints mounted at /orchestrate
  server.py              # Mount orchestration routes
  migrate.py             # Add agent_scope migration step

tests/
  test_agent_store.py
  test_task_store.py
  test_orchestration_tools.py
  test_orchestration_e2e.py
```

---

## Open Questions

1. **Per-agent tokens vs shared token + header** — shared token is simpler but weaker isolation. Defer to Phase 2.
2. **Qdrant vs SQLite for task queue** — Qdrant works but a relational store is better for task ordering. Consider adding SQLite in Phase 2 if ordering becomes critical.
3. **Workflow step parallelism** — fan-out requires managing concurrent task completion. Likely a background asyncio task in Phase 2.
4. **memory-ui orchestration view** — out of scope for now; REST endpoints make it easy to add later.

---

*Plan authored on branch `kiro/orchestration-plan`. Feedback welcome via PR comments.*

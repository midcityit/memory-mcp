"""Audit recorder (RCL-73 task 6): rows, scrubbing, read gating, purge, and the app wiring."""
from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timedelta, timezone

import pytest

from memory_mcp.admin.audit import Audit, NullAudit, _scrub, run_purge_loop
from memory_mcp.admin.keys import generate_key
from memory_mcp.admin.models import Principal
from memory_mcp.admin.store import InMemoryAdminStore
from tests.admin.test_memory_isolation import Caller, _build, _serving

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def key_principal(agent="agent-a") -> Principal:
    return Principal(kind="key", id="k1", agent=agent, roles={"alpha": "editor"})


def ui_principal() -> Principal:
    return Principal(kind="ui", id="user-1", agent="ui:Ada Example", roles={"alpha": "editor"},
                     user_name="Ada Example")


@pytest.fixture
def parts():
    store, settings, clock = InMemoryAdminStore(), {}, Clock()
    return store, settings, clock, Audit(store, lambda: settings, clock)


def rows(store):
    return store.query_audit(limit=1000)[0]


def test_write_produces_allowed_row(parts):
    store, _, _, audit = parts
    audit.record(key_principal(), "save_memory", "alpha", "mem-1", "allowed")
    [r] = rows(store)
    assert (r["principal_type"], r["principal_id"], r["agent"], r["acting_user"]) == ("key", "k1", "agent-a", None)
    assert (r["tenant"], r["action"], r["target"], r["outcome"], r["ts"]) == (
        "alpha", "save_memory", "mem-1", "allowed", T0)


def test_ui_principal_stamps_agent_and_acting_user(parts):
    store, _, _, audit = parts
    audit.record(ui_principal(), "save_memory", "alpha", None, "allowed")
    [r] = rows(store)
    assert (r["principal_type"], r["agent"], r["acting_user"]) == ("ui", "ui:Ada Example", "Ada Example")


def test_legacy_principal_has_no_agent(parts):
    store, _, _, audit = parts
    audit.record(Principal(kind="legacy", id="legacy", agent=None, roles={}, all_tenants=True),
                 "save_memory", "alpha", None, "allowed")
    [r] = rows(store)
    assert r["agent"] is None and r["acting_user"] is None


def test_denial_names_the_rule(parts):
    store, _, _, audit = parts
    audit.record(key_principal(), "save_memory", "beta", None, "denied", {"rule": "tenant_forbidden"})
    [r] = rows(store)
    assert r["outcome"] == "denied" and r["detail"] == {"rule": "tenant_forbidden"}


def test_read_not_logged_by_default_but_logged_when_enabled(parts):
    store, settings, _, audit = parts
    audit.record(key_principal(), "search_memories", "alpha", None, "allowed")
    assert rows(store) == []
    settings["audit_reads"] = "true"  # only exactly True counts
    audit.record(key_principal(), "search_memories", "alpha", None, "allowed")
    assert rows(store) == []
    settings["audit_reads"] = True
    audit.record(key_principal(), "search_memories", "alpha", None, "allowed")
    assert len(rows(store)) == 1


@pytest.mark.parametrize("outcome", ["denied", "error"])
def test_failed_reads_always_recorded(parts, outcome):
    store, _, _, audit = parts
    audit.record(key_principal(), "list_memories", "alpha", None, outcome, {"rule": "x"})
    assert [r["outcome"] for r in rows(store)] == [outcome]


def test_detail_scrubbed_before_storage(parts):
    store, _, _, audit = parts
    full, _, _ = generate_key()
    detail = {"content": "x", "key": full, "rule": "r", "Token": "t"}
    audit.record(key_principal(), "save_memory", "alpha", None, "denied", detail)
    [r] = rows(store)
    assert r["detail"] == {"rule": "r"}
    assert detail["content"] == "x"  # input untouched


def test_scrub_recurses_and_redacts_key_shaped_values():
    full, _, _ = generate_key()
    detail = {"a": {"password": "p", "ok": 1, "deep": [{"secret_hash": "h", "note": f"got {full}"}, full]},
              "msg": full, "Authorization": "Bearer x", "plain": "mt_short"}
    before = copy.deepcopy(detail)
    out = _scrub(detail)
    assert out == {"a": {"ok": 1, "deep": [{"note": "[redacted]"}, "[redacted]"]},
                   "msg": "[redacted]", "plain": "mt_short"}
    assert detail == before and out is not detail
    assert _scrub(None) is None


def test_record_never_raises_and_warns_without_detail(caplog):
    class Broken(InMemoryAdminStore):
        def add_audit(self, row):
            raise RuntimeError("db down")

    audit = Audit(Broken(), lambda: {})
    with caplog.at_level(logging.WARNING, logger="memory_mcp.admin.audit"):
        audit.record(key_principal(), "save_memory", "alpha", None, "allowed", {"rule": "SENTINEL"})
    assert len(caplog.records) == 1 and "SENTINEL" not in caplog.text


def test_record_survives_settings_failure():
    def boom():
        raise RuntimeError("no settings")

    Audit(InMemoryAdminStore(), boom).record(key_principal(), "list_memories", "alpha", None, "allowed")


def _seed_old_and_new(store, clock, days_old):
    for age in (days_old + 1, days_old - 1):
        store.add_audit({"ts": clock.now - timedelta(days=age), "principal_type": "key", "principal_id": "k1",
                         "agent": "a", "acting_user": None, "tenant": "alpha", "action": "save_memory",
                         "target": f"age{age}", "outcome": "allowed", "detail": None})


@pytest.mark.parametrize("setting,days", [(None, 90), (30, 30), (0, 90), (-5, 90), ("30", 90), (True, 90), (2.5, 90)])
def test_purge_honours_retention(parts, setting, days):
    store, settings, clock, audit = parts
    if setting is not None:
        settings["audit_retention_days"] = setting
    _seed_old_and_new(store, clock, days)
    assert audit.purge() == 1
    assert [r["target"] for r in rows(store)] == [f"age{days - 1}"]


async def test_purge_loop_runs_repeatedly_and_survives_errors():
    calls = []

    class Flaky:
        def purge(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return 0

    task = asyncio.create_task(run_purge_loop(Flaky(), interval_s=0.01, first_delay_s=0.01))
    for _ in range(200):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 3 and task.cancelled()


def test_null_audit_records_nothing():
    assert NullAudit().record(key_principal(), "save_memory", "alpha", None, "allowed") is None


# ── through the real app ────────────────────────────────────────────────────────
SECRET_TEXT = "zebra-quartz-payload-7731"


def _real_audit_env():
    """Like test_memory_isolation._build, but with the real Audit left in place."""
    from unittest.mock import patch

    from qdrant_client import QdrantClient

    from memory_mcp.server import create_app
    from memory_mcp.store import MemoryStore
    from tests.admin.test_memory_isolation import Env, RecordingAudit, _cfg
    from tests.kg.conftest import StubEmbedder

    mstore = MemoryStore("unused", embedder=StubEmbedder(), client=QdrantClient(":memory:"))
    admin = InMemoryAdminStore()
    with patch("memory_mcp.server.load_config", return_value=_cfg()):
        app = create_app(admin_store=admin, memory_store=mstore)
    app.state.tenants.create("beta", "Beta", "", by="test")
    return Env(app, admin, mstore, RecordingAudit())


@pytest.mark.filterwarnings("ignore:Payload indexes have no effect")
async def test_end_to_end_save_and_denied_save_are_audited():
    async for e in _serving(_real_audit_env()):
        k = e.key(tenants=("alpha",), agent="agent-a")
        c = Caller("rest", e.client)

        ok = await c.save(k, name="n1", tenant="alpha", content=SECRET_TEXT)
        assert ok.ok, ok.message
        denied = await c.save(k, name="n2", tenant="beta", content=SECRET_TEXT)
        assert not denied.ok

        stored = rows(e.admin)
        assert len(stored) == 2
        [allowed] = [r for r in stored if r["outcome"] == "allowed"]
        [deny] = [r for r in stored if r["outcome"] == "denied"]
        assert (allowed["action"], allowed["tenant"], allowed["agent"], allowed["principal_type"]) == (
            "save_memory", "alpha", "agent-a", "key")
        assert deny["tenant"] == "beta" and deny["detail"]["rule"]
        assert SECRET_TEXT not in repr(stored)


@pytest.mark.filterwarnings("ignore:Payload indexes have no effect")
async def test_lifespan_runs_and_cancels_purge_task():
    e = _real_audit_env()
    e.admin.add_audit({"ts": datetime(2000, 1, 1, tzinfo=timezone.utc), "principal_type": "key",
                       "principal_id": "k1", "agent": "a", "acting_user": None, "tenant": "alpha",
                       "action": "save_memory", "target": None, "outcome": "allowed", "detail": None})
    from unittest.mock import patch

    from memory_mcp.server import create_app
    from tests.admin.test_memory_isolation import _cfg
    with patch("memory_mcp.server.load_config", return_value=_cfg()):
        app = create_app(admin_store=e.admin, memory_store=e.mstore,
                         audit_purge_interval_s=0.01, audit_purge_first_delay_s=0.01)
    async with app.router.lifespan_context(app):
        for _ in range(200):
            if not rows(e.admin):
                break
            await asyncio.sleep(0.01)
    assert rows(e.admin) == []
    leftover = [t for t in asyncio.all_tasks() if "run_purge_loop" in repr(t.get_coro())]
    assert leftover == []

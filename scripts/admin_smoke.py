#!/usr/bin/env python3
"""End-to-end smoke test for tenants, scoped keys and RBAC against a running server.

Not a pytest test: it drives a real server (real Qdrant, real Postgres) over HTTP and a
real MCP client session, prints one PASS/FAIL line per check and exits non-zero on any
failure. Tokens and generated keys are never printed.

API mode (server running with ADMIN_PG_DSN, BOOTSTRAP_TENANT_ID=alpha, KG_ENABLED=true):
  SMOKE_API_TOKEN=... SMOKE_UI_TOKEN=... SMOKE_ADMIN_GROUP=<guid> \\
    python scripts/admin_smoke.py --base-url http://localhost:58000 \\
      [--qdrant-url http://localhost:56333] [--expect-restart]

  --qdrant-url    also checks the stored `copied_from` payload (the API does not return it)
  --expect-restart after the API checks, prints "WAITING FOR RESTART", waits until the
                  server goes down and comes back, then checks that state persisted.

Split mode (server stopped; touches only `smoke_split_src` and `smoke_split_*`):
  python scripts/admin_smoke.py --split-qdrant-url http://localhost:56333
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx

REVOKE_WAIT_S = 31.0
POLL_S = 2.0


class Checks:
    def __init__(self, secrets: list[str]):
        self.secrets = secrets  # anything in here is masked in every printed line
        self.failed = 0
        self.passed = 0

    def _mask(self, text: str) -> str:
        for s in self.secrets:
            if s:
                text = text.replace(s, "<redacted>")
        return text

    def check(self, ok: bool, desc: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        line = f"{'PASS' if ok else 'FAIL'} {desc}" + (f" ({detail})" if detail and not ok else "")
        print(self._mask(line), flush=True)
        return ok

    def note(self, text: str) -> None:
        print(self._mask(f"---- {text}"), flush=True)


def _code(r: httpx.Response) -> str | None:
    try:
        body = r.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


def _ids(body: dict) -> set[str]:
    return {m["id"] for m in body.get("memories", [])}


# ── MCP client ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def mcp_session(base_url: str, token: str):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(f"{base_url}/mcp/", http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


class ToolResult:
    def __init__(self, raw):
        self.is_error = bool(raw.is_error)
        self.text = " ".join(getattr(c, "text", "") or "" for c in raw.content)
        data = raw.structured_content
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        if data is None and not self.is_error:
            try:
                data = json.loads(self.text)
            except ValueError:
                data = None
        self.data = data


async def tool(session, tool_name: str, /, **args) -> ToolResult:
    return ToolResult(await session.call_tool(tool_name, args))


# ── API smoke ─────────────────────────────────────────────────────────────────
class ApiSmoke:
    def __init__(self, base_url: str, api_token: str, ui_token: str, admin_group: str,
                 qdrant_url: str | None):
        self.base = base_url.rstrip("/")
        self.api_token, self.ui_token = api_token, ui_token
        self.admin_group = admin_group
        self.qdrant_url = qdrant_url
        self.c = Checks([api_token, ui_token])
        self.http = httpx.Client(base_url=self.base, timeout=60)
        self.run = uuid.uuid4().hex[:8]
        self.contents: list[str] = []  # every memory content saved, for the audit check
        self.keys: dict[str, str] = {}  # label -> full key (secret)
        self.key_ids: dict[str, str] = {}

    # helpers
    def admin_h(self) -> dict:
        return {"Authorization": f"Bearer {self.ui_token}", "X-MT-User": "smoke-admin",
                "X-MT-User-Name": quote("Smoke Admin (ä)"), "X-MT-Groups": self.admin_group}

    @staticmethod
    def bearer(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def content(self, label: str) -> str:
        text = f"smoke {self.run} {label} confidential body {uuid.uuid4().hex}"
        self.contents.append(text)
        return text

    def save(self, token: str, label: str, *, tenant: str | None = None, agent: str = "impostor-agent"):
        params = {"tenant": tenant} if tenant else None
        return self.http.post("/memories", headers=self.bearer(token), params=params, json={
            "type": "reference", "name": f"smoke {self.run} {label}", "content": self.content(label),
            "source_repo": "smoke", "agent": agent, "tags": ["smoke"]})

    def get(self, token: str, path: str, **params) -> httpx.Response:
        return self.http.get(path, headers=self.bearer(token), params=params or None)

    def admin(self, method: str, path: str, **kw) -> httpx.Response:
        return self.http.request(method, path, headers=self.admin_h(), **kw)

    def wait_status(self, fn, want: int, limit_s: float = REVOKE_WAIT_S) -> tuple[bool, float]:
        t0 = time.monotonic()
        while True:
            if fn().status_code == want:
                return True, time.monotonic() - t0
            if time.monotonic() - t0 > limit_s:
                return False, time.monotonic() - t0
            time.sleep(POLL_S)

    # 1-2
    def basics(self) -> None:
        c = self.c
        c.note("1. unauthenticated routes")
        r = self.http.get("/health")
        c.check(r.status_code == 200, "/health is 200 without a token", str(r.status_code))
        r = self.http.get("/docs")
        c.check(r.status_code == 401, "/docs is 401 without a token", str(r.status_code))
        r = self.http.get("/memories")
        c.check(r.status_code == 401, "/memories is 401 without a token", str(r.status_code))

        c.note("2. /admin/me as instance admin")
        r = self.admin("GET", "/admin/me")
        me = r.json() if r.status_code == 200 else {}
        c.check(r.status_code == 200, "GET /admin/me is 200", str(r.status_code))
        c.check(me.get("legacy_mode") is False, "legacy_mode is false")
        c.check(me.get("instance_admin") is True, "instance_admin is true")
        c.check("alpha" in {t["id"] for t in me.get("tenants", [])}, "tenant alpha is listed")
        c.check(me.get("user", {}).get("name") == "Smoke Admin (ä)", "percent-encoded user name decoded")
        r = self.get(self.api_token, "/admin/me")
        c.check(r.status_code == 403, "legacy token gets 403 on /admin", str(r.status_code))

    # 3-5
    def legacy_and_setup(self) -> None:
        c = self.c
        c.note("3. legacy token saves and searches (default tenant alpha)")
        r = self.save(self.api_token, "legacy", agent="legacy-agent")
        c.check(r.status_code == 201, "legacy save is 201", str(r.status_code))
        rec = r.json() if r.status_code == 201 else {}
        self.legacy_id = rec.get("id", "")
        c.check(rec.get("agent") == "legacy-agent", "legacy save keeps the declared agent", rec.get("agent", ""))
        r = self.http.post("/memories/search", headers=self.bearer(self.api_token),
                           json={"query": self.contents[-1], "limit": 5})
        c.check(r.status_code == 200 and self.legacy_id in _ids(r.json()), "legacy search finds it")
        r = self.get(self.api_token, f"/memories/{self.legacy_id}", tenant="alpha")
        c.check(r.status_code == 200, "legacy memory is in tenant alpha", str(r.status_code))

        c.note("4. create tenant beta")
        r = self.admin("POST", "/admin/tenants", json={"id": "beta", "name": "Beta (smoke)"})
        c.check(r.status_code == 201 and r.json().get("id") == "beta", "POST /admin/tenants beta is 201",
                str(r.status_code))
        r = self.admin("POST", "/admin/tenants", json={"id": "beta", "name": "again"})
        c.check(r.status_code == 409, "creating beta again is 409", str(r.status_code))
        r = self.get(self.api_token, f"/memories/{self.legacy_id}", tenant="beta")
        c.check(r.status_code == 404, "legacy memory is not in beta", str(r.status_code))

        c.note("5. create keys")
        specs = {
            "alpha": {"agent": "smoke-alpha", "tenants": ["alpha"], "access": "write"},
            "beta": {"agent": "smoke-beta", "tenants": ["beta"], "access": "write"},
            "reader": {"agent": "smoke-reader", "tenants": ["alpha", "beta"], "access": "read"},
            "nodelete": {"agent": "smoke-nodelete", "tenants": ["alpha"], "access": "write",
                         "denied_tools": ["delete_memory"]},
        }
        for label, spec in specs.items():
            r = self.admin("POST", "/admin/keys", json={**spec, "expires_in_days": 30})
            ok = r.status_code == 201
            body = r.json() if ok else {}
            if ok:
                self.keys[label] = body["key"]
                self.key_ids[label] = body["record"]["id"]
                self.c.secrets.append(body["key"])
            c.check(ok and body["key"].startswith("mt_") and "secret_hash" not in body["record"],
                    f"create {label} key ({spec['access']}, {','.join(spec['tenants'])})", str(r.status_code))
        r = self.admin("GET", "/admin/keys")
        listing = r.text
        c.check(r.status_code == 200 and all(k not in listing for k in self.keys.values())
                and "secret_hash" not in listing, "GET /admin/keys never shows a key or hash")
        if len(self.keys) != len(specs):
            raise SystemExit("cannot continue without the keys")

    # 6 REST
    def rest_isolation(self) -> None:
        c, k = self.c, self.keys
        c.note("6a. REST isolation")
        ids = {}
        for label, other in (("alpha", "beta"), ("beta", "alpha")):
            r = self.save(k[label], f"{label} rest")
            rec = r.json() if r.status_code == 201 else {}
            ids[label] = rec.get("id", "")
            c.check(r.status_code == 201, f"{label} key saves (REST)", str(r.status_code))
            c.check(rec.get("agent") == f"smoke-{label}", f"{label} save stamped agent smoke-{label}, "
                    "not the declared one", str(rec.get("agent")))
            r = self.http.post("/memories/search", headers=self.bearer(k[label]),
                               json={"query": self.contents[-1], "limit": 5})
            c.check(r.status_code == 200 and ids[label] in _ids(r.json()), f"{label} key search finds it")
            r = self.get(k[label], "/memories")
            c.check(r.status_code == 200 and ids[label] in _ids(r.json()), f"{label} key list includes it")
            r = self.get(k[label], f"/memories/{ids[label]}")
            c.check(r.status_code == 200, f"{label} key get is 200", str(r.status_code))
            r = self.get(k[label], "/memories", tenant=other)
            c.check(r.status_code == 403 and _code(r) == "tenant_forbidden",
                    f"{label} key gets 403 for tenant={other}", f"{r.status_code} {_code(r)}")
            r = self.http.post("/memories", headers=self.bearer(k[label]), params={"tenant": other},
                               json={"type": "reference", "name": "x", "content": "x"})
            c.check(r.status_code == 403, f"{label} key cannot save into {other}", str(r.status_code))
        self.alpha_id, self.beta_id = ids["alpha"], ids["beta"]
        for label, other in (("alpha", "beta"), ("beta", "alpha")):
            r = self.get(k[label], f"/memories/{ids[other]}")
            c.check(r.status_code == 404, f"{label} key gets 404 for a {other} memory id", str(r.status_code))
            r = self.http.delete(f"/memories/{ids[other]}", headers=self.bearer(k[label]))
            c.check(r.status_code == 404, f"{label} key delete of a {other} memory id is 404", str(r.status_code))
        r = self.get(k["beta"], f"/memories/{self.beta_id}")
        c.check(r.status_code == 200, "beta memory survived the cross-tenant delete")
        r = self.get(k["alpha"], f"/memories/{self.legacy_id}")
        c.check(r.status_code == 200, "alpha key reads the legacy-token memory", str(r.status_code))

        rd = k["reader"]
        r = self.get(rd, "/memories", tenant="alpha")
        c.check(r.status_code == 200 and self.alpha_id in _ids(r.json()), "reader lists alpha with tenant=alpha")
        r = self.get(rd, "/memories", tenant="beta")
        c.check(r.status_code == 200 and self.beta_id in _ids(r.json()), "reader lists beta with tenant=beta")
        r = self.get(rd, f"/memories/{self.beta_id}", tenant="beta")
        c.check(r.status_code == 200, "reader gets a beta memory with tenant=beta", str(r.status_code))
        r = self.http.post("/memories", headers=self.bearer(rd), params={"tenant": "alpha"},
                           json={"type": "reference", "name": "x", "content": "x"})
        c.check(r.status_code == 403 and _code(r) == "key_read_only", "reader cannot save",
                f"{r.status_code} {_code(r)}")
        r = self.get(rd, "/memories")
        c.check(r.status_code == 400 and _code(r) == "tenant_required", "reader without tenant gets tenant_required",
                f"{r.status_code} {_code(r)}")

        nd = k["nodelete"]
        r = self.get(nd, f"/memories/{self.alpha_id}")
        c.check(r.status_code == 200, "denied-tools key can read", str(r.status_code))
        r = self.http.delete(f"/memories/{self.alpha_id}", headers=self.bearer(nd))
        c.check(r.status_code == 403 and _code(r) == "tool_denied", "denied-tools key cannot delete (REST)",
                f"{r.status_code} {_code(r)}")
        r = self.get(k["alpha"], f"/memories/{self.alpha_id}")
        c.check(r.status_code == 200, "memory still exists after the refused delete")

    # 6 MCP
    async def mcp_isolation(self) -> None:
        c, k = self.c, self.keys
        c.note("6b. MCP isolation (real client session per key)")
        mids = {}
        for label, other in (("alpha", "beta"), ("beta", "alpha")):
            async with mcp_session(self.base, k[label]) as s:
                content = self.content(f"{label} mcp")
                r = await tool(s, "save_memory", type="reference", name=f"smoke {self.run} {label} mcp",
                               content=content, source_repo="smoke", agent="impostor-agent")
                rec = r.data if isinstance(r.data, dict) else {}
                mids[label] = rec.get("id", "")
                c.check(not r.is_error and mids[label], f"{label} key saves (MCP)", r.text[:120])
                c.check(rec.get("agent") == f"smoke-{label}", f"{label} MCP save stamped agent smoke-{label}",
                        str(rec.get("agent")))
                r = await tool(s, "search_memories", query=content, limit=5)
                found = {m["id"] for m in (r.data or [])} if isinstance(r.data, list) else set()
                c.check(not r.is_error and mids[label] in found, f"{label} key search finds it (MCP)", r.text[:120])
                r = await tool(s, "list_memories")
                c.check(not r.is_error and mids[label] in _ids(r.data or {}), f"{label} key list includes it (MCP)")
                r = await tool(s, "list_memories", tenant=other)
                c.check(r.is_error and "No access to tenant" in r.text, f"{label} key refused tenant={other} (MCP)",
                        r.text[:120])
                r = await tool(s, "delete_memory", memory_id=getattr(self, f"{other}_id"))
                c.check(not r.is_error and r.data == {"deleted": False},
                        f"{label} key cannot see a {other} memory id (MCP delete finds nothing)", r.text[:120])
        self.alpha_mcp_id = mids["alpha"]
        for label in ("alpha", "beta"):
            r = self.get(k[label], f"/memories/{getattr(self, f'{label}_id')}")
            c.check(r.status_code == 200, f"{label} memory survived the cross-tenant MCP delete")

        async with mcp_session(self.base, k["reader"]) as s:
            r = await tool(s, "list_memories", tenant="alpha")
            c.check(not r.is_error and self.alpha_id in _ids(r.data or {}), "reader lists alpha (MCP)")
            r = await tool(s, "list_memories", tenant="beta")
            c.check(not r.is_error and self.beta_id in _ids(r.data or {}), "reader lists beta (MCP)")
            r = await tool(s, "save_memory", type="reference", name="x", content="x", tenant="alpha")
            c.check(r.is_error and "read-only" in r.text, "reader cannot save (MCP)", r.text[:120])
            r = await tool(s, "list_memories")
            c.check(r.is_error and "pass tenant" in r.text, "reader without tenant gets tenant_required (MCP)",
                    r.text[:120])
        async with mcp_session(self.base, k["nodelete"]) as s:
            r = await tool(s, "delete_memory", memory_id=self.alpha_id)
            c.check(r.is_error and "denied" in r.text, "denied-tools key cannot delete (MCP)", r.text[:120])
        r = self.get(k["alpha"], f"/memories/{self.alpha_id}")
        c.check(r.status_code == 200, "memory still exists after the refused MCP delete")

    # 7 KG
    async def kg(self) -> None:
        c, k = self.c, self.keys
        c.note("7. knowledge graph per tenant")
        native = f"smoke-{self.run}-svc"
        args = dict(provider="logical", type="workload", native_id=native, display_name=f"smoke {self.run} shared service")
        async with mcp_session(self.base, k["alpha"]) as s:
            r = await tool(s, "kg_upsert_entity", graph="alpha", **args)
            self.kg_key = (r.data or {}).get("key", "") if isinstance(r.data, dict) else ""
            c.check(not r.is_error and self.kg_key, "alpha key upserts an entity in graph alpha", r.text[:160])
        async with mcp_session(self.base, k["beta"]) as s:
            r = await tool(s, "kg_upsert_entity", graph="beta", **args)
            c.check(not r.is_error, "beta key upserts the same entity in graph beta", r.text[:160])
            r = await tool(s, "kg_upsert_entity", graph="alpha", **args)
            c.check(r.is_error and "No access to tenant 'alpha'" in r.text, "beta key refused on graph alpha (MCP)",
                    r.text[:160])
        r = self.get(k["beta"], f"/kg/alpha/entities/{self.kg_key}")
        c.check(r.status_code == 403, "beta key gets 403 on graph alpha (REST)", str(r.status_code))
        r = self.get(k["alpha"], f"/kg/alpha/entities/{self.kg_key}")
        c.check(r.status_code == 200, "alpha key reads its entity (REST)", str(r.status_code))
        for label in ("alpha", "beta"):
            async with mcp_session(self.base, k[label]) as s:
                r = await tool(s, "kg_resolve", query=args["display_name"], graphs="*")
                hits = (r.data or {}).get("results", []) if isinstance(r.data, dict) else []
                graphs = {h.get("graph") for h in hits}
                c.check(not r.is_error and bool(hits) and graphs == {label},
                        f"kg_resolve('*') for the {label} key returns only {label} hits (MCP)", str(sorted(graphs)))
        r = self.http.post("/kg/resolve", headers=self.bearer(k["alpha"]),
                           json={"query": args["display_name"], "graphs": "*"})
        graphs = {h.get("graph") for h in r.json().get("results", [])} if r.status_code == 200 else set()
        c.check(graphs == {"alpha"}, "POST /kg/resolve graphs=* for the alpha key returns only alpha (REST)",
                f"{r.status_code} {sorted(graphs)}")

    # 8 copy
    def copy(self) -> None:
        c, k = self.c, self.keys
        c.note("8. copy alpha -> beta")
        name = f"smoke {self.run} alpha rest"
        body = {"from": "alpha", "to": "beta", "ids": [self.alpha_id]}
        r = self.admin("POST", "/admin/copy", json={**body, "dry_run": True})
        res = r.json() if r.status_code == 200 else {}
        c.check(len(res.get("created", [])) == 1 and not res.get("updated") and not res.get("missing"),
                "dry run reports 1 created", f"{r.status_code} {res}")
        r = self.get(k["beta"], "/memories")
        c.check(name not in {m["name"] for m in r.json().get("memories", [])}, "dry run wrote nothing to beta")
        r = self.admin("POST", "/admin/copy", json={**body, "dry_run": False})
        res = r.json() if r.status_code == 200 else {}
        created = res.get("created", [])
        self.copy_id = created[0]["id"] if created else ""
        c.check(len(created) == 1 and self.copy_id != self.alpha_id, "copy creates 1 memory with a new id",
                f"{r.status_code} {res}")
        r = self.get(k["beta"], f"/memories/{self.copy_id}")
        c.check(r.status_code == 200 and r.json().get("name") == name, "beta key reads the copy", str(r.status_code))
        r = self.get(k["alpha"], f"/memories/{self.alpha_id}")
        c.check(r.status_code == 200, "source memory still in alpha (copy, not move)")
        if self.qdrant_url:
            from qdrant_client import QdrantClient
            pts = QdrantClient(url=self.qdrant_url).retrieve("memories_beta", [self.copy_id], with_payload=True)
            cf = pts[0].payload.get("copied_from", {}) if pts else {}
            c.check(cf.get("tenant") == "alpha" and cf.get("id") == self.alpha_id and cf.get("at")
                    and cf.get("by") == "Smoke Admin (ä)", "copy carries copied_from {tenant,id,at,by}", str(cf))
        else:
            c.note("copied_from not checked (no --qdrant-url; the API does not return it)")
        r = self.admin("POST", "/admin/copy", json={**body, "dry_run": False})
        res = r.json() if r.status_code == 200 else {}
        c.check(not res.get("created") and [u["id"] for u in res.get("updated", [])] == [self.copy_id],
                "copying again reports updated, not created", str(res))
        r = self.get(k["beta"], "/memories")
        n = sum(1 for m in r.json().get("memories", []) if m["name"] == name)
        c.check(n == 1, "beta holds exactly one copy", str(n))

    # 9 rotate / revoke
    async def rotate_revoke(self) -> None:
        c, k = self.c, self.keys
        c.note("9. rotate alpha key, revoke beta key")
        r = self.admin("POST", f"/admin/keys/{self.key_ids['alpha']}/rotate")
        ok = r.status_code == 200
        if ok:
            k["alpha2"] = r.json()["key"]
            self.key_ids["alpha2"] = r.json()["record"]["id"]
            c.secrets.append(k["alpha2"])
        c.check(ok and r.json()["record"]["rotated_from"] == self.key_ids["alpha"], "rotate returns a new key",
                str(r.status_code))
        r = self.get(k["alpha2"], f"/memories/{self.alpha_id}")
        c.check(r.status_code == 200, "rotated key works (REST)", str(r.status_code))
        async with mcp_session(self.base, k["alpha2"]) as s:
            r2 = await tool(s, "list_memories")
            c.check(not r2.is_error and self.alpha_id in _ids(r2.data or {}), "rotated key works (MCP)")
        r = self.get(k["alpha"], f"/memories/{self.alpha_id}")
        c.check(r.status_code == 200, "old alpha key still works (24 h grace)", str(r.status_code))
        keys = {x["id"]: x for x in self.admin("GET", "/admin/keys").json()["keys"]}
        exp = keys.get(self.key_ids["alpha"], {}).get("expires_at")
        from datetime import datetime, timezone
        left = (datetime.fromisoformat(exp) - datetime.now(timezone.utc)).total_seconds() / 3600 if exp else -1
        c.check(23.5 < left <= 24.01, "old alpha key now expires in 24 h", f"{left:.2f} h")

        r = self.admin("POST", f"/admin/keys/{self.key_ids['beta']}/revoke")
        c.check(r.status_code == 204, "revoke beta key is 204", str(r.status_code))
        ok, took = self.wait_status(lambda: self.get(k["beta"], "/memories"), 401)
        c.check(ok, f"revoked beta key gets 401 within {REVOKE_WAIT_S:.0f} s (took {took:.1f} s)")
        try:
            async with mcp_session(self.base, k["beta"]) as s:
                await tool(s, "list_memories")
            refused = False
        except Exception:  # noqa: BLE001 - the transport raises on the 401
            refused = True
        c.check(refused, "revoked beta key is refused over MCP")

    # 10 audit
    def audit(self) -> None:
        c = self.c
        c.note("10. audit")

        def rows(**params) -> list[dict]:
            out, cursor = [], None
            for _ in range(50):
                r = self.admin("GET", "/admin/audit", params={**params, **({"cursor": cursor} if cursor else {})})
                if r.status_code != 200:
                    c.check(False, f"GET /admin/audit {params}", str(r.status_code))
                    return out
                body = r.json()
                out += body["rows"]
                cursor = body.get("next_cursor")
                if not cursor:
                    return out
            return out

        alpha = rows(tenant="alpha")
        saved = {x["target"] for x in alpha if x["action"] == "save_memory" and x["outcome"] == "allowed"}
        c.check({self.legacy_id, self.alpha_id, self.alpha_mcp_id} <= saved,
                "audit tenant=alpha has rows for the legacy, REST and MCP saves")
        c.check(any(x["action"] == "save_memory" and x["agent"] == "smoke-alpha" for x in alpha),
                "audit rows carry the key's agent")
        denied = [x for x in alpha if x["outcome"] == "denied" and (x.get("detail") or {}).get("rule")]
        rules = {x["detail"]["rule"] for x in denied}
        c.check({"tool_denied", "key_read_only"} <= rules, "audit tenant=alpha has denied rows with rules",
                str(sorted(rules)))
        beta = rows(tenant="beta")
        copies = [x for x in beta if x["action"] == "copy_memory" and x["target"] == self.copy_id]
        c.check(len(copies) == 2 and all(x["detail"].get("from") == "alpha" for x in copies),
                "copy_memory rows (one per write) are recorded under the destination tenant beta", str(len(copies)))
        c.check(not any(x["action"] == "copy_memory" for x in alpha),
                "copy_memory rows are not under the source tenant alpha (recorded under beta)")
        everything = json.dumps(rows())
        c.check(not any(s in everything for s in c.secrets if s), "no audit row contains a token or generated key")
        secrets_parts = [s.rsplit("_", 1)[-1] for s in self.keys.values()]
        c.check(not any(p in everything for p in secrets_parts), "no audit row contains a key secret part")
        c.check(not any(t in everything for t in self.contents), "no audit row contains saved memory content")

    # 11 retire legacy
    async def retire(self) -> None:
        c, k = self.c, self.keys
        c.note("11. retire the legacy token")
        r = self.admin("POST", "/admin/legacy-token/retire")
        c.check(r.status_code == 204, "retire legacy token is 204", str(r.status_code))
        ok, took = self.wait_status(lambda: self.get(self.api_token, "/memories"), 401)
        c.check(ok, f"legacy token gets 401 within {REVOKE_WAIT_S:.0f} s (took {took:.1f} s)")
        r = self.get(k["alpha2"], f"/memories/{self.alpha_id}")
        c.check(r.status_code == 200, "rotated alpha key still works", str(r.status_code))
        r = self.get(k["reader"], "/memories", tenant="beta")
        c.check(r.status_code == 200, "reader key still works", str(r.status_code))
        async with mcp_session(self.base, k["alpha2"]) as s:
            r2 = await tool(s, "search_memories", query="smoke", limit=3)
            c.check(not r2.is_error, "scoped key still works over MCP")
        r = self.admin("GET", "/admin/settings")
        c.check(r.status_code == 200 and r.json().get("legacy_token_enabled") is False,
                "settings show legacy_token_enabled false")

    # restart
    def snapshot_state(self) -> dict:
        tenants = {t["id"]: t for t in self.admin("GET", "/admin/tenants").json()["tenants"]}
        return {"tenants": tenants, "keys": {x["id"]: x for x in self.admin("GET", "/admin/keys").json()["keys"]},
                "settings": self.admin("GET", "/admin/settings").json()}

    def wait_restart(self, timeout_s: float) -> bool:
        def health() -> bool:
            try:
                return self.http.get("/health", timeout=5).status_code == 200
            except httpx.HTTPError:
                return False
        print("WAITING FOR RESTART", flush=True)
        t0 = time.monotonic()
        while health():
            if time.monotonic() - t0 > timeout_s:
                return False
            time.sleep(0.5)
        while not health():
            if time.monotonic() - t0 > timeout_s:
                return False
            time.sleep(1)
        return True

    async def after_restart(self, before: dict) -> None:
        c, k = self.c, self.keys
        c.note("12. after a server restart")
        after = self.snapshot_state()
        c.check(set(after["tenants"]) == set(before["tenants"]) and {"alpha", "beta"} <= set(after["tenants"]),
                "tenants persisted", str(sorted(after["tenants"])))
        for t in ("alpha", "beta"):
            n0, n1 = before["tenants"][t]["memories"], after["tenants"][t]["memories"]
            c.check(n0 == n1 and n1 > 0, f"tenant {t} still holds its {n0} memories", f"{n0} -> {n1}")
        c.check(after["keys"] == before["keys"], "keys persisted unchanged")
        c.check(after["keys"][self.key_ids["beta"]]["revoked_at"] is not None, "beta key still revoked")
        c.check(after["settings"] == before["settings"], "settings persisted unchanged")
        c.check(after["settings"]["default_tenant"] == "alpha", "bootstrap did not change default_tenant")
        r = self.get(self.api_token, "/memories")
        c.check(r.status_code == 401, "legacy token still retired", str(r.status_code))
        r = self.get(k["beta"], "/memories")
        c.check(r.status_code == 401, "revoked key still refused", str(r.status_code))
        for label in ("alpha", "alpha2"):
            r = self.get(k[label], f"/memories/{self.legacy_id}")
            c.check(r.status_code == 200, f"{label} key reads the first (legacy) memory after restart", str(r.status_code))
        r = self.get(k["alpha2"], f"/kg/alpha/entities/{self.kg_key}")
        c.check(r.status_code == 200, "KG entity persisted (Postgres)", str(r.status_code))
        async with mcp_session(self.base, k["alpha2"]) as s:
            r2 = await tool(s, "list_memories")
            c.check(not r2.is_error and self.alpha_mcp_id in _ids(r2.data or {}), "MCP works after restart")

    async def run_all(self, expect_restart: bool, restart_timeout: float) -> int:
        self.basics()
        self.legacy_and_setup()
        self.rest_isolation()
        await self.mcp_isolation()
        await self.kg()
        self.copy()
        await self.rotate_revoke()
        self.audit()
        await self.retire()
        if expect_restart:
            before = self.snapshot_state()
            ok = self.wait_restart(restart_timeout)
            self.c.check(ok, "server went down and came back")
            if ok:
                await self.after_restart(before)
        return self.summary()

    def summary(self) -> int:
        print(f"==== {self.c.passed} passed, {self.c.failed} failed", flush=True)
        return 1 if self.c.failed else 0


# ── split_memories.py on a real Qdrant ────────────────────────────────────────
SPLIT_SRC = "smoke_split_src"
SPLIT_PREFIX = "smoke_split_"


def split_smoke(qdrant_url: str) -> int:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    c = Checks([])
    q = QdrantClient(url=qdrant_url)
    c.note("13. split_memories.py on a real Qdrant")
    for name in (SPLIT_SRC, f"{SPLIT_PREFIX}beta", f"{SPLIT_PREFIX}alpha"):
        if q.collection_exists(name):
            q.delete_collection(name)
    q.create_collection(SPLIT_SRC, vectors_config=VectorParams(size=384, distance=Distance.COSINE))
    rnd = random.Random(73)
    seed = [  # (source_repo, tags, expected tenant)
        ("org-a/app", ["runbook"], "alpha"), ("org-a/app", [], "alpha"), ("global", ["decision"], "alpha"),
        ("org-b/infra", ["terraform"], "beta"), ("org-b/web", [], "beta"), ("org-a/app", ["beta", "x"], "beta"),
    ]
    expected, now = {}, "2026-11-17T00:00:00+00:00"
    points = []
    for i, (repo, tags, tenant) in enumerate(seed):
        pid = str(uuid.uuid4())
        expected[pid] = tenant
        points.append(PointStruct(id=pid, vector=[rnd.uniform(-1, 1) for _ in range(384)], payload={
            "type": "reference", "name": f"split seed {i}", "content": f"seed content {i}", "source_repo": repo,
            "agent": "claude-code", "tags": tags, "created_at": now, "updated_at": now}))
    q.upsert(SPLIT_SRC, points=points, wait=True)
    stored = {str(p.id): p for p in q.retrieve(SPLIT_SRC, list(expected), with_payload=True, with_vectors=True)}
    c.check(len(stored) == 6, "seeded 6 points")

    script = Path(__file__).with_name("split_memories.py")
    base = [sys.executable, str(script), "--qdrant-url", qdrant_url, "--source", SPLIT_SRC,
            "--collection-prefix", SPLIT_PREFIX, "--rule", "source_repo=org-b/*:beta", "--rule", "tag=beta:beta",
            "--default", "alpha"]
    with tempfile.TemporaryDirectory() as tmp:
        plan_csv = Path(tmp) / "plan.csv"
        p = subprocess.run(base + ["--out", str(plan_csv)], capture_output=True, text=True)
        print(p.stdout.rstrip())
        c.check(p.returncode == 0, "dry run exits 0", p.stderr[-300:])
        import csv
        rows = list(csv.DictReader(plan_csv.open())) if plan_csv.exists() else []
        c.check({r["id"]: r["tenant"] for r in rows} == expected, "plan CSV assigns every point as expected")
        c.check(set(rows[0]) == {"id", "name", "source_repo", "tags", "tenant"} if rows else False,
                "plan CSV columns are id,name,source_repo,tags,tenant")
    c.check(q.count(SPLIT_SRC, exact=True).count == 6 and not q.collection_exists(f"{SPLIT_PREFIX}beta"),
            "dry run wrote nothing")

    p = subprocess.run(base + ["--apply"], capture_output=True, text=True)
    print(p.stdout.rstrip())
    c.check(p.returncode == 0, "apply exits 0", p.stderr[-300:])
    snaps = [s.name for s in q.list_snapshots(SPLIT_SRC)]
    c.check(len(snaps) == 1 and snaps[0] in p.stdout, "apply took a real snapshot and printed its name", str(snaps))
    beta_ids = sorted(i for i, t in expected.items() if t == "beta")
    alpha_ids = sorted(i for i, t in expected.items() if t == "alpha")
    src_after = {str(x.id) for x in q.scroll(SPLIT_SRC, limit=100)[0]}
    c.check(src_after == set(alpha_ids), "source keeps exactly the alpha points", str(len(src_after)))
    moved = {str(x.id): x for x in q.retrieve(f"{SPLIT_PREFIX}beta", beta_ids, with_payload=True, with_vectors=True)}
    c.check(set(moved) == set(beta_ids) and q.count(f"{SPLIT_PREFIX}beta", exact=True).count == 3,
            "target holds exactly the 3 beta points under their original ids")
    same_vec = all(max(abs(a - b) for a, b in zip(moved[i].vector, stored[i].vector)) < 1e-6 for i in moved)
    same_payload = all(moved[i].payload == stored[i].payload for i in moved)
    c.check(same_vec, "vectors identical after the move")
    c.check(same_payload, "payloads identical after the move")

    p = subprocess.run(base + ["--apply"], capture_output=True, text=True)
    print(p.stdout.rstrip())
    c.check(p.returncode == 0 and q.count(f"{SPLIT_PREFIX}beta", exact=True).count == 3
            and q.count(SPLIT_SRC, exact=True).count == 3, "re-running apply is a no-op")
    for name in (SPLIT_SRC, f"{SPLIT_PREFIX}beta"):
        q.delete_collection(name)
    print(f"==== {c.passed} passed, {c.failed} failed", flush=True)
    return 1 if c.failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", help="running server, e.g. http://localhost:58000")
    ap.add_argument("--qdrant-url", help="Qdrant URL, used only to check the stored copied_from payload")
    ap.add_argument("--expect-restart", action="store_true",
                    help="after the API checks, wait for a server restart and check persistence")
    ap.add_argument("--restart-timeout", type=float, default=300.0)
    ap.add_argument("--split-qdrant-url", help="run only the split_memories.py checks against this Qdrant")
    args = ap.parse_args(argv)
    if args.split_qdrant_url:
        return split_smoke(args.split_qdrant_url)
    if not args.base_url:
        ap.error("--base-url or --split-qdrant-url is required")
    env = {n: os.environ.get(n, "") for n in ("SMOKE_API_TOKEN", "SMOKE_UI_TOKEN", "SMOKE_ADMIN_GROUP")}
    missing = [n for n, v in env.items() if not v]
    if missing:
        ap.error(f"set {', '.join(missing)}")
    smoke = ApiSmoke(args.base_url, env["SMOKE_API_TOKEN"], env["SMOKE_UI_TOKEN"], env["SMOKE_ADMIN_GROUP"],
                     args.qdrant_url)
    try:
        return asyncio.run(smoke.run_all(args.expect_restart, args.restart_timeout))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - report, never dump request headers
        while isinstance(e, BaseExceptionGroup) and e.exceptions:
            e = e.exceptions[0]
        smoke.c.check(False, f"smoke run aborted: {type(e).__name__}", str(e)[:300])
        return smoke.summary()


if __name__ == "__main__":
    sys.exit(main())
